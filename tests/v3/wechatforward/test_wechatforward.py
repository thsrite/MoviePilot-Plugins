from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import wechatforward as wechatforward_module
from app.plugins.wechatforward import WeChatForward
from app.schemas.types import EventType, NotificationChannel
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "wechatforward" / "__init__.py"


@pytest.fixture
def plugin(monkeypatch: pytest.MonkeyPatch) -> WeChatForward:
    """构造不访问真实数据库或企业微信的插件实例。"""
    monkeypatch.setattr(wechatforward_module, "SubscribeOper", Mock)
    monkeypatch.setattr(wechatforward_module, "SubscribeHistoryOper", Mock)
    return WeChatForward()


def _imports() -> set[str]:
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引应发布独立实现，并阻止旧代目录继续回退。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["WeChatForward"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["WeChatForward"]

    assert manifest["version"] == WeChatForward.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v3.0.0"]
    assert legacy_manifest["v3"] is False


def test_v3_source_uses_sdk_and_only_stable_database_opers() -> None:
    """事件和网络必须走 SDK，宿主订阅数据只能通过现有 Oper 读取。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = _imports()

    assert {
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.network",
        "app.db.oper.subscribe",
        "app.db.oper.subscribehistory",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.core",
        "app.db.models",
        "app.db.session",
        "app.db.decorators",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "db_query" not in source
    assert "Session" not in source
    assert "if __name__ ==" not in source


def test_legacy_config_is_migrated_without_logging_secrets(plugin: WeChatForward) -> None:
    """旧逐行配置应迁移为 JSON 编辑器结构，并保留额外消息规则。"""
    config = {
        "wechat": "1001:corp:secret#下载通知",
        "pattern": "开始下载",
        "extra_confs": "开始下载 > user-1 > {name} 已提交 > 1001",
    }

    assert plugin._WeChatForward__sync_old_config(config) is True
    migrated = json.loads(config["wechat_confs"])
    assert migrated == [
        {
            "remark": "下载通知",
            "appid": "1001",
            "corpid": "corp",
            "appsecret": "secret",
            "pattern": "开始下载",
            "extra_confs": [
                {"pattern": "开始下载", "userid": "user-1", "msg": "{name} 已提交"}
            ],
        }
    ]


@pytest.mark.asyncio
async def test_notice_event_uses_typed_snapshot_and_filters_other_channels(
    plugin: WeChatForward,
) -> None:
    """通知事件必须先按 V3 快照读取，并只转发企业微信通知。"""
    plugin._enabled = True
    plugin._wechat_token_pattern_confs = {
        "1001": {"pattern": "已入库", "extra_confs": []}
    }
    plugin._WeChatForward__flush_access_token = AsyncMock(return_value="token")
    plugin._WeChatForward__send_message = AsyncMock(return_value=True)

    await plugin.send(
        Event(
            EventType.NoticeMessage,
            {
                "channel": NotificationChannel.Telegram.value,
                "title": "影片已入库",
                "text": "完成",
            },
        )
    )
    plugin._WeChatForward__send_message.assert_not_awaited()

    await plugin.send(
        Event(
            EventType.NoticeMessage,
            {
                "channel": NotificationChannel.Wechat.value,
                "title": "影片已入库",
                "text": "完成",
                "userid": "user-1",
            },
        )
    )
    plugin._WeChatForward__send_message.assert_awaited_once_with(
        title="影片已入库",
        text="完成",
        userid="user-1",
        access_token="token",
        appid="1001",
    )


@pytest.mark.asyncio
async def test_expired_token_is_refreshed_and_message_retried(
    monkeypatch: pytest.MonkeyPatch,
    plugin: WeChatForward,
) -> None:
    """企业微信报告令牌失效后应刷新一次，并使用新令牌重发原消息。"""

    class ResponseManager:
        def __init__(self, response: SimpleNamespace) -> None:
            self.response = response

        async def __aenter__(self) -> SimpleNamespace:
            return self.response

        async def __aexit__(self, *_args) -> None:
            return None

    responses = [
        SimpleNamespace(status_code=200, json=lambda: {"errcode": 42001}),
        SimpleNamespace(status_code=200, json=lambda: {"errcode": 0}),
    ]
    requested_urls: list[str] = []

    class Request:
        def response_manager(self, _method: str, url: str, **_kwargs) -> ResponseManager:
            requested_urls.append(url)
            return ResponseManager(responses.pop(0))

    monkeypatch.setattr(wechatforward_module, "AsyncRequestUtils", lambda **_kwargs: Request())
    plugin._wechat_token_pattern_confs = {"1001": {"remark": "下载通知"}}
    plugin._WeChatForward__flush_access_token = AsyncMock(return_value="fresh-token")
    plugin.async_get_data = AsyncMock(return_value=[])
    plugin.async_save_data = AsyncMock()

    result = await plugin._WeChatForward__post_request(
        access_token="expired-token",
        req_json={"touser": "user-1"},
        appid="1001",
        title="测试消息",
        text="正文",
        userid="user-1",
    )

    assert result is True
    assert len(requested_urls) == 2
    assert requested_urls[0].endswith("expired-token")
    assert requested_urls[1].endswith("fresh-token")
    plugin._WeChatForward__flush_access_token.assert_awaited_once_with(
        appid="1001",
        force=True,
    )
    plugin.async_save_data.assert_awaited_once()


@pytest.mark.asyncio
async def test_extra_download_message_uses_async_subscribe_oper(plugin: WeChatForward) -> None:
    """异步通知链路不得退回同步 Session，应通过异步订阅 Oper 查询。"""
    plugin.async_get_data = AsyncMock(return_value={})
    plugin.async_save_data = AsyncMock()
    plugin._subscribe_oper.async_list_by_username = AsyncMock(return_value=[])
    plugin._WeChatForward__send_message = AsyncMock(return_value=True)

    await plugin._WeChatForward__send_extra_msg(
        wechat_appid="1001",
        extra_confs=[
            {
                "pattern": "开始下载",
                "userid": "user-1",
                "msg": "{name} 已提交",
            }
        ],
        access_token="token",
        title="电视剧 示例 (2026) S01 E01 开始下载",
        text="用户：user-1\n",
    )

    plugin._subscribe_oper.async_list_by_username.assert_awaited_once_with(
        username="user-1",
        state="R",
        mtype="电视剧",
    )
    plugin._WeChatForward__send_message.assert_awaited_once()
    plugin.async_save_data.assert_awaited_once()
