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

    assert manifest["version"] == MediaSyncDel.plugin_version == "2.0.3"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.3", "v2.0.2", "v2.0.1", "v2.0.0"]
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

    assert plugin.plugin_version == "2.0.3"
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


def _scripter_x_plugin(item_isvirtual) -> MediaSyncDel:
    """构造 Scripter X 方式的插件实例，并按宿主行为原样赋值 item_isvirtual。"""
    plugin = MediaSyncDel()
    plugin._enabled = True
    plugin._sync_type = "plugin"
    plugin._exclude_path = ""
    plugin._MediaSyncDel__sync_del = Mock()
    event_data = schemas.WebhookEventInfo(
        event="media_del",
        item_type="Series",
        item_name="示例剧",
        item_path="/media/example",
        media_source=MediaSource.Douban,
        media_id="1295644",
    )
    # 宿主解析 GET 参数时直接属性赋值，不触发布尔校验，字符串会原样透传
    event_data.item_isvirtual = item_isvirtual
    plugin.sync_del_by_plugin(Event(EventType.WebhookMessage, event_data))
    return plugin


@pytest.mark.parametrize("item_isvirtual", ["False", "false", "0", False])
def test_scripter_x_non_virtual_flag_reaches_delete_pipeline(item_isvirtual) -> None:
    """Scripter X 上报的非虚拟标识（含字符串 "False"）必须进入同步删除链路。"""
    plugin = _scripter_x_plugin(item_isvirtual)

    plugin._MediaSyncDel__sync_del.assert_called_once_with(
        media_type="Series",
        media_name="示例剧",
        media_path="/media/example",
        media_source=MediaSource.Douban,
        media_id="1295644",
        season_num=None,
        episode_num=None,
    )


@pytest.mark.parametrize("item_isvirtual", ["True", "true", "1", True])
def test_scripter_x_virtual_flag_skips_delete(item_isvirtual) -> None:
    """Scripter X 上报的虚拟条目不得触发任何删除。"""
    plugin = _scripter_x_plugin(item_isvirtual)

    plugin._MediaSyncDel__sync_del.assert_not_called()


@pytest.fixture
def deletion_case(monkeypatch, tmp_path):
    """为删除行为准备隔离的下载/媒体库根、成功历史和下载器桩。"""
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    source = downloads / "movie" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    destination = library / "movie" / "destination.mkv"
    configuration = Mock()
    configuration.get.return_value = [{
        "storage": "local", "download_path": str(downloads),
        "library_storage": "local", "library_path": str(library),
    }]
    monkeypatch.setattr(mediasyncdel_module, "SystemConfigOper", lambda: configuration)
    plugin = MediaSyncDel()
    plugin._transferhis = Mock()
    plugin._transferhis.list_success_by_src.return_value = []
    plugin._downloadhis = Mock()
    plugin._del_source = True
    plugin._notify = False
    plugin._library_path = ""
    plugin._default_downloader = "test-downloader"
    plugin.get_data = Mock(return_value=[])
    plugin.save_data = Mock()
    plugin.del_data = Mock()
    plugin.post_message = Mock()
    chain = Mock()
    chain.remove_torrents.return_value = True
    chain.stop_torrents.return_value = True
    chain.obtain_specific_image.return_value = None
    plugin.chain = chain

    def history(**overrides):
        """构造一条完整的成功本地整理记录。"""
        values = dict(id=7, status=True, title="示例电影", type=MediaType.MOVIE.value,
                      year="2026", image=None, src=str(source), dest=str(destination),
                      src_storage="local", dest_storage="local", download_hash=None,
                      date="2026-08-27 10:00:00", media_source=MediaSource.TMDB, media_id="550")
        values.update(overrides)
        return SimpleNamespace(**values)

    def run(histories=None, **overrides):
        """运行单次事件，历史查询与所有宿主副作用均使用测试桩。"""
        plugin._MediaSyncDel__get_transfer_his = Mock(return_value=("示例电影", histories or [history()]))
        values = dict(media_type="Movie", media_name="示例电影", media_path=str(destination),
                      media_source=MediaSource.TMDB, media_id="550", season_num=None, episode_num=None)
        values.update(overrides)
        plugin._MediaSyncDel__sync_del(**values)

    return SimpleNamespace(plugin=plugin, source=source, destination=destination,
                           downloads=downloads, library=library, configuration=configuration,
                           history=history, run=run, chain=chain)


