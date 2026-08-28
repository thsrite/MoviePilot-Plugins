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
from app.plugins import embyextendtype as embyextendtype_module
from app.plugins.embyextendtype import EmbyExtendType
from app.schemas.types import MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embyextendtype" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
            "download_history",
            "transfer_history",
            "transfer_pending",
            "media_server",
            "download_failure",
            "user",
            "transfer_execution",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复全局提供器。"""
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
    """返回插件源码中显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_strict_sdk_contract() -> None:
    """V3 索引、旧代回退标记和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyExtendType"]
    legacy_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["EmbyExtendType"]
    legacy_v2 = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyExtendType"]

    assert manifest["version"] == EmbyExtendType.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_v1["v3"] is False
    assert legacy_v2["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
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
        "app.modules",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "NotificationType" not in source
    assert "app.log" not in source
    assert "api_key=" not in source


def test_plugin_initializes_without_scheduling_and_exposes_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """无启用配置时插件应完成初始化，表单只读取 SDK 媒体服务器配置。"""
    helper = Mock()
    helper.get_configs.return_value = {
        "主 Emby": SimpleNamespace(name="主 Emby", type="emby"),
        "Jellyfin": SimpleNamespace(name="Jellyfin", type="jellyfin"),
    }
    monkeypatch.setattr(embyextendtype_module, "MediaServerHelper", lambda: helper)

    plugin = EmbyExtendType()
    plugin.init_plugin({})

    form, defaults = plugin.get_form()
    assert plugin.get_state() is False
    assert plugin.get_api() == []
    assert plugin.get_command() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []
    assert plugin._scheduler is None
    assert defaults["msgtype"] == "Manual"
    assert defaults["mediaservers"] == []
    media_server_select = form[0]["content"][2]["content"][0]["content"][0]
    assert media_server_select["props"]["items"] == [
        {"title": "主 Emby", "value": "主 Emby"}
    ]


def test_check_extend_uses_selected_emby_service_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主检查链路应通过服务门面读取媒体库、按配置类型通知并关闭响应。"""

    class FakeResponse:
        status_code = 200

        def __init__(self) -> None:
            self.closed = False

        def json(self) -> dict:
            return {"Items": [{"Name": "喜剧"}]}

        def close(self) -> None:
            self.closed = True

    response = FakeResponse()
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(embyextendtype_module, "RequestUtils", lambda: request)

    emby = Mock()
    emby.get_librarys.return_value = [SimpleNamespace(id="lib-1", name="电影")]
    service = SimpleNamespace(
        instance=emby,
        config=SimpleNamespace(config={"host": "https://emby.example/", "apikey": "key"}),
    )
    helper = Mock()
    helper.get_services.return_value = {"主 Emby": service}

    plugin = EmbyExtendType()
    plugin._mediaserver_helper = helper
    plugin._extend = "动作, 喜剧,动作"
    plugin._notify = True
    plugin._msgtype = "Organize"
    plugin.post_message = Mock()

    plugin.check_extend()

    helper.get_services.assert_called_once_with(
        name_filters=[],
        type_filter="emby",
    )
    emby.get_librarys.assert_called_once_with()
    request.get_res.assert_called_once_with(
        url="https://emby.example/emby/ExtendedVideoTypes",
        params={
            "ParentId": "lib-1",
            "Recursive": "true",
            "IncludeItemTypes": "Episode,Movie",
            "Limit": 10,
            "api_key": "key",
        },
    )
    assert response.closed is True
    plugin.post_message.assert_called_once_with(
        title="Emby视频类型检查",
        mtype=MessageType.Organize,
        text="媒体库 电影 命中 喜剧 视频类型",
    )


def test_check_extend_fails_closed_for_missing_service_or_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务未连接、配置不完整或 Emby 响应无效时不得发送通知。"""

    class FalseyResponse:
        status_code = 500

        def __init__(self) -> None:
            self.closed = False

        def __bool__(self) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

        def json(self) -> dict:
            return {"Items": [{"Name": "喜剧"}]}

    response = FalseyResponse()
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(embyextendtype_module, "RequestUtils", lambda: request)

    emby = Mock()
    emby.get_librarys.return_value = [SimpleNamespace(id="lib-1", name="电影")]
    helper = Mock()
    helper.get_services.return_value = {
        "断开 Emby": SimpleNamespace(instance=None, config=SimpleNamespace(config={})),
        "配置错误": SimpleNamespace(
            instance=emby,
            config=SimpleNamespace(config={"host": "emby.example"}),
        ),
        "主 Emby": SimpleNamespace(
            instance=emby,
            config=SimpleNamespace(config={"host": "emby.example", "apikey": "key"}),
        ),
    }

    plugin = EmbyExtendType()
    plugin._mediaserver_helper = helper
    plugin._extend = "喜剧"
    plugin._notify = True
    plugin.post_message = Mock()

    plugin.check_extend()

    assert response.closed is True
    plugin.post_message.assert_not_called()


def test_legacy_library_selection_and_invalid_message_type_are_safe() -> None:
    """旧配置中的媒体库选择仍可使用，未知消息类型回退为手动处理。"""
    plugin = EmbyExtendType()
    plugin._librarys = ["儿童  library-2", "电影 library-1"]
    libraries = [
        SimpleNamespace(id="library-1", name="电影"),
        SimpleNamespace(id="library-2", name="儿童"),
        SimpleNamespace(id="library-3", name="剧集"),
    ]

    selected = plugin._select_libraries(libraries)

    assert [library.id for library in selected] == ["library-1", "library-2"]
    plugin._msgtype = "unknown"
    assert plugin._message_type() is MessageType.Manual
