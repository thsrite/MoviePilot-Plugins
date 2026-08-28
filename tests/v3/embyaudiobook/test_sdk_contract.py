from __future__ import annotations

import ast
import json
import os
import sys
from contextlib import contextmanager
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
from app.plugins import embyaudiobook as embyaudiobook_module
from app.plugins.embyaudiobook import EmbyAudioBook
from app.schemas.types import EventType
from app.sdk.events import Event


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embyaudiobook" / "__init__.py"

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
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复全局状态。"""
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
    """返回插件源码中显式声明的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _emby_server(name: str = "主 Emby") -> SimpleNamespace:
    """构造带有完整 V3 服务配置的 Emby 测试替身。"""
    instance = SimpleNamespace(get_user=lambda: "user-1")
    config = SimpleNamespace(config={"host": "https://emby.example/", "apikey": "secret"})
    return SimpleNamespace(name=name, instance=instance, config=config)


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyAudioBook"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyAudioBook"]
    legacy_v1_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["EmbyAudioBook"]

    assert manifest["version"] == EmbyAudioBook.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False
    assert legacy_v1_manifest["v3"] is False

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
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "NotificationType" not in source
    assert "def audiobook_artist" in source


def test_commands_keep_distinct_routes_and_are_v3_events() -> None:
    """整理和演播作者命令必须分别注册，避免旧实现的同名函数覆盖。"""
    commands = EmbyAudioBook.get_command()

    assert [(command["cmd"], command["data"]["action"]) for command in commands] == [
        ("/ab", "audiobook"),
        ("/aba", "audiobook_artist"),
    ]
    assert all(command["event"] is EventType.PluginAction for command in commands)


def test_host_and_message_type_normalization() -> None:
    """服务地址和旧配置中的消息类型值应转换为稳定的 V3 表示。"""
    plugin = EmbyAudioBook()

    assert plugin._normalize_host("emby.example/") == "http://emby.example/"
    assert plugin._normalize_host("https://emby.example///") == "https://emby.example/"
    assert plugin._normalize_host("") == ""

    plugin._msgtype = "Manual"
    assert plugin._message_type().name == "Manual"
    plugin._msgtype = "手动处理"
    assert plugin._message_type().name == "Manual"
    plugin._msgtype = "not-exists"
    assert plugin._message_type().name == "Manual"


def test_http_response_manager_closes_falsey_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 网络返回的假值响应也必须在读取后关闭。"""

    class FalseyResponse:
        status_code = 200

        def __init__(self) -> None:
            self.closed = False

        def __bool__(self) -> bool:
            return False

        def json(self) -> dict:
            return {"Items": [{"Id": "book-1"}]}

        def close(self) -> None:
            self.closed = True

    response = FalseyResponse()

    class Request:
        @contextmanager
        def response_manager(self, *_args, **_kwargs):
            try:
                yield response
            finally:
                response.close()

    monkeypatch.setattr(embyaudiobook_module, "RequestUtils", lambda **_kwargs: Request())
    plugin = EmbyAudioBook()
    plugin._emby_host = "https://emby.example/"
    plugin._emby_api_key = "secret"

    assert plugin._EmbyAudioBook__request_json("emby/Artists") == {
        "Items": [{"Id": "book-1"}]
    }
    assert response.closed is True


def test_organize_items_updates_missing_metadata_and_extracts_episode_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整理应继承来源剧集元数据，并从中文文件名提取整数集数。"""
    source = {
        "Id": "episode-1",
        "Name": "filename",
        "Album": "示例有声书",
        "AlbumId": "album-1",
        "AlbumPrimaryImageTag": "tag-1",
        "Artists": ["作者"],
        "ArtistItems": [{"Id": "artist-1", "Name": "作者"}],
        "Composers": [{"Id": "artist-1", "Name": "作者"}],
        "AlbumArtist": "作者",
        "AlbumArtists": [{"Id": "artist-1", "Name": "作者"}],
        "ParentIndexNumber": 1,
    }
    target = {"Id": "episode-2", "Name": "第12集", "AlbumId": None}
    plugin = EmbyAudioBook()
    plugin._EmbyAudioBook__get_item_info = Mock(
        return_value={"Id": "episode-2", "Name": "filename", "Path": "/audio/第12集.mp3"}
    )
    plugin._EmbyAudioBook__update_item_info = Mock(return_value=True)
    monkeypatch.setattr(embyaudiobook_module.time, "sleep", lambda _seconds: None)

    assert plugin._EmbyAudioBook__organize_items([source, target], 1) is True
    update_call = plugin._EmbyAudioBook__update_item_info.call_args
    payload = update_call.args[1]
    assert update_call.args[0] == "episode-2"
    assert payload["AlbumId"] == "album-1"
    assert payload["IndexNumber"] == 12
    assert payload["Name"] == "第12集"
    assert payload["LockData"] is True