def test_source_delete_failure_preserves_transfer_history(monkeypatch, deletion_case) -> None:
    """源文件删除失败被单条隔离，既不删历史也不写成功记录。"""
    case = deletion_case
    monkeypatch.setattr(Path, "unlink", Mock(side_effect=OSError("source is busy")))
    case.run()
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin.save_data.assert_not_called()
    assert case.source.exists()


def test_torrent_cleanup_failure_preserves_transfer_history(deletion_case) -> None:
    """下载器清理失败后不写成功历史，源文件缺失时仍可按整理历史重试。"""
    case = deletion_case
    case.plugin.handle_torrent = Mock(side_effect=[(False, False, []), (True, True, ["hash-1"])])
    case.run([case.history(download_hash="hash-1")])
    assert not case.source.exists()
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin.save_data.assert_not_called()
    case.run([case.history(download_hash="hash-1")])
    case.plugin._transferhis.delete.assert_called_once_with(7)
    case.plugin.save_data.assert_called_once()


@pytest.mark.parametrize("overrides", [
    {"status": False}, {"status": None}, {"dest": None}, {"src": None},
    {"src_storage": "alist"}, {"dest_storage": "alist"},
    {"src": "relative.mkv"}, {"dest": "relative.mkv"},
    {"src": "/etc/private.mkv"}, {"dest": "/etc/private.mkv"},
    {"src": "/"}, {"dest": "/"}, {"src": "/tmp/folder.txt"},
])
def test_unsafe_history_has_no_side_effects(deletion_case, overrides) -> None:
    """失败、不完整、非本地和越界历史不得授权删除。"""
    case = deletion_case
    case.run([case.history(**overrides)])
    assert case.source.exists()
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin.save_data.assert_not_called()
    case.chain.remove_torrents.assert_not_called()


@pytest.mark.parametrize("media_path", [None, "", " ", "relative.mkv", 123])
def test_invalid_event_path_stops_before_query(deletion_case, media_path) -> None:
    """没有有效绝对事件路径时不能按身份扩大删除范围。"""
    case = deletion_case
    case.run(media_path=media_path)
    case.plugin._MediaSyncDel__get_transfer_his.assert_not_called()
    assert case.source.exists()


def test_one_record_failure_does_not_abort_later_records(deletion_case) -> None:
    """第一条失败后继续处理后续记录，并只统计真实成功数量。"""
    case = deletion_case
    case.plugin._notify = True
    case.plugin.handle_torrent = Mock(side_effect=[RuntimeError("offline"), (False, True, ["hash-2"])])
    second = case.downloads / "second.mkv"
    second.write_bytes(b"video")
    case.run([case.history(download_hash="hash-1"),
              case.history(id=8, src=str(second), download_hash="hash-2")])
    case.plugin._transferhis.delete.assert_called_once_with(8)
    assert "删除记录1个" in case.plugin.post_message.call_args.kwargs["text"]
    assert "处理失败1个" in case.plugin.post_message.call_args.kwargs["text"]
    case.plugin.save_data.assert_called_once()


def test_existing_destination_and_other_version_are_preserved(deletion_case) -> None:
    """仍存在、重新生成或同身份的另一版本库文件不能连带删除。"""
    case = deletion_case
    case.destination.parent.mkdir()
    case.destination.write_bytes(b"library video")
    case.run()
    assert case.destination.exists() and case.source.exists()
    case.destination.unlink()
    case.run([case.history(dest=str(case.library / "another-version.mkv"))])
    case.plugin._transferhis.delete.assert_not_called()
    assert case.source.exists()


