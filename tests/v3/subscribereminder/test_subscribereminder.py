from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytz

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import subscribereminder as subscribereminder_module
from app.plugins.subscribereminder import SubscribeReminder
from app.schemas.types import MediaSource, MediaType, MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "subscribereminder" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _subscribe(**overrides):
    """构造包含完整 V3 媒体身份的最小订阅对象。"""
    values = {
        "name": "示例媒体",
        "year": "2026",
        "type": MediaType.MOVIE.value,
        "media_source": MediaSource.TMDB,
        "media_id": "550",
        "season": None,
        "episode_group": None,
        "backdrop": None,
        "poster": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_v3_manifest_and_import_contract() -> None:
    """V3 索引、旧代回退开关和源码导入边界应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SubscribeReminder"]
    package_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["SubscribeReminder"]
    legacy_manifest = package_manifest

    assert manifest["version"] == SubscribeReminder.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.media",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db.models",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "app.core.config" not in imports
    assert "app.db.subscribe_oper" not in imports
    assert "app.log" not in imports
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "NotificationType" not in source
    assert "subscribe.tmdbid" not in source


def test_initialization_defers_daily_schedule_to_host_and_rejects_bad_hour(
    monkeypatch,
) -> None:
    """启用插件时由宿主注册每日异步服务，非法时间不得注册任务。"""
    monkeypatch.setattr(subscribereminder_module, "SubscribeOper", Mock)
    monkeypatch.setattr(subscribereminder_module, "MediaChain", Mock)
    monkeypatch.setattr(subscribereminder_module, "TmdbChain", Mock)

    plugin = SubscribeReminder()
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": False,
            "time": "7",
            "subtype": ["movie"],
            "msgtype": "Plugin",
        }
    )

    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["id"] == "SubscribeReminder"
    assert inspect.iscoroutinefunction(services[0]["func"])
    assert plugin.get_state() is True

    plugin._time = "24"
    assert plugin.get_service() == []
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_page() == []
    plugin.stop_service()


def test_onlyonce_uses_short_lived_scheduler_and_persists_reset(monkeypatch) -> None:
    """立即运行应安排一次性异步回调，并在注册后持久化关闭开关。"""

    class FakeScheduler:
        def __init__(self, **_kwargs):
            self.jobs = []
            self.running = False

        def add_job(self, **kwargs):
            self.jobs.append(kwargs)

        def get_jobs(self):
            return self.jobs

        def start(self):
            self.running = True

        def remove_all_jobs(self):
            self.jobs.clear()

        def shutdown(self):
            self.running = False

    monkeypatch.setattr(subscribereminder_module, "BackgroundScheduler", FakeScheduler)
    monkeypatch.setattr(subscribereminder_module, "SubscribeOper", Mock)
    monkeypatch.setattr(subscribereminder_module, "MediaChain", Mock)
    monkeypatch.setattr(subscribereminder_module, "TmdbChain", Mock)

    plugin = SubscribeReminder()
    plugin.update_config = Mock()
    plugin.init_plugin({"enabled": False, "onlyonce": True})

    assert plugin._onlyonce is False
    assert plugin._scheduler.running is True
    assert len(plugin._scheduler.jobs) == 1
    assert plugin._scheduler.jobs[0]["name"] == "订阅提醒"
    plugin.update_config.assert_called_once_with(
        {
            "enabled": False,
            "onlyonce": False,
            "time": 9,
            "subtype": ["movie", "tv"],
            "msgtype": "Plugin",
        }
    )
    plugin.stop_service()
    assert plugin._scheduler is None


def test_stop_service_retains_scheduler_when_shutdown_fails() -> None:
    """关闭失败时必须保留调度器句柄，供后续生命周期重试收敛。"""
    scheduler = Mock()
    scheduler.running = True
    scheduler.shutdown.side_effect = RuntimeError("shutdown failed")

    plugin = SubscribeReminder()
    plugin._scheduler = scheduler
    plugin.stop_service()

    scheduler.remove_all_jobs.assert_called_once_with()
    scheduler.shutdown.assert_called_once_with()
    assert plugin._scheduler is scheduler


@pytest.mark.asyncio
async def test_send_notify_uses_complete_identity_and_async_v3_apis() -> None:
    """提醒查询必须使用完整媒体身份和异步 V3 链路。"""
    current_date = datetime.now(
        tz=pytz.timezone(subscribereminder_module.settings.TZ)
    ).date().isoformat()
    tv_subscribe = _subscribe(
        name="示例剧",
        type=MediaType.TV.value,
        media_source=MediaSource.Douban,
        media_id="34912145",
        season=2,
        episode_group="standard",
        backdrop="tv-backdrop.jpg",
    )
    movie_subscribe = _subscribe(
        name="示例电影",
        media_source=MediaSource.TMDB,
        media_id="550",
        poster="movie-poster.jpg",
    )

    subscribe_oper = Mock()
    subscribe_oper.async_list = AsyncMock(return_value=[tv_subscribe, movie_subscribe])
    media_chain = Mock()
    media_chain.async_convert_media_identity = AsyncMock(return_value={"id": 1399})
    media_chain.async_recognize_media = AsyncMock(
        return_value=SimpleNamespace(release_date=f"{current_date}T00:00:00")
    )
    tmdb_chain = Mock()
    tmdb_chain.async_tmdb_episodes = AsyncMock(
        return_value=[
            SimpleNamespace(episode_number=4, air_date=current_date),
            SimpleNamespace(episode_number=3, air_date=f"{current_date}T00:00:00"),
            SimpleNamespace(episode_number=5, air_date="2099-01-01"),
        ]
    )

    plugin = SubscribeReminder()
    plugin._subscribe_oper = subscribe_oper
    plugin._media_chain = media_chain
    plugin._tmdb_chain = tmdb_chain
    plugin._subtype = ["tv", "movie"]
    plugin._msgtype = "Plugin"
    plugin.post_message = Mock()

    await plugin._SubscribeReminder__send_notify()

    subscribe_oper.async_list.assert_awaited_once_with()
    media_chain.async_convert_media_identity.assert_awaited_once_with(
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="34912145",
        mtype=MediaType.TV,
        season=2,
    )
    tmdb_chain.async_tmdb_episodes.assert_awaited_once_with(
        tmdbid=1399,
        season=2,
        episode_group="standard",
    )
    media_chain.async_recognize_media.assert_awaited_once_with(
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="550",
    )

    calls = plugin.post_message.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["mtype"] is MessageType.Plugin
    assert calls[0].kwargs["title"] == "电视剧更新"
    assert calls[0].kwargs["text"] == "📺︎示例剧 (2026) S02E03-E04\n"
    assert calls[0].kwargs["image"] == "tv-backdrop.jpg"
    assert calls[1].kwargs["mtype"] is MessageType.Plugin
    assert calls[1].kwargs["title"] == "电影更新"
    assert calls[1].kwargs["text"] == "📽︎示例电影 (2026)\n"
    assert calls[1].kwargs["image"] == "movie-poster.jpg"


@pytest.mark.asyncio
async def test_send_notify_fails_closed_for_invalid_identity() -> None:
    """缺失或零值媒体身份时不得退化为旧的 TMDB ID 查询。"""
    subscribe_oper = Mock()
    subscribe_oper.async_list = AsyncMock(
        return_value=[
            _subscribe(
                type=MediaType.TV.value,
                media_source=MediaSource.Douban,
                media_id="0",
                season=1,
            ),
            _subscribe(
                type=MediaType.MOVIE.value,
                media_source=None,
                media_id=None,
            ),
        ]
    )
    media_chain = Mock()
    media_chain.async_convert_media_identity = AsyncMock()
    media_chain.async_recognize_media = AsyncMock()
    tmdb_chain = Mock()
    tmdb_chain.async_tmdb_episodes = AsyncMock()

    plugin = SubscribeReminder()
    plugin._subscribe_oper = subscribe_oper
    plugin._media_chain = media_chain
    plugin._tmdb_chain = tmdb_chain
    plugin.post_message = Mock()

    await plugin._SubscribeReminder__send_notify()

    media_chain.async_convert_media_identity.assert_not_awaited()
    media_chain.async_recognize_media.assert_not_awaited()
    tmdb_chain.async_tmdb_episodes.assert_not_awaited()
    plugin.post_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_batches_limits_each_message_to_eight_items() -> None:
    """消息分批上限应保持八条，且无图片时仍能正常发送。"""
    plugin = SubscribeReminder()
    plugin.post_message = Mock()
    items = [
        {
            "name": f"剧集{i}",
            "season": "S01",
            "episode": f"E{i:02d}",
            "image": None,
        }
        for i in range(1, 10)
    ]

    await plugin._SubscribeReminder__send_batches(
        items,
        title="电视剧更新",
        prefix="📺︎",
        message_type=MessageType.Plugin,
        include_episodes=True,
    )

    calls = plugin.post_message.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["text"].count("\n") == 8
    assert calls[1].kwargs["text"] == "📺︎剧集9 S01E09\n"
    assert calls[0].kwargs["image"] is None
    assert calls[1].kwargs["image"] is None
