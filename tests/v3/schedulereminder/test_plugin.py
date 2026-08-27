from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins.schedulereminder import ScheduleReminder
from app.schemas.types import MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "schedulereminder" / "__init__.py"

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
    """为插件基类提供隔离的 V3 Chain 上下文。"""
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


def test_v3_manifest_and_strict_sdk_contract() -> None:
    """V3 版本、代际路由和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["ScheduleReminder"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["ScheduleReminder"]

    assert manifest["version"] == ScheduleReminder.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy["v3"] is False

    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"app.sdk.config", "app.sdk.logging"}.issubset(imports)
    forbidden = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden) for module in imports)
    assert "BackgroundScheduler" not in PLUGIN_PATH.read_text(encoding="utf-8")


def test_services_parse_colons_isolate_invalid_lines_and_keep_timezone() -> None:
    """提醒内容允许冒号，错误行不得阻断其它宿主服务。"""
    plugin = ScheduleReminder()
    plugin.systemmessage = Mock()
    plugin.init_plugin(
        {
            "enabled": True,
            "confs": "备份:NAS:0 8 * * *\n错误行\n无效cron:not cron\n# 注释",
        }
    )

    services = plugin.get_service()

    assert len(services) == 1
    service = services[0]
    assert service["id"] == "ScheduleReminder.1"
    assert service["name"] == "备份:NAS提醒"
    assert isinstance(service["trigger"], CronTrigger)
    assert str(service["trigger"].timezone) == "Asia/Shanghai"
    assert service["kwargs"] == {}
    assert service["func_kwargs"] == {"theme": "备份:NAS"}
    assert plugin.systemmessage.put.call_count == 2


def test_service_callback_sends_manual_notification() -> None:
    """宿主执行服务函数时应发送原提醒内容。"""
    plugin = ScheduleReminder()
    plugin.post_message = Mock()
    plugin.init_plugin({"enabled": True, "confs": "喝水:*/5 * * * *"})

    service = plugin.get_service()[0]
    service["func"](**service["func_kwargs"])

    plugin.post_message.assert_called_once_with(
        mtype=MessageType.Manual,
        title="日程提醒",
        text="喝水",
    )


def test_disabled_plugin_exposes_no_services_and_static_surfaces() -> None:
    """禁用状态不注册任务，静态插件接口返回明确空集合。"""
    plugin = ScheduleReminder()
    plugin.init_plugin({"enabled": False, "confs": "喝水:*/5 * * * *"})

    form, defaults = plugin.get_form()
    assert plugin.get_state() is False
    assert plugin.get_service() == []
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_page() == []
    assert form
    assert defaults == {"enabled": False, "confs": ""}
    assert plugin.stop_service() is None