def test_shared_source_is_preserved(deletion_case) -> None:
    """另一个仍存库文件共享同一源时保留源文件和种子。"""
    case = deletion_case
    other = case.library / "other-version.mkv"
    other.write_bytes(b"library video")
    case.plugin._transferhis.list_success_by_src.return_value = [case.history(id=8, dest=str(other))]
    case.run()
    assert case.source.exists() and other.exists()
    case.plugin._transferhis.delete.assert_not_called()


@pytest.mark.parametrize("path_kind", ["source", "source_parent", "destination", "destination_parent"])
def test_symlink_paths_are_rejected(deletion_case, tmp_path, path_kind) -> None:
    """历史路径或根内父级的符号链接不能扩大删除授权。"""
    case = deletion_case
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "video.mkv"
    outside_file.write_bytes(b"untouched")
    overrides = {}
    if path_kind.endswith("parent"):
        root = case.downloads if path_kind.startswith("source") else case.library
        link = root / "linked"
        link.symlink_to(outside, target_is_directory=True)
        overrides["src" if path_kind.startswith("source") else "dest"] = str(link / "video.mkv")
    else:
        root = case.downloads if path_kind == "source" else case.library
        link = root / "linked.mkv"
        link.symlink_to(outside_file)
        overrides["src" if path_kind == "source" else "dest"] = str(link)
    case.run([case.history(**overrides)])
    assert case.source.exists() and outside_file.exists()
    case.plugin._transferhis.delete.assert_not_called()


def test_parent_cleanup_keeps_non_media_files_and_roots(deletion_case) -> None:
    """清理只删除空目录，保留根目录和同目录其他非媒体文件。"""
    case = deletion_case
    subtitle = case.source.with_suffix(".srt")
    subtitle.write_text("subtitle", encoding="utf-8")
    case.run()
    assert not case.source.exists()
    assert subtitle.exists() and case.downloads.exists() and case.library.exists()
    subtitle.unlink()
    case.plugin._MediaSyncDel__remove_parent_dir(case.source, [case.downloads, case.library])
    assert not case.source.parent.exists() and case.downloads.exists()


@pytest.mark.parametrize("root_kind", ["source", "destination", "event"])
def test_configured_roots_are_never_deleted(deletion_case, root_kind) -> None:
    """下载根、库根或指向库根的整剧事件不能进入文件清理。"""
    case = deletion_case
    overrides = {"src": str(case.downloads)} if root_kind == "source" else {"dest": str(case.library)}
    if root_kind == "event":
        case.run(media_path=str(case.library), media_type="Series")
    else:
        case.run([case.history(**overrides)])
    assert case.source.exists() and case.downloads.exists() and case.library.exists()
    case.plugin._transferhis.delete.assert_not_called()


def test_series_scope_uses_path_boundary(deletion_case) -> None:
    """整剧事件只处理该目录子项，不能因字符串前缀匹配另一个目录。"""
    case = deletion_case
    event_path = case.library / "Show"
    case.run([case.history(dest=str(case.library / "Show-other" / "episode.mkv"))],
             media_type="Series", media_path=str(event_path))
    assert case.source.exists()
    case.run([case.history(dest=str(event_path / "Season 01" / "episode.mkv"))],
             media_type="Series", media_path=str(event_path))
    assert not case.source.exists()
    case.plugin._transferhis.delete.assert_called_once_with(7)


def test_missing_or_unreadable_directory_config_fails_closed(deletion_case) -> None:
    """根配置缺失或读取异常时禁止产生删除副作用。"""
    case = deletion_case
    case.configuration.get.return_value = []
    case.run()
    case.configuration.get.side_effect = RuntimeError("config unavailable")
    case.run()
    assert case.source.exists()
    case.plugin._transferhis.delete.assert_not_called()


