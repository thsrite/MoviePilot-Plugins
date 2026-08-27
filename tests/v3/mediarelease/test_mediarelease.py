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
from app.application.chain.data import (
    configure_chain_data_ports,
    get_chain_data_ports,
)
from app.plugins import mediarelease as mediarelease_module
from app.plugins.mediarelease import MediaRelease
from app.schemas.types import MediaSource, MediaType


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "mediarelease" / "__init__.py"

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
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离的链运行时上下文。"""
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
    """返回 V3 插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _walk_dicts(value):
    """递归遍历页面描述中的字典节点。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["MediaRelease"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["MediaRelease"]

    assert manifest["version"] == MediaRelease.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.utilities",
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
    assert "app.modules" not in source
    assert "logger.warn(" not in source
    assert "self.tmdb" not in source


def test_plugin_initializes_with_v3_media_chain(monkeypatch) -> None:
    """插件初始化应构造 V3 媒体链，并完成历史迁移入口。"""
    media_chain = Mock()
    monkeypatch.setattr(mediarelease_module, "DownloadChain", Mock)
    monkeypatch.setattr(mediarelease_module, "SubscribeChain", Mock)
    monkeypatch.setattr(mediarelease_module, "MediaChain", lambda: media_chain)

    plugin = MediaRelease()
    plugin.get_data = Mock(return_value=[])
    plugin.init_plugin({})

    assert plugin.mediachain is media_chain
    assert plugin.get_state() is False
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
    assert plugin.get_api()[0]["auth"] == "bear"
    assert plugin.get_command()[0]["data"] == {"action": "media_release"}
    plugin.stop_service()


def test_subscribe_search_uses_complete_identity_and_v3_add_contract(monkeypatch) -> None:
    """将映搜索应使用 TMDB pair，并把同一 pair 传给入库与订阅链。"""
    plugin = MediaRelease()
    monkeypatch.setattr(
        mediarelease_module,
        "MetaInfo",
        lambda title: SimpleNamespace(name=title, type=None),
    )
    plugin.mediachain = Mock()
    plugin.downloadchain = Mock()
    plugin.subscribechain = Mock()

    mediainfo = SimpleNamespace(
        media_source=MediaSource.TMDB,
        media_id="550",
        type=MediaType.MOVIE,
        title="搏击俱乐部",
        title_year="搏击俱乐部 (1999)",
        year="1999",
        overview="简介",
        detail_link="https://www.themoviedb.org/movie/550",
        get_poster_image=Mock(return_value="poster"),
    )
    plugin.mediachain.search_medias.return_value = [mediainfo]
    plugin.downloadchain.get_no_exists_info.return_value = (False, {})
    plugin.subscribechain.exists.return_value = False
    plugin.subscribechain.add.return_value = (7, "")

    remaining, history = plugin._MediaRelease__subscribe(
        "搏击俱乐部 1999",
        MediaType.MOVIE,
        [],
    )

    assert remaining == []
    assert history[0]["media_source"] == MediaSource.TMDB.value
    assert history[0]["media_id"] == "550"
    assert {"tmdbid", "doubanid", "tmdb_id", "douban_id"}.isdisjoint(history[0])
    plugin.mediachain.search_medias.assert_called_once()
    assert plugin.mediachain.search_medias.call_args.kwargs["media_source"] == MediaSource.TMDB
    plugin.downloadchain.get_no_exists_info.assert_called_once_with(
        meta=plugin.downloadchain.get_no_exists_info.call_args.kwargs["meta"],
        mediainfo=mediainfo,
    )
    plugin.subscribechain.add.assert_called_once_with(
        title="搏击俱乐部",
        year="1999",
        mtype=MediaType.MOVIE,
        media_source=MediaSource.TMDB,
        media_id="550",
        season=None,
        exist_ok=True,
        username="影视将映订阅",
    )


def test_failed_subscription_is_not_recorded_as_history(monkeypatch) -> None:
    """订阅写入失败时不得把失败目标伪装成已处理历史。"""
    plugin = MediaRelease()
    monkeypatch.setattr(
        mediarelease_module,
        "MetaInfo",
        lambda title: SimpleNamespace(name=title, type=None),
    )
    plugin.mediachain = Mock()
    plugin.downloadchain = Mock()
    plugin.subscribechain = Mock()
    mediainfo = SimpleNamespace(
        media_source=MediaSource.TMDB,
        media_id="550",
        type=MediaType.MOVIE,
        title="搏击俱乐部",
        title_year="搏击俱乐部 (1999)",
        year="1999",
        overview="简介",
        detail_link="",
        get_poster_image=Mock(return_value="poster"),
    )
    plugin.mediachain.search_medias.return_value = [mediainfo]
    plugin.downloadchain.get_no_exists_info.return_value = (False, {})
    plugin.subscribechain.exists.return_value = False
    plugin.subscribechain.add.return_value = (None, "写入失败")

    remaining, history = plugin._MediaRelease__subscribe(
        "搏击俱乐部 1999", MediaType.MOVIE, []
    )

    assert remaining == []
    assert history == []


@pytest.mark.parametrize(
    "history,expected_source,expected_id",
    (
        (
            [{"title": "搏击俱乐部", "tmdbid": 550, "doubanid": "0"}],
            MediaSource.TMDB.value,
            "550",
        ),
        (
            [{"title": "双来源记录", "tmdbid": 550, "doubanid": "1295644"}],
            MediaSource.TMDB.value,
            "550",
        ),
        (
            [{"title": "示例", "media_source": "unknown", "media_id": "0", "doubanid": "129"}],
            MediaSource.Douban.value,
            "129",
        ),
    ),
)
def test_history_identity_migration_is_idempotent(
    history: list[dict],
    expected_source: str,
    expected_id: str,
) -> None:
    """存量来源专有 ID 应回填为统一 pair 并移除旧字段。"""
    plugin = object.__new__(MediaRelease)
    saved = []
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, deepcopy(value)))

    plugin._MediaRelease__migrate_history_identity()

    item = history[0]
    assert item["media_source"] == expected_source
    assert item["media_id"] == expected_id
    assert {"tmdbid", "doubanid"}.isdisjoint(item)
    expected_key = "tmdb:550" if expected_source == MediaSource.TMDB.value else "douban:129"
    assert expected_key in item["unique"]
    assert saved == [("history", history)]


def test_invalid_history_identity_preserves_original_record() -> None:
    """没有合法回填来源时应原样保留历史，避免丢失用户数据。"""
    plugin = object.__new__(MediaRelease)
    history = [{
        "title": "未知媒体",
        "media_source": "not a source",
        "media_id": "0",
        "tmdbid": "0",
        "doubanid": "",
    }]
    original = deepcopy(history)
    saved = []
    plugin.get_data = lambda _key: history
    plugin.save_data = lambda key, value: saved.append((key, value))

    plugin._MediaRelease__migrate_history_identity()

    assert history == original
    assert saved == []


def test_history_page_and_api_do_not_expose_legacy_api_key() -> None:
    """V3 详情删除接口使用宿主 Bearer 认证，不再拼接旧 API token。"""
    plugin = MediaRelease()
    history = [{
        "title": "搏击俱乐部",
        "type": MediaType.MOVIE.value,
        "time": "2026-08-27 20:00:00",
        "poster": "poster",
        "media_source": MediaSource.TMDB.value,
        "media_id": "550",
        "unique": "mediarelease: 搏击俱乐部 (themoviedb:550)",
    }]
    plugin.get_data = Mock(return_value=history)
    page = plugin.get_page()
    event = next(
        value["events"]["click"]
        for value in _walk_dicts(page)
        if "events" in value and "click" in value["events"]
    )

    assert event["params"] == {"key": history[0]["unique"]}
    assert "apikey" not in event["params"]
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
