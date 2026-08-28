from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins import actorsubscribeplus as actorsubscribeplus_module
from app.plugins.actorsubscribeplus import ActorSubscribePlus
from app.schemas.types import MediaSource, MediaType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "actorsubscribeplus" / "__init__.py"


@pytest.fixture(autouse=True)
def _meta_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离需要宿主识别配置的元数据解析器。"""
    monkeypatch.setattr(
        actorsubscribeplus_module,
        "MetaInfo",
        lambda title: SimpleNamespace(title=title),
    )


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _media_info(**overrides):
    """构造包含完整 V3 媒体身份的影视信息。"""
    values = {
        "media_source": MediaSource.TMDB,
        "media_id": "550",
        "type": MediaType.MOVIE,
        "title": "搏击俱乐部",
        "title_year": "搏击俱乐部 (1999)",
        "year": "1999",
        "first_air_date": None,
        "release_date": "1999-10-15",
        "vote_average": 8.8,
        "overview": "简介",
        "detail_link": "https://www.themoviedb.org/movie/550",
        "get_poster_image": Mock(return_value="poster"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _saved_values(plugin: ActorSubscribePlus) -> dict[str, object]:
    """读取插件本轮保存的数据快照。"""
    return {
        call.args[0]: call.args[1]
        for call in plugin.save_data.call_args_list
        if len(call.args) >= 2
    }


def _prepare_actor_run(plugin: ActorSubscribePlus, data: dict[str, object], mediainfo):
    """注入演员订阅任务的链和隔离存储。"""
    plugin._actors = "演员"
    plugin._mtype = [MediaType.MOVIE.value, MediaType.TV.value]
    plugin._year = 1990
    plugin._last = 30
    plugin._vate = 0
    plugin.mediachain = Mock()
    plugin.tmdbchain = Mock()
    plugin.downloadchain = Mock()
    plugin.subscribechain = Mock()
    plugin.mediachain.search_persons.return_value = [
        SimpleNamespace(source=MediaSource.TMDB.value, id=123)
    ]
    plugin.tmdbchain.person_credits.side_effect = [[mediainfo], []]
    plugin.downloadchain.get_no_exists_info.return_value = (False, {})
    plugin.subscribechain.exists.return_value = False
    plugin.get_data = lambda key: data.get(key)
    plugin.save_data = Mock()


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关、版本和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["ActorSubscribePlus"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["ActorSubscribePlus"]

    assert manifest["version"] == ActorSubscribePlus.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
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
    assert "tmdb_id" not in source
    assert "douban_id" not in source
    assert "tmdbid=" not in source
    assert "doubanid=" not in source
    assert "logger.warn(" not in source
    assert "apikey" not in source


def test_plugin_initializes_v3_chains_and_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件初始化应构造 V3 媒体、TMDB、下载和订阅链。"""
    media_chain = Mock()
    tmdb_chain = Mock()
    download_chain = Mock()
    subscribe_chain = Mock()
    monkeypatch.setattr(actorsubscribeplus_module, "MediaChain", lambda: media_chain)
    monkeypatch.setattr(actorsubscribeplus_module, "TmdbChain", lambda: tmdb_chain)
    monkeypatch.setattr(actorsubscribeplus_module, "DownloadChain", lambda: download_chain)
    monkeypatch.setattr(actorsubscribeplus_module, "SubscribeChain", lambda: subscribe_chain)

    plugin = ActorSubscribePlus()
    plugin.get_data = Mock(return_value=[])
    plugin.init_plugin({})

    assert plugin.mediachain is media_chain
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
    plugin = object.__new__(ActorSubscribePlus)
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, deepcopy(value)))

    plugin._ActorSubscribePlus__migrate_history_identity()

    item = history[0]
    assert item["media_source"] == MediaSource.TMDB.value
    assert item["media_id"] == "550"
    assert item["detail_link"] == "https://www.themoviedb.org/movie/550"
    assert item["unique"] == "actorsubscribeplus: 搏击俱乐部 (tmdb:550)"
    assert {"tmdbid", "doubanid"}.isdisjoint(item)
    assert saved == [("history", history)]

    plugin._ActorSubscribePlus__migrate_history_identity()
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
    plugin = object.__new__(ActorSubscribePlus)
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, value))

    plugin._ActorSubscribePlus__migrate_history_identity()

    assert history == original
    assert saved == []