@pytest.mark.parametrize("remaining", [True, False])
def test_torrent_remaining_files_choose_stop_or_task_only_delete(deletion_case, remaining) -> None:
    """部分删除暂停任务，全部删除只移除任务而保留其他共享数据。"""
    case = deletion_case
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=int(remaining))]
    case.plugin.get_data.return_value = None
    assert case.plugin.handle_torrent("电影", str(case.source), "hash-1") == (not remaining, True, ["hash-1"])
    if remaining:
        case.chain.stop_torrents.assert_called_once_with(hashs="hash-1", downloader=None)
        case.chain.remove_torrents.assert_not_called()
    else:
        case.chain.remove_torrents.assert_called_once_with(hashs="hash-1", delete_file=False, downloader=None)
        case.chain.stop_torrents.assert_not_called()


@pytest.mark.parametrize("result", [False, None])
@pytest.mark.parametrize("remaining", [True, False])
def test_downloader_must_confirm_success(deletion_case, result, remaining) -> None:
    """下载器返回失败或无服务时不能把清理误报为成功。"""
    case = deletion_case
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=int(remaining))]
    case.chain.stop_torrents.return_value = result
    case.chain.remove_torrents.return_value = result
    case.run([case.history(download_hash="hash-1")])
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin.save_data.assert_not_called()


def test_transferred_torrent_uses_target_id_and_preserves_retry_data_on_failure(deletion_case) -> None:
    """转种使用目标任务 ID，失败时保留转种记录供重试。"""
    case = deletion_case
    transfer = {"to_download": "target", "to_download_id": "target-hash", "delete_source": True}
    case.plugin.get_data.side_effect = lambda **kwargs: transfer if kwargs.get("plugin_id") == "TorrentTransfer" else []
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=0)]
    case.chain.remove_torrents.return_value = False
    assert case.plugin.handle_torrent("电影", str(case.source), "source-hash") == (False, False, [])
    case.chain.remove_torrents.assert_called_once_with(hashs="target-hash", delete_file=False, downloader="target")
    case.plugin.del_data.assert_not_called()


def test_auxiliary_seed_cycle_is_bounded(deletion_case) -> None:
    """辅种记录成环时每个任务最多处理一次，不发生无限递归。"""
    case = deletion_case
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=0)]
    case.plugin.get_data.side_effect = lambda **kwargs: ([{"downloader": "test-downloader", "torrents": [
        "hash-2" if kwargs["key"] == "hash-1" else "hash-1"]}]
        if kwargs.get("plugin_id") == "IYUUAutoSeed" else None)
    assert case.plugin.handle_torrent("电影", str(case.source), "hash-1") == (True, True, ["hash-1", "hash-2"])
    assert case.chain.remove_torrents.call_count == 2
    assert all(call.kwargs["delete_file"] is False for call in case.chain.remove_torrents.call_args_list)


def test_collection_failure_retains_transfer_history(deletion_case) -> None:
    """合集下载器失败不能被辅助清理吞掉并误删整理历史。"""
    case = deletion_case
    case.plugin.get_data.return_value = None
    case.plugin._downloadhis.get_files_by_hash.side_effect = [
        [SimpleNamespace(id=1, state=0)],
        [SimpleNamespace(id=2, state=0), SimpleNamespace(id=3, state=1)],
    ]
    case.plugin._downloadhis.get_files_by_fullpath.return_value = [
        SimpleNamespace(download_hash="collection", downloader="test")]
    case.chain.stop_torrents.return_value = False
    case.run([case.history(type="电视剧", download_hash="hash-1")])
    case.chain.stop_torrents.assert_called_once_with(hashs="collection", downloader="test")
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin.save_data.assert_not_called()


