from __future__ import annotations

import ast
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
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
from app.plugins import actorsubscribe as actorsubscribe_module
from app.plugins.actorsubscribe import ActorSubscribe
from app.schemas.types import MediaSource, MediaType


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "actorsubscribe" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
            "workflow",
            "download_history",
            "transfer_history",
            "transfer_execution",
            "transfer_pending",
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
            data_ports=get_chain_data_ports(),
        )
    )
    yield
    configure_chain_runtime_context_provider(None)


@pytest.fixture(autouse=True)
def _meta_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离需要宿主识别配置的元数据解析器。"""
    monkeypatch.setattr(
        actorsubscribe_module,
        "MetaInfo",
        lambda title: SimpleNamespace(title=title, type=None),
    )


def _imports() -> set[str]:
    """返回 V3 插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _media_info(**overrides):
    """构造带完整 V3 媒体身份和演员信息的影视信息。"""
    values = {
        "media_source": MediaSource.TMDB,
        "media_id": "550",
        "type": MediaType.MOVIE,
        "title": "搏击俱乐部",
        "title_year": "搏击俱乐部 (1999)",
        "year": "1999",
        "actors": [{"name": "演员"}],
        "directors": [],
        "imdb_id": None,
        "overview": "简介",
        "detail_link": "https://www.themoviedb.org/movie/550",
        "get_poster_image": Mock(return_value="poster"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _saved_values(plugin: ActorSubscribe) -> dict[str, object]:
    """读取插件本轮保存的数据快照。"""
    return {
        call.args[0]: call.args[1]
        for call in plugin.save_data.call_args_list
        if len(call.args) >= 2
    }


def _prepare_actor_run(
    plugin: ActorSubscribe,
    data: dict[str, object],
    mediainfo,
    source: str = "tmdb_movies",
) -> None:
    """注入演员订阅任务的链和隔离存储。"""
    plugin._actors = "演员"
    plugin._source = [source]
    plugin._quality = "Remux"
    plugin._resolution = "1080[pi]|x1080"
    plugin._effect = ""
    plugin._username = "演员订阅"
    plugin.mediachain = Mock()
    plugin.doubanchain = Mock()
    plugin.tmdbchain = Mock()
    plugin.downloadchain = Mock()
    plugin.subscribechain = Mock()
    if source == "tmdb_movies":
        plugin.tmdbchain.tmdb_discover.return_value = [mediainfo]
    else:
        plugin.doubanchain.movie_showing.return_value = [mediainfo]
    plugin.downloadchain.get_no_exists_info.return_value = (False, {})
    plugin.subscribechain.exists.return_value = False
    plugin.get_data = lambda key: data.get(key)
    plugin.save_data = Mock()


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关、版本和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["ActorSubscribe"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["ActorSubscribe"]

    assert manifest["version"] == ActorSubscribe.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {"app.sdk.config", "app.sdk.logging", "app.sdk.media"}.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
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
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "app.log" not in imports
    assert "logger.warn(" not in source
    assert "tmdbid=" not in source
    assert "doubanid=" not in source
    assert "apikey" not in source


def test_plugin_initializes_v3_chains_and_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件初始化应构造 V3 来源、下载和订阅链。"""
    douban_chain = Mock()
    tmdb_chain = Mock()
    download_chain = Mock()
    subscribe_chain = Mock()
    monkeypatch.setattr(actorsubscribe_module, "DoubanChain", lambda: douban_chain)
    monkeypatch.setattr(actorsubscribe_module, "TmdbChain", lambda: tmdb_chain)
    monkeypatch.setattr(actorsubscribe_module, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(actorsubscribe_module, "SubscribeChain", lambda: subscribe_chain)

    plugin = ActorSubscribe()
    plugin.get_data = Mock(return_value=[])
    plugin.init_plugin({})

    assert plugin.doubanchain is douban_chain
    assert plugin.tmdbchain is tmdb_chain
    assert plugin.downloadchain is download_chain
    assert plugin.subscribechain is subscribe_chain
    assert plugin.get_state() is False
    assert plugin.get_command() == []
    assert plugin.get_api()[0]["auth"] == "bear"
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
    plugin.stop_service()


def test_history_identity_migration_is_idempotent() -> None:
    """存量 TMDB/豆瓣字段应迁移为统一 pair，并保留详情链接。"""
    history = [
        {
            "title": "搏击俱乐部",
            "type": "电影",
            "tmdbid": 550,
            "doubanid": "0",
            "time": "2026-08-28 10:00:00",
        }
    ]
    original = deepcopy(history)
    saved = []
    plugin = ActorSubscribe()
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, deepcopy(value)))

    plugin._ActorSubscribe__migrate_history_identity()

    item = history[0]
    assert item["media_source"] == MediaSource.TMDB.value
    assert item["media_id"] == "550"
    assert item["detail_link"] == "https://www.themoviedb.org/movie/550"
    assert item["unique"] == "actorsubscribe: 搏击俱乐部 (tmdb:550)"
    assert {"tmdbid", "doubanid"}.isdisjoint(item)
    assert saved == [("history", history)]

    plugin._ActorSubscribe__migrate_history_identity()
    assert history != original
    assert len(saved) == 1


def test_invalid_history_identity_is_preserved() -> None:
    """无法回填合法身份的记录不得被清理或改写。"""
    history = [
        {
            "title": "未知媒体",
            "media_source": "invalid source",
            "media_id": "0",
            "tmdbid": "0",
            "doubanid": "",
        }
    ]
    original = deepcopy(history)
    saved = []
    plugin = ActorSubscribe()
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, value))

    plugin._ActorSubscribe__migrate_history_identity()

    assert history == original
    assert saved == []


def test_actor_subscription_passes_media_identity_and_records_success() -> None:
    """演员命中后应把同一媒体 pair 传给检查、写入和历史。"""
    plugin = ActorSubscribe()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin.subscribechain.add.return_value = (7, "")

    plugin._ActorSubscribe__actor_subscribe()

    discover_kwargs = plugin.tmdbchain.tmdb_discover.call_args.kwargs
    assert discover_kwargs == {
        "mtype": MediaType.MOVIE,
        "sort_by": "popularity.desc",
        "with_genres": "",
        "with_original_language": "",
        "with_keywords": "",
        "with_watch_providers": "",
        "vote_average": 0,
        "vote_count": 0,
        "release_date": "",
        "page": 1,
    }
    plugin.downloadchain.get_no_exists_info.assert_called_once()
    plugin.subscribechain.exists.assert_called_once_with(mediainfo=mediainfo)
    plugin.subscribechain.add.assert_called_once_with(
        title="搏击俱乐部",
        year="1999",
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="550",
        exist_ok=True,
        quality="Remux",
        resolution="1080[pi]|x1080",
        effect="",
        username="演员订阅",
    )

    saved = _saved_values(plugin)
    assert saved["already_handle"] == ["tmdb:550"]
    assert saved["history"][0]["media_source"] == MediaSource.TMDB.value
    assert saved["history"][0]["media_id"] == "550"
    assert {"tmdbid", "doubanid"}.isdisjoint(saved["history"][0])


def test_failed_subscription_is_not_marked_as_handled() -> None:
    """订阅写入失败时不得伪装成已处理，后续任务仍可重试。"""
    plugin = ActorSubscribe()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin.subscribechain.add.return_value = (None, "写入失败")

    plugin._ActorSubscribe__actor_subscribe()

    saved = _saved_values(plugin)
    assert saved["history"] == []
    assert saved["already_handle"] == []


def test_legacy_handled_title_is_normalized_to_media_key() -> None:
    """旧标题年份键首次命中时应转为来源和原生 ID，避免跨来源误判长期存在。"""
    plugin = ActorSubscribe()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": ["搏击俱乐部 (1999)"]}
    _prepare_actor_run(plugin, data, mediainfo)

    plugin._ActorSubscribe__actor_subscribe()

    plugin.subscribechain.add.assert_not_called()
    saved = _saved_values(plugin)
    assert saved["history"] == []
    assert saved["already_handle"] == ["tmdb:550"]


def test_douban_actor_fallback_keeps_douban_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """豆瓣详情补充演员时仍应按候选的 Douban pair 创建订阅。"""
    plugin = ActorSubscribe()
    mediainfo = _media_info(
        media_source=MediaSource.Douban,
        media_id="129",
        title="示例电影",
        title_year="示例电影 (2026)",
        year="2026",
        actors=[],
        directors=[],
        detail_link="https://movie.douban.com/subject/129",
    )
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo, source="douban_showing")
    plugin.doubanchain.douban_info.return_value = {"actors": [{"name": "演员"}], "directors": []}
    plugin.subscribechain.add.return_value = (7, "")
    monkeypatch.setattr(actorsubscribe_module.time, "sleep", Mock())

    plugin._ActorSubscribe__actor_subscribe()

    plugin.doubanchain.douban_info.assert_called_once_with("129", mtype=MediaType.MOVIE)
    plugin.subscribechain.add.assert_called_once_with(
        title="示例电影",
        year="2026",
        mtype=MediaType.MOVIE,
        media_source=MediaSource.Douban,
        media_id="129",
        exist_ok=True,
        quality="Remux",
        resolution="1080[pi]|x1080",
        effect="",
        username="演员订阅",
    )


def test_history_page_and_api_use_bearer_auth_without_legacy_token() -> None:
    """历史删除接口应使用宿主认证，不再把旧 API token 拼入页面。"""
    plugin = ActorSubscribe()
    plugin.get_data = Mock(
        return_value=[
            {
                "title": "搏击俱乐部",
                "type": "电影",
                "time": "2026-08-28 10:00:00",
                "poster": "poster",
                "media_source": MediaSource.TMDB.value,
                "media_id": "550",
                "unique": "actorsubscribe: 搏击俱乐部 (tmdb:550)",
            }
        ]
    )
    page = plugin.get_page()
    page_text = repr(page)

    assert "apikey" not in page_text
    assert "https://www.themoviedb.org/movie/550" in page_text
    click_params = page[0]["content"][0]["content"][0]["events"]["click"]["params"]
    assert click_params == {"key": "actorsubscribe: 搏击俱乐部 (tmdb:550)"}
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
