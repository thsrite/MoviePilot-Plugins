from __future__ import annotations

import ast
import inspect
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))


def _install_external_stubs() -> None:
    """为聚焦测试补 CloudDrive2 可选依赖，不访问真实服务。"""
    try:
        import clouddrive  # noqa: F401
    except ImportError:
        clouddrive = ModuleType("clouddrive")
        clouddrive.__path__ = []
        clouddrive.Client = Mock
        clouddrive.CloudDriveClient = Mock
        sys.modules["clouddrive"] = clouddrive

        proto = ModuleType("clouddrive.proto")
        proto.__path__ = []
        cloud_drive_pb2 = ModuleType("clouddrive.proto.CloudDrive_pb2")
        cloud_drive_pb2.AddOfflineFileRequest = lambda **kwargs: SimpleNamespace(**kwargs)
        cloud_drive_pb2.FileRequest = lambda **kwargs: SimpleNamespace(**kwargs)
        cloud_drive_pb2.GetUploadFileListRequest = (
            lambda **kwargs: SimpleNamespace(**kwargs)
        )
        proto.CloudDrive_pb2 = cloud_drive_pb2
        sys.modules["clouddrive.proto"] = proto
        sys.modules["clouddrive.proto.CloudDrive_pb2"] = cloud_drive_pb2

    try:
        from google.protobuf.json_format import MessageToDict  # noqa: F401
    except ImportError:
        google = sys.modules.setdefault("google", ModuleType("google"))
        google.__path__ = []
        protobuf = ModuleType("google.protobuf")
        protobuf.__path__ = []
        json_format = ModuleType("google.protobuf.json_format")
        json_format.MessageToDict = lambda value: dict(value)
        protobuf.json_format = json_format
        sys.modules["google.protobuf"] = protobuf
        sys.modules["google.protobuf.json_format"] = json_format


