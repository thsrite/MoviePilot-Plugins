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
from app.application.chain.data import configure_chain_data_ports
from app.plugins import embyactorsync as embyactorsync_module
from app.plugins.embyactorsync import EmbyActorSync

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embyactorsync" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
            "download_history",
            "transfer_history",
            "transfer_pending",
            "transfer_execution",
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离 Chain 上下文。"""
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
        )
    )
    yield
    configure_chain_runtime_context_provider(None)


def _imports() -> set[str]:
    """返回插件源码显式声明的导入模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引、源码版本和旧代回退开关必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyActorSync"]
    package_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["EmbyActorSync"]
    v2_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyActorSync"]

    assert manifest["version"] == EmbyActorSync.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v2.0.0"]
    assert package_manifest["v3"] is False
    assert v2_manifest["v3"] is False


def test_v3_source_uses_sdk_and_has_no_legacy_imports() -> None:
    """V3 源码应只通过稳定 SDK 访问宿主能力。"""
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
    assert "RequestUtils().post(" not in source
    assert "_EMBY_HOST" not in source


class _Response:
    """提供可观察 close 行为的最小 HTTP 响应替身。"""

    def __init__(self, payload=None, status_code: int = 200, truthy: bool = True):
        self.payload = payload
        self.status_code = status_code
        self.closed = False
        self._truthy = truthy

    def __bool__(self) -> bool:
        return self._truthy

    def json(self):
        return self.payload

    def close(self) -> None:
        self.closed = True


def test_http_responses_are_closed_for_success_and_falsey_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emby 读写请求无论结果真假都必须释放响应连接。"""
    get_response = _Response({"Items": []}, truthy=False)
    post_response = _Response(status_code=204, truthy=False)
    request = Mock()
    request.get_res.return_value = get_response
    request.post_res.return_value = post_response
    monkeypatch.setattr(embyactorsync_module, "RequestUtils", lambda **_kwargs: request)

    plugin = EmbyActorSync()
    context = embyactorsync_module._EmbyContext(
        name="主 Emby",
        host="https://emby.example",
        user="user-id",
        api_key="secret",
    )

    assert plugin._EmbyActorSync__get_items(context, "library") == []
    assert plugin._EmbyActorSync__update_item_info(context, "episode", {}) is True
    assert get_response.closed is True
    assert post_response.closed is True


def test_sync_copies_season_people_and_locks_cast(monkeypatch: pytest.MonkeyPatch) -> None:
    """同步应保留季优先级，并在成功更新时锁定演员字段。"""
    plugin = EmbyActorSync()
    plugin._enabled = True
    plugin._mediaservers = ["主 Emby"]

    instance = Mock()
    instance.get_user.return_value = "user-id"
    instance.get_librarys.return_value = [
        SimpleNamespace(id="library", name="剧集", type="电视剧"),
        SimpleNamespace(id="movies", name="电影", type="电影"),
    ]
    plugin.mediaserver_helper = Mock()
    plugin.mediaserver_helper.get_services.return_value = {
        "主 Emby": SimpleNamespace(
            instance=instance,
            config=SimpleNamespace(
                config={"host": "emby.example/", "apikey": "secret"}
            ),
        )
    }

    responses = [
        _Response({"Items": [{"Id": "series", "Name": "示例剧 (2024)"}]}),
        _Response({"Name": "示例剧", "People": [{"Name": "季演员"}]}),
        _Response({"Items": [{"Id": "season"}]}),
        _Response({"Name": "第一季", "People": [{"Name": "季演员"}]}),
        _Response({"Items": [{"Id": "episode"}]}),
        _Response({"Name": "第一集", "People": [], "LockedFields": []}),
    ]
    request = Mock()
    request.get_res.side_effect = responses
    update_response = _Response(status_code=204)
    request.post_res.return_value = update_response
    monkeypatch.setattr(embyactorsync_module, "RequestUtils", lambda **_kwargs: request)
    monkeypatch.setattr(embyactorsync_module.time, "sleep", lambda _seconds: None)

    plugin.sync(library_name="剧集", media_name="示例剧")

    assert len(request.get_res.call_args_list) == 6
    request.post_res.assert_called_once()
    payload = request.post_res.call_args.kwargs["json"]
    assert payload["People"] == [{"Name": "季演员"}]
    assert payload["LockedFields"] == ["Cast"]
    assert all(response.closed for response in responses)
    assert update_response.closed is True


def test_missing_people_does_not_clear_episode_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """剧集和季都没有演员信息时不得向 Emby 写入空值。"""
    plugin = EmbyActorSync()
    context = embyactorsync_module._EmbyContext(
        name="主 Emby",
        host="https://emby.example",
        user="user-id",
        api_key="secret",
    )
    plugin._EmbyActorSync__get_item_info = Mock(
        side_effect=[
            {"Name": "示例剧", "People": []},
            {"Name": "第一季", "People": []},
        ]
    )
    plugin._EmbyActorSync__get_items = Mock(
        side_effect=[
            [{"Id": "season"}],
            [{"Id": "episode"}],
        ]
    )
    plugin._EmbyActorSync__update_item_info = Mock()

    plugin._EmbyActorSync__sync_series(context, {"Id": "series", "Name": "示例剧"})

    plugin._EmbyActorSync__update_item_info.assert_not_called()


def test_concurrent_sync_is_skipped_and_lock_released() -> None:
    """并发触发应跳过，任务完成后仍可再次执行。"""
    plugin = EmbyActorSync()
    plugin._EmbyActorSync__sync = Mock()
    assert plugin._run_lock.acquire(blocking=False) is True
    try:
        plugin.sync()
        plugin._EmbyActorSync__sync.assert_not_called()
    finally:
        plugin._run_lock.release()

    plugin.sync()
    plugin._EmbyActorSync__sync.assert_called_once_with(None, None, None)


def test_init_without_one_time_service_and_stop_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无一次性任务时不注册服务，重复停止也应安全。"""
    helper = Mock()
    helper.get_configs.return_value = {}
    monkeypatch.setattr(embyactorsync_module, "MediaServerHelper", lambda: helper)
    plugin = EmbyActorSync()

    plugin.init_plugin({"enabled": False, "onlyonce": False, "mediaservers": []})

    assert plugin.get_state() is False
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []
    plugin.stop_service()
    plugin.stop_service()


def test_init_registers_host_managed_one_time_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """立即运行任务应通过宿主服务合同注册并消费配置开关。"""
    monkeypatch.setattr(embyactorsync_module, "MediaServerHelper", Mock)
    plugin = EmbyActorSync()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin({"enabled": False, "onlyonce": True})

    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["id"] == "EmbyActorSync.Once"
    assert services[0]["trigger"] == "date"
    assert services[0]["func"] == plugin._run_once_sync
    assert plugin.get_state() is True
    assert plugin._onlyonce is False
    plugin.update_config.assert_called_once()