def test_actor_subscription_passes_media_identity_and_records_success() -> None:
    """演员作品订阅应把同一媒体 pair 传给检查、写入和历史。"""
    plugin = ActorSubscribePlus()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin.subscribechain.add.return_value = (7, "")

    plugin._ActorSubscribePlus__actor_subscribe()

    plugin.mediachain.search_persons.assert_called_once_with(
        name="演员",
        media_source=MediaSource.TMDB,
    )
    assert plugin.tmdbchain.person_credits.call_args_list[0].kwargs == {
        "person_id": 123,
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
        username="演员作品订阅",
    )

    saved = _saved_values(plugin)
    assert saved["already_handle"] == ["tmdb:550"]
    assert saved["history"][0]["media_source"] == MediaSource.TMDB.value
    assert saved["history"][0]["media_id"] == "550"
    assert {"tmdbid", "doubanid"}.isdisjoint(saved["history"][0])


def test_failed_subscription_is_not_marked_as_handled() -> None:
    """订阅写入失败时不得伪装成已处理，后续任务仍可重试。"""
    plugin = ActorSubscribePlus()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin.subscribechain.add.return_value = (None, "写入失败")

    plugin._ActorSubscribePlus__actor_subscribe()

    saved = _saved_values(plugin)
    assert saved["history"] == []
    assert saved["already_handle"] == []


def test_completed_subscription_is_saved_before_later_actor_failure() -> None:
    """后续演员查询失败时，前序成功订阅的历史和去重状态仍应保留。"""
    plugin = ActorSubscribePlus()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin._actors = "演员一,演员二"
    person = SimpleNamespace(source=MediaSource.TMDB.value, id=123)
    plugin.mediachain.search_persons.side_effect = [
        [person],
        RuntimeError("演员服务不可用"),
    ]
    plugin.subscribechain.add.return_value = (7, "")

    with pytest.raises(RuntimeError, match="演员服务不可用"):
        plugin._ActorSubscribePlus__actor_subscribe()

    saved = _saved_values(plugin)
    assert saved["already_handle"] == ["tmdb:550"]
    assert saved["history"][0]["media_source"] == MediaSource.TMDB.value
    assert saved["history"][0]["media_id"] == "550"


@pytest.mark.parametrize("handled", [["tmdb:550"], ["搏击俱乐部 (1999)"]])
def test_existing_handled_identity_skips_subscription(handled: list[str]) -> None:
    """新旧已处理键都应阻止同一作品重复提交。"""
    plugin = ActorSubscribePlus()
    mediainfo = _media_info()
    data = {"history": [], "already_handle": handled}
    _prepare_actor_run(plugin, data, mediainfo)

    plugin._ActorSubscribePlus__actor_subscribe()

    plugin.subscribechain.add.assert_not_called()
    assert _saved_values(plugin)["already_handle"] == handled


def test_missing_release_date_is_skipped_without_crashing() -> None:
    """来源没有有效上映时间时应跳过该作品，而不是中断整轮任务。"""
    plugin = ActorSubscribePlus()
    mediainfo = _media_info(release_date=None, first_air_date=None)
    data = {"history": [], "already_handle": []}
    _prepare_actor_run(plugin, data, mediainfo)
    plugin.subscribechain.add.return_value = (7, "")

    plugin._ActorSubscribePlus__actor_subscribe()

    plugin.subscribechain.add.assert_not_called()
    assert _saved_values(plugin)["history"] == []


def test_history_page_and_api_use_bearer_auth_without_legacy_token() -> None:
    """历史删除接口应使用宿主认证，不再把旧 API token 拼入页面。"""
    plugin = ActorSubscribePlus()
    plugin.get_data = Mock(
        return_value=[
            {
                "title": "搏击俱乐部",
                "type": "电影",
                "time": "2026-08-28 10:00:00",
                "poster": "poster",
                "media_source": MediaSource.TMDB.value,
                "media_id": "550",
                "unique": "actorsubscribeplus: 搏击俱乐部 (tmdb:550)",
            }
        ]
    )
    page = plugin.get_page()
    page_text = repr(page)

    assert "apikey" not in page_text
    assert "https://www.themoviedb.org/movie/550" in page_text
    click_params = page[0]["content"][0]["content"][0]["events"]["click"]["params"]
    assert click_params == {"key": "actorsubscribeplus: 搏击俱乐部 (tmdb:550)"}
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