_install_external_stubs()

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins.cd2assistant import Cd2Assistant, CloudDriveInfo
from app.schemas.types import EventType
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cd2assistant" / "__init__.py"

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
            "transfer_execution",
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离的 Chain 运行时上下文。"""
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
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _registered_async_methods() -> set[str]:
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    registered = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "register"
            ):
                registered.add(node.name)
    return registered


def test_v3_manifest_import_and_event_contracts() -> None:
    """索引、SDK 导入、可选依赖与三个异步动作注册必须同步。"""
    v1 = json.loads((REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8"))
    v2 = json.loads((REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8"))
    v3 = json.loads((REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8"))
    manifest = v3["Cd2Assistant"]

    assert manifest["version"] == Cd2Assistant.plugin_version == "3.0.0"
    assert manifest["level"] == 2
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert v1["Cd2Assistant"]["v3"] is False
    assert v2["Cd2Assistant"]["v3"] is False
    assert (
        REPOSITORY_ROOT / "plugins.v3" / "cd2assistant" / "requirements.txt"
    ).read_text(encoding="utf-8") == (
        REPOSITORY_ROOT / "plugins.v2" / "cd2assistant" / "requirements.txt"
    ).read_text(encoding="utf-8")

    imports = _imports()
    assert {"app.sdk.config", "app.sdk.events", "app.sdk.logging"}.issubset(imports)
    forbidden = (
        "app.adapters",
        "app.core",
        "app.db.models",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden) for module in imports)
    assert {"restart_cd2", "add_offline_files", "cd2_info"}.issubset(
        _registered_async_methods()
    )
    assert "_cd2_assistant_restart" not in _registered_async_methods()


def test_plugin_initializes_and_declares_precise_homepage_api() -> None:
    """空配置初始化不连接外部服务，HomePage API 保持外部 API Key 合同。"""
    plugin = Cd2Assistant()
    plugin.get_config = Mock(return_value={})
    plugin.init_plugin({})

    api = plugin.get_api()[0]
    assert plugin.get_state() is False
    assert api["auth"] == "apikey"
    assert api["response_model"] is CloudDriveInfo
    assert inspect.iscoroutinefunction(api["endpoint"])
    assert plugin.get_service() == []
    assert plugin.get_page() == []


@pytest.mark.asyncio
async def test_restart_action_uses_typed_snapshot_and_async_rpc() -> None:
    """匹配的插件动作应调用异步 RPC，其它动作不能触发重启。"""
    client = SimpleNamespace(RestartService=AsyncMock(return_value=None))
    plugin = Cd2Assistant()
    plugin._clients = {"primary": client}
    plugin.post_message = Mock()

    await plugin.restart_cd2(
        Event(
            EventType.PluginAction,
            {"action": "cd2_restart", "arg_str": "primary", "user": "u1"},
        )
    )
    client.RestartService.assert_awaited_once_with(async_=True)

    client.RestartService.reset_mock()
    await plugin.restart_cd2(Event(EventType.PluginAction, {"action": "unrelated"}))
    client.RestartService.assert_not_awaited()


@pytest.mark.asyncio
async def test_homepage_returns_stable_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """HomePage endpoint 应把外部响应收敛为声明的稳定模型。"""
    plugin = Cd2Assistant()
    plugin._clients = {"primary": object()}
    plugin._cd2_clients = {"primary": object()}
    collect = AsyncMock(
        return_value={
            "cpuUsage": "1.25%",
            "memUsageKB": "64.00MB",
            "upload_count": 2,
            "download_count": 3,
        }
    )
    monkeypatch.setattr(plugin, "_Cd2Assistant__get_cd2_info", collect)

    result = await plugin.homepage("primary")

    assert result == CloudDriveInfo(
        cpuUsage="1.25%",
        memUsageKB="64.00MB",
        upload_count=2,
        download_count=3,
    )


@pytest.mark.parametrize("surface", ["page", "dashboard"])
def test_status_surfaces_refresh_live_info(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
) -> None:
    """详情页和仪表板每次渲染前都应刷新 CloudDrive2 状态。"""
    plugin = Cd2Assistant()
    client = object()
    cd2_client = object()
    plugin._clients = {"primary": client}
    plugin._cd2_clients = {"primary": cd2_client}
    collect = AsyncMock(
        return_value={
            "cpuUsage": "1.25%",
            "memUsageKB": "64.00MB",
            "upload_count": 2,
            "download_count": 3,
        }
    )
    monkeypatch.setattr(plugin, "_Cd2Assistant__get_cd2_info", collect)

    result = plugin.get_page() if surface == "page" else plugin.get_dashboard()

    assert result
    collect.assert_awaited_once_with(client=client, cd2_client=cd2_client)
    assert plugin._info["primary"]["cpuUsage"] == "1.25%"


def test_old_single_instance_config_is_migrated() -> None:
    """历史单实例配置应迁移为同前缀下的多实例配置。"""
    plugin = Cd2Assistant()
    plugin.get_config = Mock(
        return_value={
            "cd2_url": "http://cd2.example",
            "cd2_username": "user",
            "cd2_password": "password",
        }
    )
    plugin.update_config = Mock()

    plugin.init_plugin({})

    assert plugin._cd2_confs == "默认配置1#http://cd2.example#user#password"
    plugin.update_config.assert_called_once()


def test_stop_service_retains_failed_resources() -> None:
    """关闭失败的 scheduler 或 client 必须保留 owner，阻止重复初始化。"""
    scheduler = Mock()
    scheduler.remove_all_jobs.side_effect = RuntimeError("scheduler busy")
    failed_client = Mock()
    failed_client.close.side_effect = RuntimeError("client busy")
    closed_client = Mock()
    plugin = Cd2Assistant()
    plugin._scheduler = scheduler
    plugin._cd2_clients = {"primary": failed_client}
    plugin._clients = {"primary": closed_client}
    plugin._cd2_url = {"primary": "http://cd2.example"}

    plugin.stop_service()

    assert plugin._scheduler is scheduler
    assert plugin._cd2_clients == {"primary": failed_client}
    assert plugin._clients == {}
    assert plugin._cd2_url == {"primary": "http://cd2.example"}


def test_stop_service_releases_converged_resources() -> None:
    """资源确认关闭后应清空 owner 和实例 URL。"""
    scheduler = Mock(running=True)
    cd2_client = Mock()
    client = Mock()
    plugin = Cd2Assistant()
    plugin._scheduler = scheduler
    plugin._cd2_clients = {"primary": cd2_client}
    plugin._clients = {"primary": client}
    plugin._cd2_url = {"primary": "http://cd2.example"}

    plugin.stop_service()

    scheduler.shutdown.assert_called_once_with(wait=False)
    cd2_client.close.assert_called_once_with()
    client.close.assert_called_once_with()
    assert plugin._scheduler is None
    assert plugin._cd2_clients == {}
    assert plugin._clients == {}
    assert plugin._cd2_url == {}
