from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import (
    configure_chain_data_ports,
    get_chain_data_ports,
)
from app.plugins import embymetatag as embymetatag_module
from app.plugins.embymetatag import EmbyMetaTag

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embymetatag" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
            "workflow",
            "download_history",
            "transfer_history",
            "transfer_pending",
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复未配置状态。"""
    configure_chain_runtime_context_provider(
        lambda: ChainRuntimeContext(
            module_manager=Mock(),
            plugin_manager=Mock(),
            event_manager=Mock(),
            message_oper=Mock(),
            message_helper=Mock(),
            file_cache=Mock(),
            async_file_cache=Mock(),
            message_queue_factory=lambda _callback: Mock(),
            module_dispatcher_factory=lambda **_kwargs: Mock(),
            data_ports=get_chain_data_ports(),
        )
    )
    yield
    configure_chain_runtime_context_provider(None)


def _imports() -> set[str]:
    """返回插件源码中显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引应与源码版本一致，并阻止旧代实现回退加载。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyMetaTag"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyMetaTag"]

    assert manifest["version"] == EmbyMetaTag.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v2.0.0"]
    assert legacy_manifest["v3"] is False


def test_v3_source_uses_sdk_and_has_no_legacy_database_or_http_imports() -> None:
    """V3 源码应使用稳定 SDK，不应再依赖宿主旧代 Model、Session 或工具路径。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = _imports()

    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.services",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "db_query" not in source
    assert "from requests import Session" not in source
    assert "MediaServerChain" not in source


def test_config_parsers_preserve_supported_forms() -> None:
    """三类旧配置格式应继续解析，并忽略空行和空标签。"""
    plugin = EmbyMetaTag()

    assert plugin._EmbyMetaTag__parse_library_tags("电影,剧集# 关注,收藏\n") == {
        "电影": ["关注", "收藏"],
        "剧集": ["关注", "收藏"],
    }
    assert plugin._EmbyMetaTag__parse_audio_tags(r"粤语|Cantonese#粤语,粤语") == [
        {"regex": r"粤语|Cantonese", "tags": ["粤语", "粤语"]}
    ]
    assert plugin._EmbyMetaTag__parse_name_tags(
        "特别篇#Series,Movie#精选,精选"
    ) == (
        {"特别篇": ["精选", "精选"]},
        {"特别篇": ["Series", "Movie"]},
    )


def test_http_responses_are_closed_even_when_falsey(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK HTTP 返回的错误或假值响应也必须释放连接。"""

    class FalseyResponse:
        status_code = 500

        def __init__(self) -> None:
            self.closed = False

        def __bool__(self) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    response = FalseyResponse()
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(embymetatag_module, "RequestUtils", lambda: request)

    plugin = EmbyMetaTag()
    plugin._emby_host = "https://emby/"
    plugin._emby_api_key = "secret"
    plugin._emby_user = "user"

    assert plugin._EmbyMetaTag__get_item_tags("item") == []
    assert response.closed is True


def test_failed_audio_tag_write_does_not_poison_cache(tmp_path: Path) -> None:
    """只有远端写入成功的媒体才能进入音频标签缓存。"""
    plugin = EmbyMetaTag()
    media_item = SimpleNamespace(item_id="movie-1", item_type="Movie", title="示例电影")
    plugin._emby = Mock()
    plugin._emby.get_librarys.return_value = [SimpleNamespace(id="library-1")]
    plugin._emby.get_items.return_value = [media_item]
    plugin._acc_tags = [{"regex": "Cantonese", "tags": ["粤语"]}]
    plugin._emby_name = "主 Emby"
    plugin._audio_files = {}
    plugin._audio_files_json = tmp_path / "audio_files.json"
    plugin._EmbyMetaTag__get_item_info = Mock(return_value=["Cantonese"])
    plugin._EmbyMetaTag__get_item_tags = Mock(return_value=[])
    plugin._EmbyMetaTag__add_tag = Mock(return_value=False)

    plugin._EmbyMetaTag__tag_audio()

    assert plugin._audio_files == {"主 Emby": []}
    plugin._EmbyMetaTag__add_tag.assert_called_once()


def test_audio_cache_isolated_by_media_server_and_requires_successful_write(tmp_path: Path) -> None:
    """同名项目 ID 必须按服务器隔离，未执行写入的项目不能进入缓存。"""
    plugin = EmbyMetaTag()
    media_item = SimpleNamespace(item_id="shared-id", item_type="Movie", title="示例电影")
    plugin._emby = Mock()
    plugin._emby.get_librarys.return_value = [SimpleNamespace(id="library-1")]
    plugin._emby.get_items.return_value = [media_item]
    plugin._acc_tags = [{"regex": "Cantonese", "tags": ["粤语"]}]
    plugin._audio_files = {"其他 Emby": ["shared-id"]}
    plugin._audio_files_json = tmp_path / "audio_files.json"
    plugin._emby_name = "主 Emby"
    plugin._EmbyMetaTag__get_item_info = Mock(return_value=["Cantonese"])
    plugin._EmbyMetaTag__get_item_tags = Mock(return_value=["粤语"])
    plugin._EmbyMetaTag__add_tag = Mock()

    plugin._EmbyMetaTag__tag_audio()

    assert plugin._audio_files == {
        "其他 Emby": ["shared-id"],
        "主 Emby": [],
    }
    plugin._EmbyMetaTag__add_tag.assert_not_called()


def test_legacy_audio_cache_without_server_identity_is_rebuilt(tmp_path: Path) -> None:
    """旧缓存缺少服务器归属，不能继续作为跨服务器去重依据。"""
    cache_file = tmp_path / "audio_files.json"
    cache_file.write_text('["shared-id"]', encoding="utf-8")
    plugin = EmbyMetaTag()
    plugin._audio_files = {"stale": ["item"]}
    plugin._audio_files_json = cache_file

    plugin._EmbyMetaTag__load_audio_cache()

    assert plugin._audio_files == {}


def test_concurrent_auto_tag_trigger_is_skipped_and_lock_released() -> None:
    """并发触发不得进入共享服务器上下文，任务结束后门禁必须恢复。"""
    plugin = EmbyMetaTag()
    plugin._EmbyMetaTag__run_auto_tag = Mock()
    assert plugin._run_lock.acquire(blocking=False) is True

    try:
        assert plugin.auto_tag() is False
        plugin._EmbyMetaTag__run_auto_tag.assert_not_called()
    finally:
        plugin._run_lock.release()

    assert plugin.auto_tag() is True
    plugin._EmbyMetaTag__run_auto_tag.assert_called_once()
    assert plugin._run_lock.acquire(blocking=False) is True
    plugin._run_lock.release()


def test_plugin_initializes_without_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    """无启用配置时插件应完成构造，且不启动后台调度器。"""
    monkeypatch.setattr(embymetatag_module, "MediaServerHelper", Mock)
    plugin = EmbyMetaTag()

    plugin.init_plugin({})

    assert plugin.plugin_version == "2.0.0"
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []
    assert plugin.get_state() is False
    assert plugin._scheduler is None