def test_successful_event_deletes_only_source_and_target_task(deletion_case) -> None:
    """正常媒体删除应删除对应源文件和下载任务，保存准确的成功结果。"""
    case = deletion_case
    unrelated = case.downloads / "other.mkv"
    unrelated.write_bytes(b"keep")
    case.plugin.get_data.return_value = None
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=0)]
    case.run([case.history(download_hash="hash-1")])
    assert not case.source.exists() and unrelated.exists()
    case.chain.remove_torrents.assert_called_once_with(hashs="hash-1", delete_file=False, downloader=None)
    case.plugin._transferhis.delete.assert_called_once_with(7)
    case.plugin.save_data.assert_called_once()


def test_invalid_records_do_not_prevent_later_success(deletion_case) -> None:
    """空目标和失败记录不会阻止后续成功历史的同步清理。"""
    case = deletion_case
    case.run([case.history(id=1, dest=None), case.history(id=2, status=False), case.history()])
    assert not case.source.exists()
    case.plugin._transferhis.delete.assert_called_once_with(7)


def test_history_only_mode_preserves_source_and_torrent(deletion_case) -> None:
    """关闭源同步时只移除确认已删除的本地整理历史。"""
    case = deletion_case
    case.plugin._del_source = False
    case.run([case.history(download_hash="hash-1")])
    assert case.source.exists()
    case.plugin._transferhis.delete.assert_called_once_with(7)
    case.chain.remove_torrents.assert_not_called()
    case.chain.stop_torrents.assert_not_called()


def test_failed_history_timestamp_does_not_block_success(deletion_case) -> None:
    """晚于删除事件的失败整理不应阻止早期成功记录的清理。"""
    case = deletion_case
    case.run([case.history(id=1, status=False, date="2026-08-28 10:00:00"), case.history()],
             delete_time="2026-08-27 11:00:00")
    case.plugin._transferhis.delete.assert_called_once_with(7)


def test_path_mapping_matches_directory_boundary(deletion_case) -> None:
    """路径映射只替换目录前缀，不能改写目录名中的重复文字。"""
    case = deletion_case
    case.plugin._library_path = f"/emby:{case.library}"
    case.run(media_path="/emby/movie/destination.mkv")
    case.plugin._transferhis.delete.assert_called_once_with(7)
    assert not case.source.exists()


def test_directory_disguised_as_media_is_preserved(deletion_case) -> None:
    """目录即使以媒体扩展名命名，也不能作为文件删除。"""
    case = deletion_case
    directory = case.downloads / "folder.mkv"
    directory.mkdir()
    (directory / "other.txt").write_bytes(b"keep")
    case.run([case.history(src=str(directory))])
    assert directory.is_dir() and case.source.exists()
    case.plugin._transferhis.delete.assert_not_called()


def test_missing_or_newer_record_date_is_isolated(deletion_case) -> None:
    """缺失或晚于事件的时间只跳过该条，不阻止其他可验证历史。"""
    case = deletion_case
    case.run([case.history(id=1, date=None), case.history(id=2, date="2026-08-28 10:00:00"),
              case.history()], delete_time="2026-08-27 11:00:00")
    case.plugin._transferhis.delete.assert_called_once_with(7)


def test_same_hash_on_other_downloader_is_still_synchronized(deletion_case) -> None:
    """不同下载器上的同哈希辅种仍需处理，去重不能只按哈希。"""
    case = deletion_case
    case.plugin._downloadhis.get_files_by_hash.return_value = [SimpleNamespace(state=0)]
    case.plugin.get_data.side_effect = lambda **kwargs: (
        [{"downloader": "other", "torrents": ["hash-1"]}]
        if kwargs.get("plugin_id") == "IYUUAutoSeed" else None)
    assert case.plugin.handle_torrent("电影", str(case.source), "hash-1")[1] is True
    assert case.chain.remove_torrents.call_count == 2
    assert case.chain.remove_torrents.call_args.kwargs == {
        "hashs": "hash-1", "delete_file": False, "downloader": "other"}
