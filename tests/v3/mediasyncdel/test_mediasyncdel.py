from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins import mediasyncdel as mediasyncdel_module
from app.plugins.mediasyncdel import MediaSyncDel
from app.schemas.types import EventType, MediaSource, MediaType
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "mediasyncdel" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["MediaSyncDel"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["MediaSyncDel"]

    assert manifest["version"] == MediaSyncDel.plugin_version == "2.0.1"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.1", "v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.services",
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
    assert "NotificationType" not in source
    assert "tmdbid" not in source
    assert "tmdb_id" not in source
    assert "logger.warn(" not in source


def test_plugin_initializes_and_declares_response_model(monkeypatch) -> None:
    """插件应在隔离宿主服务后初始化，并声明准确的 API 返回模型。"""
    downloader_helper = Mock()
    downloader_helper.get_services.return_value = {}
    monkeypatch.setattr(mediasyncdel_module, "DownloaderHelper", lambda: downloader_helper)
    monkeypatch.setattr(mediasyncdel_module, "TransferHistoryOper", Mock)
    monkeypatch.setattr(mediasyncdel_module, "DownloadHistoryOper", Mock)

    plugin = MediaSyncDel()
    plugin.init_plugin({})

    assert plugin.plugin_version == "2.0.1"
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]
    assert plugin.get_api()[0]["auth"] == "bear"
    assert plugin.get_command() == []
    assert plugin.stop_service() is None


def test_transfer_history_query_uses_complete_media_identity() -> None:
    """整理历史查询必须传递来源和来源原生 ID，不能退回裸 TMDB ID。"""
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()
    history = SimpleNamespace(id=1)
    plugin._transferhis.get_by.return_value = [history]

    message, histories = plugin._MediaSyncDel__get_transfer_his(
        media_type="Movie",
        media_name="示例电影",
        media_path="/media/example.mkv",
        media_source=MediaSource.Douban,
        media_id="1295644",
        season_num=None,
        episode_num=None,
    )

    assert histories == [history]
    assert "douban:1295644" in message
    plugin._transferhis.get_by.assert_called_once_with(
        media_source=MediaSource.Douban,
        media_id="1295644",
        mtype=MediaType.MOVIE.value,
        dest="/media/example.mkv",
    )


def test_invalid_episode_scope_fails_closed() -> None:
    """非法季集编号不得退化为整部剧的整理历史查询。"""
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()

    assert plugin._MediaSyncDel__get_transfer_his(
        media_type="Episode",
        media_name="示例剧",
        media_path="/media/example.mkv",
        media_source=MediaSource.Douban,
        media_id="34912145",
        season_num="not-a-season",
        episode_num="1",
    ) == ("", [])
    plugin._transferhis.get_by.assert_not_called()


def test_season_delete_without_identity_fails_closed() -> None:
    """整季删除缺少媒体身份时不得按季号或目标路径猜测整理记录。"""
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()

    message, histories = plugin._MediaSyncDel__get_transfer_his(
        media_type="Season",
        media_name="示例剧",
        media_path="/media/example/Season 02",
        media_source=None,
        media_id=None,
        season_num="2",
        episode_num=None,
    )

    assert "未识别媒体" in message
    assert histories == []
    plugin._transferhis.get_by.assert_not_called()


def test_webhook_season_delete_without_identity_stops_before_delete() -> None:
    """媒体服务器缺失身份的整季事件不得进入文件和历史删除管线。"""
    plugin = MediaSyncDel()
    plugin._enabled = True
    plugin._sync_type = "webhook"
    plugin._exclude_path = ""
    plugin.format_timestamp = Mock(return_value="2026-08-27 20:00:00")
    plugin._MediaSyncDel__sync_del = Mock()
    event_data = schemas.WebhookEventInfo(
        event="library.deleted",
        media_type="Season",
        item_name="示例剧 第二季",
        item_path="/media/example/Season 02",
        season_id="2",
    )

    plugin.sync_del_by_webhook(Event(EventType.WebhookMessage, event_data))

    plugin._MediaSyncDel__sync_del.assert_not_called()