def test_audiobook_artist_command_updates_book_and_episodes() -> None:
    """演播作者命令应同时更新专辑和剧集，并删除旧作者项目。"""
    plugin = EmbyAudioBook()
    plugin._enabled = True
    plugin._library_id = "library-1"
    plugin._mediaserver_helper = Mock()
    plugin._mediaserver_helper.get_services.return_value = {"主 Emby": _emby_server()}
    plugin._EmbyAudioBook__get_items = Mock(
        side_effect=[
            [{"Id": "book-1", "Name": "示例书"}],
            [{"Id": "episode-1", "Name": "第1集"}],
        ]
    )
    plugin._EmbyAudioBook__get_item_info = Mock(
        side_effect=[
            {
                "Id": "book-1",
                "ArtistItems": [{"Id": "old-artist", "Name": "旧作者"}],
            },
            {"Id": "episode-1", "Name": "第1集"},
        ]
    )
    plugin._EmbyAudioBook__get_artists = Mock(
        return_value=[{"Id": "new-artist", "Name": "新作者"}]
    )
    plugin._EmbyAudioBook__update_item_info = Mock(return_value=True)
    plugin._EmbyAudioBook__delete_by_id = Mock(return_value=True)
    plugin.post_message = Mock()

    plugin.audiobook_artist(
        Event(
            EventType.PluginAction,
            {
                "action": "audiobook_artist",
                "arg_str": "主 Emby 示例书 新作者",
                "channel": "telegram",
                "user": "user-1",
            },
        )
    )

    assert plugin._EmbyAudioBook__update_item_info.call_count == 2
    plugin._EmbyAudioBook__delete_by_id.assert_called_once_with("old-artist")
    plugin.post_message.assert_called_once()
    assert "成功" in plugin.post_message.call_args.kwargs["title"]


def test_init_without_enablement_does_not_register_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """无启用配置时插件仍应完成 V3 生命周期初始化且不注册任务。"""
    monkeypatch.setattr(embyaudiobook_module, "MediaServerHelper", Mock)
    plugin = EmbyAudioBook()

    plugin.init_plugin({})

    assert plugin.get_state() is False
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []


def test_get_service_registers_host_managed_once_and_cron_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次性和周期任务应由宿主调度器注册并使用稳定服务 ID。"""
    monkeypatch.setattr(embyaudiobook_module, "MediaServerHelper", Mock)
    plugin = EmbyAudioBook()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "cron": "5 1 * * *",
        }
    )

    services = plugin.get_service()
    assert [service["id"] for service in services] == [
        "EmbyAudioBook.Once",
        "EmbyAudioBook",
    ]
    assert services[0]["trigger"] == "date"
    assert services[0]["func"] == plugin._run_once_check
    assert services[1]["func"] == plugin.check
    plugin.update_config.assert_called_once()
    assert plugin._onlyonce is False


def test_init_rebuilds_runtime_dependencies_inside_run_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置重载必须等待整理任务结束后再替换共享 Emby 上下文。"""

    class TrackingLock:
        entered = False

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, *_args):
            self.entered = False

    plugin = EmbyAudioBook()
    lock = TrackingLock()
    plugin._run_lock = lock
    helper_created_while_locked = []
    monkeypatch.setattr(
        embyaudiobook_module,
        "MediaServerHelper",
        lambda: helper_created_while_locked.append(lock.entered) or Mock(),
    )

    plugin.init_plugin({})

    assert helper_created_while_locked == [True]
