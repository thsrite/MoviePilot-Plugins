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
from app.plugins import popularsubscribe as popularsubscribe_module
from app.plugins.popularsubscribe import PopularSubscribe
from app.schemas.types import MediaSource, MediaType
from app.sdk.media import MediaInfo

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "popularsubscribe" / "__init__.py"


@pytest.fixture(autouse=True)
def _meta_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离需要宿主识别配置的元数据解析器。"""
    monkeypatch.setattr(
        popularsubscribe_module,
        "MetaInfo",
        lambda title: SimpleNamespace(title=title, type=None, begin_season=None),
    )


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _statistic_item(**overrides) -> dict:
    """构造包含完整 V3 媒体身份的热门订阅条目。"""
    item = {
        "name": "搏击俱乐部",
        "year": "1999",
        "type": "电影",
        "media_source": MediaSource.TMDB.value,
        "media_id": "550",
        "count": 20,
        "poster": "/poster.jpg",
        "description": "简介",
        "vote": 8.8,
        "genre_ids": [18],
    }
    item.update(overrides)
    return item


def _saved_values(plugin: PopularSubscribe) -> dict[str, object]:
    """读取插件本轮保存的数据快照。"""
    return {
        call.args[0]: deepcopy(call.args[1])
        for call in plugin.save_data.call_args_list
        if len(call.args) >= 2
    }


def _prepare_run(plugin: PopularSubscribe, items: list[dict], data: dict[str, object]):
    """注入热门订阅任务的链、统计响应和隔离存储。"""
    plugin.downloadchain = Mock()
    plugin.subscribechain = Mock()
    plugin.mediachain = Mock()
    plugin._username = "热门订阅"
    plugin.downloadchain.get_no_exists_info.return_value = (False, {})
    plugin.subscribechain.exists.return_value = False
    plugin.subscribechain.add.return_value = (7, "")
    plugin.get_data = lambda key: data.get(key)
    plugin.save_data = Mock()
    plugin._PopularSubscribe__get_subscribe_statistic = Mock(return_value=items)


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代路由、版本和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["PopularSubscribe"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["PopularSubscribe"]

    assert manifest["version"] == PopularSubscribe.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.network",
    }.issubset(imports)
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
    assert "logger.warn(" not in source
    assert "apikey" not in source

    tree = ast.parse(source)
    subscribe_add_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "subscribechain"
    ]
    assert subscribe_add_calls
    assert not {
        keyword.arg
        for call in subscribe_add_calls
        for keyword in call.keywords
    }.intersection({"tmdbid", "doubanid"})


def test_init_without_config_resets_runtime_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """空配置热重载不得保留上一次启用状态或任务参数。"""
    monkeypatch.setattr(popularsubscribe_module, "DownloadChain", Mock)
    monkeypatch.setattr(popularsubscribe_module, "SubscribeChain", Mock)
    monkeypatch.setattr(popularsubscribe_module, "MediaChain", Mock)
    plugin = PopularSubscribe()
    plugin._movie_enabled = True
    plugin._tv_enabled = True
    plugin._anime_enabled = True
    plugin.get_data = Mock(return_value=[])

    plugin.init_plugin(None)

    assert plugin.get_state() is False
    assert plugin._movie_cron == ""
    assert plugin._username == "热门订阅"
    plugin.stop_service()


def test_api_and_service_contract() -> None:
    """插件 API 应使用宿主认证，周期任务应由宿主服务投影注册。"""
    plugin = PopularSubscribe()
    plugin._movie_enabled = True
    plugin._movie_cron = "5 1 * * *"
    plugin._movie_page_cnt = 12
    plugin._movie_popular_cnt = 8

    api = plugin.get_api()[0]
    services = plugin.get_service()

    assert api["auth"] == "bear"
    assert api["response_model"] is schemas.Response[None]
    assert len(services) == 1
    assert services[0]["id"] == "PopularSubscribeMovie"
    assert services[0]["func_kwargs"] == {
        "stype": "电影",
        "page_cnt": 12,
        "popular_cnt": 8,
    }


def test_history_identity_migration_is_idempotent() -> None:
    """旧专用 ID 历史应迁移为统一媒体身份且重复执行不再写入。"""
    history = [{
        "title": "搏击俱乐部",
        "type": "电影",
        "year": "1999",
        "tmdbid": 550,
        "doubanid": "0",
        "time": "2026-08-28 10:00:00",
    }]
    plugin = object.__new__(PopularSubscribe)
    plugin.get_data = lambda _key: history
    plugin.save_data = Mock()

    plugin._PopularSubscribe__migrate_history_identity()

    assert history[0]["media_source"] == MediaSource.TMDB.value
    assert history[0]["media_id"] == "550"
    assert {"tmdbid", "doubanid"}.isdisjoint(history[0])
    assert "tmdb:550" in history[0]["unique"]
    assert plugin.save_data.call_count == 1

    plugin._PopularSubscribe__migrate_history_identity()
    assert plugin.save_data.call_count == 1


def test_build_media_requires_complete_identity() -> None:
    """中心统计缺少统一身份 pair 时不得退回旧专用 ID。"""
    valid = PopularSubscribe._PopularSubscribe__build_media(_statistic_item())
    invalid = PopularSubscribe._PopularSubscribe__build_media(
        _statistic_item(media_source=None, media_id=None, tmdbid=550)
    )

    assert valid.media_source == MediaSource.TMDB
    assert valid.media_id == "550"
    assert valid.type == MediaType.MOVIE
    assert invalid is None


def test_successful_subscription_uses_identity_and_checkpoints() -> None:
    """成功订阅应传递统一身份并及时保存历史和媒体键。"""
    plugin = PopularSubscribe()
    _prepare_run(plugin, [_statistic_item()], {"history": [], "already_handle": []})

    plugin._PopularSubscribe__popular_subscribe("电影", 30, 0)

    plugin.subscribechain.add.assert_called_once_with(
        title="搏击俱乐部",
        year="1999",
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="550",
        season=None,
        exist_ok=True,
        username="热门订阅",
    )
    saved = _saved_values(plugin)
    assert saved["already_handle"] == ["tmdb:550"]
    assert saved["history"][0]["media_source"] == MediaSource.TMDB.value
    assert saved["history"][0]["media_id"] == "550"


@pytest.mark.parametrize("handled", [["tmdb:550"], ["搏击俱乐部 (1999)"]])
def test_new_and_legacy_handled_keys_skip_subscription(handled: list[str]) -> None:
    """统一媒体键和存量标题键都应阻止重复订阅。"""
    plugin = PopularSubscribe()
    _prepare_run(plugin, [_statistic_item()], {"history": [], "already_handle": handled})

    plugin._PopularSubscribe__popular_subscribe("电影", 30, 0)

    plugin.subscribechain.add.assert_not_called()
    assert _saved_values(plugin)["already_handle"] == handled


def test_inventory_failure_is_fail_closed() -> None:
    """媒体库查询失败时不得继续创建订阅或写入已处理状态。"""
    plugin = PopularSubscribe()
    _prepare_run(plugin, [_statistic_item()], {"history": [], "already_handle": []})
    plugin.downloadchain.get_no_exists_info.side_effect = RuntimeError("媒体库不可用")

    plugin._PopularSubscribe__popular_subscribe("电影", 30, 0)

    plugin.subscribechain.exists.assert_not_called()
    plugin.subscribechain.add.assert_not_called()
    saved = _saved_values(plugin)
    assert saved["history"] == []
    assert saved["already_handle"] == []


def test_failed_subscription_is_not_marked_as_handled() -> None:
    """订阅写入失败时不得产生历史或已处理状态。"""
    plugin = PopularSubscribe()
    _prepare_run(plugin, [_statistic_item()], {"history": [], "already_handle": []})
    plugin.subscribechain.add.return_value = (None, "写入失败")

    plugin._PopularSubscribe__popular_subscribe("电影", 30, 0)

    saved = _saved_values(plugin)
    assert saved["history"] == []
    assert saved["already_handle"] == []


def test_completed_item_is_saved_before_later_candidate_failure() -> None:
    """后续候选构建失败时，前序成功订阅的状态仍应保留。"""
    plugin = PopularSubscribe()
    _prepare_run(
        plugin,
        [_statistic_item(), _statistic_item(name="第二部", media_id="551")],
        {"history": [], "already_handle": []},
    )
    first = PopularSubscribe._PopularSubscribe__build_media(_statistic_item())
    second = PopularSubscribe._PopularSubscribe__build_media(
        _statistic_item(name="第二部", media_id="551")
    )
    second.get_poster_image = Mock(side_effect=RuntimeError("海报构建失败"))
    plugin._PopularSubscribe__build_media = Mock(side_effect=[first, second])

    with pytest.raises(RuntimeError, match="海报构建失败"):
        plugin._PopularSubscribe__popular_subscribe("电影", 30, 0)

    saved = _saved_values(plugin)
    assert saved["already_handle"] == ["tmdb:550"]
    assert saved["history"][0]["media_id"] == "550"


def test_history_page_uses_bearer_route_without_legacy_token() -> None:
    """详情页删除动作不得暴露 API token，并应保留来源详情链接。"""
    plugin = PopularSubscribe()
    plugin.get_data = Mock(return_value=[{
        "title": "搏击俱乐部",
        "type": "电影",
        "year": "1999",
        "poster": "poster",
        "media_source": MediaSource.TMDB.value,
        "media_id": "550",
        "time": "2026-08-28 10:00:00",
        "unique": "popularsubscribe:搏击俱乐部:tmdb:550:2026-08-28 10:00:00",
    }])

    page = plugin.get_page()
    page_text = repr(page)

    assert "apikey" not in page_text
    assert "https://www.themoviedb.org/movie/550" in page_text
    params = page[0]["content"][0]["content"][0]["events"]["click"]["params"]
    assert params == {
        "key": "popularsubscribe:搏击俱乐部:tmdb:550:2026-08-28 10:00:00"
    }