def test_webhook_forwards_media_identity_to_delete_pipeline() -> None:
    """媒体服务器删除事件应把完整媒体身份传入同步删除链路。"""
    plugin = MediaSyncDel()
    plugin._enabled = True
    plugin._sync_type = "webhook"
    plugin._exclude_path = ""
    plugin.format_timestamp = Mock(return_value="2026-08-27 20:00:00")
    plugin._MediaSyncDel__sync_del = Mock()
    event_data = schemas.WebhookEventInfo(
        event="library.deleted",
        media_type="Movie",
        item_name="示例电影",
        item_path="/media/example.mkv",
        media_source=MediaSource.Douban,
        media_id="1295644",
    )

    plugin.sync_del_by_webhook(Event(EventType.WebhookMessage, event_data))

    plugin._MediaSyncDel__sync_del.assert_called_once_with(
        media_type="Movie",
        media_name="示例电影",
        media_path="/media/example.mkv",
        media_source=MediaSource.Douban,
        media_id="1295644",
        season_num=None,
        episode_num=None,
        delete_time="2026-08-27 20:00:00",
    )


def test_source_delete_failure_preserves_transfer_history(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """源文件删除失败时应保留整理历史，为后续重试保留事实依据。"""
    source_file = tmp_path / "source.mkv"
    source_file.write_bytes(b"video")
    transfer_history = SimpleNamespace(
        id=7,
        title="示例电影",
        year="2026",
        image=None,
        src=str(source_file),
        dest=str(tmp_path / "missing-destination.mkv"),
        download_hash=None,
        media_source=MediaSource.TMDB,
        media_id="550",
    )
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()
    plugin._downloadhis = Mock()
    plugin._del_source = True
    plugin._notify = False
    plugin._library_path = ""
    plugin._MediaSyncDel__get_transfer_his = Mock(
        return_value=("电影 示例电影 themoviedb:550", [transfer_history])
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        Mock(side_effect=OSError("source is busy")),
    )

    with pytest.raises(OSError, match="source is busy"):
        plugin._MediaSyncDel__sync_del(
            media_type="Movie",
            media_name="示例电影",
            media_path=str(tmp_path / "deleted-library-item.mkv"),
            media_source=MediaSource.TMDB,
            media_id="550",
            season_num=None,
            episode_num=None,
        )

    plugin._transferhis.delete.assert_not_called()


def test_torrent_cleanup_failure_preserves_transfer_history(tmp_path: Path) -> None:
    """下载器清理失败时应保留整理历史，避免把部分完成误报为成功。"""
    transfer_history = SimpleNamespace(
        id=8,
        title="示例电影",
        type=MediaType.MOVIE.value,
        year="2026",
        image=None,
        src=str(tmp_path / "missing-source.mkv"),
        dest=str(tmp_path / "missing-destination.mkv"),
        download_hash="hash-1",
        media_source=MediaSource.TMDB,
        media_id="550",
    )
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()
    plugin._downloadhis = Mock()
    plugin._del_source = True
    plugin._notify = False
    plugin._library_path = ""
    plugin.get_data = Mock(return_value=[])
    plugin.save_data = Mock()
    plugin.handle_torrent = Mock(return_value=(False, False, []))
    plugin._MediaSyncDel__get_transfer_his = Mock(
        return_value=("电影 示例电影 themoviedb:550", [transfer_history])
    )

    plugin._MediaSyncDel__sync_del(
        media_type="Movie",
        media_name="示例电影",
        media_path=str(tmp_path / "deleted-library-item.mkv"),
        media_source=MediaSource.TMDB,
        media_id="550",
        season_num=None,
        episode_num=None,
    )

    plugin.handle_torrent.assert_called_once_with(
        type=MediaType.MOVIE.value,
        src=transfer_history.src,
        torrent_hash="hash-1",
    )
    plugin._transferhis.delete.assert_not_called()
    plugin.save_data.assert_called_once()
