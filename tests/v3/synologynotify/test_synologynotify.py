from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app import schemas
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins.synologynotify import SynologyNotify
from app.schemas.types import MessageType


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "synologynotify" / "__init__.py"

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
    """为插件基类提供隔离的消息链上下文。"""
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
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_import_contract() -> None:
    """V3 索引、旧代回退开关和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SynologyNotify"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["SynologyNotify"]
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert manifest["version"] == SynologyNotify.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {"app.plugins", "app.schemas.types", "app.sdk.logging"}.issubset(
        imports
    )
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
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
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "NotificationType" not in source


def test_plugin_lifecycle_and_api_contract() -> None:
    """插件应重置生命周期状态，并声明带统一响应模型的 Webhook API。"""
    plugin = SynologyNotify()

    assert plugin.get_state() is False
    plugin.init_plugin({"enabled": True, "notify": True, "msgtype": "Plugin"})
    assert plugin.get_state() is True
    assert plugin.get_api() == [
        {
            "path": "/webhook",
            "endpoint": plugin.send_notify,
            "methods": ["GET"],
            "auth": "apikey",
            "summary": "群辉webhook",
            "description": "接受群辉webhook通知并推送",
            "response_model": schemas.Response[None],
        }
    ]
    assert plugin.get_command() == []
    assert plugin.get_page() == []
    plugin.init_plugin({})
    assert plugin.get_state() is False
    assert plugin.stop_service() is None


def test_get_form_uses_v3_message_types() -> None:
    """配置表单的消息类型选项应来自 V3 MessageType。"""
    form, defaults = SynologyNotify().get_form()

    select = form[0]["content"][1]["content"][0]["content"][0]
    options = select["props"]["items"]
    assert options == [
        {"title": item.value, "value": item.name} for item in MessageType
    ]
    assert defaults == {"enabled": False, "notify": False, "msgtype": ""}


def test_send_notify_forwards_text_and_selected_message_type() -> None:
    """Webhook text 应按配置转发，并保留统一通知标题。"""
    plugin = SynologyNotify()
    plugin.init_plugin({"enabled": True, "notify": True, "msgtype": "Plugin"})
    plugin.post_message = Mock()

    response = plugin.send_notify(
        text="下载完成",
        title="忽略的标题",
        content="正文",
        url="https://example.test/detail",
    )

    assert response == schemas.Response(success=True, message="发送成功")
    plugin.post_message.assert_called_once_with(
        title="群辉通知",
        mtype=MessageType.Plugin,
        text="下载完成",
    )


def test_send_notify_uses_detail_fields_and_falls_back_to_manual_type() -> None:
    """没有 text 时应拼接详情链接，非法消息类型应回退为手动处理。"""
    plugin = SynologyNotify()
    plugin.init_plugin({"enabled": True, "notify": True, "msgtype": "unknown"})
    plugin.post_message = Mock()

    plugin.send_notify(
        title="详情标题",
        content="详情正文",
        url="https://example.test/detail",
    )

    plugin.post_message.assert_called_once_with(
        title="详情标题",
        mtype=MessageType.Manual,
        text="详情正文\n[查看详情](https://example.test/detail)",
    )


def test_send_notify_respects_notification_switch() -> None:
    """插件或通知开关关闭时不得投递消息，但 Webhook 仍返回成功。"""
    plugin = SynologyNotify()
    plugin.init_plugin({"enabled": True, "notify": False})
    plugin.post_message = Mock()

    response = plugin.send_notify(text="不会发送")

    assert response.success is True
    plugin.post_message.assert_not_called()


def test_send_notify_rejects_empty_payload() -> None:
    """空 Webhook 不得生成包含 None 的伪通知。"""
    plugin = SynologyNotify()
    plugin.init_plugin({"enabled": True, "notify": True})
    plugin.post_message = Mock()

    response = plugin.send_notify()

    assert response == schemas.Response(success=False, message="消息内容不能为空")
    plugin.post_message.assert_not_called()


def test_send_notify_omits_missing_detail_url() -> None:
    """详情 URL 缺失时通知正文不得包含无效 Markdown 链接。"""
    plugin = SynologyNotify()
    plugin.init_plugin({"enabled": True, "notify": True})
    plugin.post_message = Mock()

    plugin.send_notify(title="存储告警", content="空间不足")

    plugin.post_message.assert_called_once_with(
        title="存储告警",
        mtype=MessageType.Manual,
        text="空间不足",
    )
