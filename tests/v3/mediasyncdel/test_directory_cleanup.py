"""目录路径兼容与蓝光源目录清理的真实文件系统回归。"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from app import schemas
from app.plugins.mediasyncdel import MediaSyncDel
from app.schemas.types import MediaSource, MediaType


HASH = "hash-a"
DOWNLOADER = "client-a"


@pytest.fixture
def cleanup_case(tmp_path: Path) -> SimpleNamespace:
    """仅创建临时原盘，数据库和下载器使用可观察的替身。"""
    root = tmp_path / "downloads" / "movie"
    relative_paths = ("BDMV/index.bdmv", "BDMV/STREAM/00001.m2ts", "CERTIFICATE/id.bdmv")
    records = []
    original_files = {}
    for index, relative in enumerate(relative_paths, 1):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"original-{index}".encode())
        original_files[path] = f"original-{index}".encode()
        records.append(SimpleNamespace(
            id=index, fullpath=str(path), downloader=DOWNLOADER,
            download_hash=HASH, state=1,
        ))

    sibling = root.parent / "unrelated.txt"
    sibling.write_bytes(b"keep sibling")
    events = []
    plugin = object.__new__(MediaSyncDel)
    plugin._downloadhis = Mock()
    plugin._transferhis = Mock()
    plugin._downloader_helper = Mock()
    plugin._downloader_helper.get_service.return_value = SimpleNamespace(
        config=SimpleNamespace(path_mapping=[]),
    )
    plugin.chain = Mock()
    plugin.get_data = Mock(return_value=[])
    plugin.save_data = Mock()
    plugin.del_data = Mock()
    plugin._default_downloader = "wrong-default"
    plugin._library_path = ""
    plugin._del_source = True
    plugin._notify = False
    history = SimpleNamespace(download_hash=HASH, downloader=DOWNLOADER)
    plugin._downloadhis.get_files_by_hash.return_value = records
    plugin._downloadhis.get_by_hash.return_value = history
    plugin._downloadhis.get_files_by_fullpath.side_effect = (
        lambda *, fullpath: [record for record in records if record.fullpath == fullpath]
    )
    task = schemas.DownloaderTorrent(
        hash=HASH, downloader=DOWNLOADER, name="movie", path=root,
        content_path=str(root), save_path=str(root.parent),
        state="seeding", progress=100, selected_completed=True,
    )
    manifest = [
        schemas.DownloaderFile(id=index, name=f"movie/{relative}", progress=1)
        for index, relative in enumerate(relative_paths, 1)
    ]
    plugin.chain.list_torrents.return_value = [task]
    plugin.chain.torrent_files.return_value = manifest

    def remove_torrent(**_kwargs):
        """文件清理完成后才能向精确下载器移除任务。"""
        assert not root.exists()
        events.append("torrent")
        return True

    def mark_deleted(*, fullpath):
        """记录删除标记必须晚于下载器确认。"""
        assert "torrent" in events
        events.append(("file-record", fullpath))
        for record in records:
            if record.fullpath == fullpath:
                record.state = 0

    plugin.chain.remove_torrents.side_effect = remove_torrent
    plugin._downloadhis.delete_file_by_fullpath.side_effect = mark_deleted
    plugin._transferhis.delete.side_effect = lambda _id: events.append("transfer-history")
    transfer = SimpleNamespace(
        id=17, title="示例电影", year="2026", image=None,
        src=str(root), dest=str(tmp_path / "library" / "deleted-movie"),
        src_storage="local", dest_storage="local", status=True,
        download_hash=HASH, downloader=DOWNLOADER,
        media_source=MediaSource.Douban, media_id="1295644",
    )
    plugin._transferhis.get_by.return_value = [transfer]
    return SimpleNamespace(
        plugin=plugin, root=root, records=records, history=history,
        transfer=transfer, sibling=sibling, events=events, tmp_path=tmp_path,
        task=task, manifest=manifest, original_files=original_files,
    )


def _cleanup(case, *, src=None, torrent_hash=HASH, downloader=DOWNLOADER):
    """调用生产目录清理入口，不模拟文件操作。"""
    return case.plugin._delete_bluray_source(
        str(case.root) if src is None else src,
        torrent_hash,
        downloader=downloader,
    )


def _sync(case):
    """通过事件实际使用的删除管线校验历史提交顺序。"""
    case.plugin._MediaSyncDel__sync_del(
        media_type="Movie", media_name="示例电影", media_path=case.transfer.dest,
        media_source=MediaSource.Douban, media_id="1295644",
        season_num=None, episode_num=None,
    )


def _assert_no_cleanup(case):
    """失败预检不得先删文件、改记录或调用下载器。"""
    for path, content in case.original_files.items():
        assert path.read_bytes() == content
    assert case.sibling.read_bytes() == b"keep sibling"
    case.plugin.chain.remove_torrents.assert_not_called()
    case.plugin._downloadhis.delete_file_by_fullpath.assert_not_called()
    assert all(record.state == 1 for record in case.records)


@pytest.mark.parametrize("requested, stored", [
    ("/media/movie", "/media/movie/"),
    ("/media/movie/", "/media/movie"),
])
def test_directory_lookup_retries_only_the_trailing_slash(requested, stored):
    """两个方向的兼容查询都必须保留完整媒体身份和类型。"""
    plugin = object.__new__(MediaSyncDel)
    plugin._transferhis = Mock()
    record = SimpleNamespace(id=1, dest=stored)
    plugin._transferhis.get_by.side_effect = [[], [record]]

    _, found = plugin._MediaSyncDel__get_transfer_his(
        "Movie", "示例电影", requested, MediaSource.Douban, "1295644", None, None,
    )

    assert found == [record]
    identity = dict(media_source=MediaSource.Douban, media_id="1295644", mtype=MediaType.MOVIE.value)
    assert plugin._transferhis.get_by.call_args_list == [
        call(**identity, dest=requested), call(**identity, dest=stored),
    ]


def test_exact_directory_lookup_wins_without_a_broader_query():
    """精确路径已有记录时不再查询另一种路径表示。"""
    plugin = object.__new__(MediaSyncDel)
    plugin._transferhis = Mock()
    record = SimpleNamespace(id=1)
    plugin._transferhis.get_by.return_value = [record]

    _, found = plugin._MediaSyncDel__get_transfer_his(
        "Movie", "示例电影", "/media/movie", MediaSource.Douban, "1295644", None, None,
    )

    assert found == [record]
    plugin._transferhis.get_by.assert_called_once_with(
        media_source=MediaSource.Douban, media_id="1295644",
        mtype=MediaType.MOVIE.value, dest="/media/movie",
    )


@pytest.mark.parametrize("path", ["", "/", "///"])
def test_blank_and_root_paths_never_enable_fallback(path):
    """缺少有效路径时不得放宽成按身份匹配所有版本。"""
    plugin = object.__new__(MediaSyncDel)
    plugin._transferhis = Mock()
    plugin._transferhis.get_by.return_value = []

    _, found = plugin._MediaSyncDel__get_transfer_his(
        "Movie", "示例电影", path, MediaSource.Douban, "1295644", None, None,
    )

    assert found == []
    assert plugin._transferhis.get_by.call_count <= 1
    for query in plugin._transferhis.get_by.call_args_list:
        assert query.kwargs["dest"] == path
        assert not query.kwargs.get("match_all_versions")


def test_complete_bluray_removes_only_owned_source_and_exact_torrent(cleanup_case):
    """完整原盘清理不碰相邻文件，也不采用默认下载器。"""
    case = cleanup_case

    assert _cleanup(case) == [HASH]

    assert not case.root.exists()
    assert case.sibling.read_bytes() == b"keep sibling"
    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, delete_file=False, downloader=DOWNLOADER,
    )
    assert case.events[0] == "torrent"
    assert case.plugin._downloadhis.delete_file_by_fullpath.call_args_list == [
        call(fullpath=record.fullpath) for record in case.records
    ]
    assert all(record.state == 0 for record in case.records)


def _keep_video_history_only(case):
    """模拟宿主仅同步视频扩展名、不记录原盘辅助文件的常见状态。"""
    case.records[:] = [record for record in case.records if record.fullpath.endswith(".m2ts")]


def test_downloader_manifest_authorizes_auxiliary_files_absent_from_database(cleanup_case):
    """下载器完整清单补齐辅助文件归属，只标记数据库中实际存在的记录。"""
    case = cleanup_case
    _keep_video_history_only(case)
    video = case.records[0]

    assert _cleanup(case) == [HASH]

    assert not case.root.exists()
    assert case.sibling.read_bytes() == b"keep sibling"
    case.plugin.chain.list_torrents.assert_called_once_with(
        hashs=HASH, downloader=DOWNLOADER, include_all_tags=True,
    )
    case.plugin.chain.torrent_files.assert_called_once_with(tid=HASH, downloader=DOWNLOADER)
    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, downloader=DOWNLOADER, delete_file=False,
    )
    case.plugin._downloadhis.delete_file_by_fullpath.assert_called_once_with(fullpath=video.fullpath)
    assert video.state == 0


@pytest.mark.parametrize("unavailable", [None, [], OSError("file manifest unavailable")])
def test_unavailable_live_manifest_rejects_before_file_deletion(cleanup_case, unavailable):
    """数据库视频记录不足以替代下载器的完整任务文件清单。"""
    case = cleanup_case
    _keep_video_history_only(case)
    if isinstance(unavailable, Exception):
        case.plugin.chain.torrent_files.side_effect = unavailable
    else:
        case.plugin.chain.torrent_files.return_value = unavailable

    with pytest.raises((ValueError, RuntimeError, OSError)):
        _cleanup(case)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("invalid_task", [
    "missing", "ambiguous", "wrong-hash", "wrong-client",
    "missing-save-path", "remote-save-path", "relative-save-path",
])
def test_manifest_requires_one_exact_local_downloader_task(cleanup_case, invalid_task):
    """任务身份或保存根目录不可靠时，不得据此解释相对清单路径。"""
    case = cleanup_case
    if invalid_task == "missing":
        case.plugin.chain.list_torrents.return_value = []
    elif invalid_task == "ambiguous":
        case.plugin.chain.list_torrents.return_value = [case.task, case.task.model_copy()]
    elif invalid_task == "wrong-hash":
        case.task.hash = "hash-b"
    elif invalid_task == "wrong-client":
        case.task.downloader = "client-b"
    else:
        case.task.save_path = {
            "missing-save-path": None,
            "remote-save-path": "u115:/downloads",
            "relative-save-path": "downloads",
        }[invalid_task]

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("missing", ["service", "config"])
def test_missing_downloader_service_configuration_rejects_cleanup(cleanup_case, missing):
    """缺少原始下载器配置时不能确认映射后的保存目录属于本地存储。"""
    case = cleanup_case
    case.plugin._downloader_helper.get_service.return_value = (
        None if missing == "service" else SimpleNamespace(config=None)
    )

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


def test_remote_storage_mapping_rejects_even_when_mapped_task_path_looks_local(cleanup_case):
    """旧宿主剥离存储前缀后，原配置仍能阻止云路径误入本地删除。"""
    case = cleanup_case
    service = case.plugin._downloader_helper.get_service.return_value
    service.config.path_mapping = [("u115:/downloads", "/downloads")]

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


def test_explicit_local_absolute_storage_mapping_allows_owned_cleanup(cleanup_case):
    """带 local 前缀的绝对本地映射仍可用于精确任务清理。"""
    case = cleanup_case
    service = case.plugin._downloader_helper.get_service.return_value
    service.config.path_mapping = [(f"local:{case.root.parent}", "/downloads")]

    assert _cleanup(case) == [HASH]

    assert not case.root.exists()
    assert case.sibling.read_bytes() == b"keep sibling"
    case.plugin._downloader_helper.get_service.assert_called_once_with(name=DOWNLOADER)
    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, downloader=DOWNLOADER, delete_file=False,
    )


@pytest.mark.parametrize("invalid_manifest", ["absolute", "parent-traversal", "outside-source", "omitted-video"])
def test_manifest_paths_must_stay_in_source_and_cover_database_video(cleanup_case, invalid_manifest):
    """清单只授予当前原盘内相对路径，且不能遗漏数据库已知文件。"""
    case = cleanup_case
    _keep_video_history_only(case)
    if invalid_manifest == "omitted-video":
        case.manifest[:] = [item for item in case.manifest if not item.name.endswith(".m2ts")]
    else:
        name = {
            "absolute": str(case.root / "extra.txt"),
            "parent-traversal": "movie/BDMV/../../outside.txt",
            "outside-source": "other-movie/BDMV/STREAM/00002.m2ts",
        }[invalid_manifest]
        case.manifest.append(schemas.DownloaderFile(name=name))

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


def test_auxiliary_file_missing_from_live_manifest_is_not_implicitly_owned(cleanup_case):
    """真实目录中不在清单内的辅助文件同样不能被原盘目录名授权。"""
    case = cleanup_case
    _keep_video_history_only(case)
    case.manifest[:] = [item for item in case.manifest if not item.name.endswith("id.bdmv")]

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


def test_auxiliary_manifest_file_with_another_active_owner_blocks_cleanup(cleanup_case):
    """下载器清单补齐的辅助文件仍需检查其他任务的活跃归属。"""
    case = cleanup_case
    _keep_video_history_only(case)
    auxiliary = str(case.root / "BDMV" / "index.bdmv")
    foreign = SimpleNamespace(
        id=99, fullpath=auxiliary, state=1, download_hash="hash-b", downloader="client-b",
    )
    case.plugin._downloadhis.get_files_by_fullpath.side_effect = lambda *, fullpath: (
        [record for record in case.records if record.fullpath == fullpath]
        + ([foreign] if fullpath == auxiliary else [])
    )

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)
    assert foreign.state == 1


@pytest.mark.parametrize("missing", ["whole-source", "bdmv-only"])
def test_recorded_bluray_can_retry_after_partial_filesystem_cleanup(cleanup_case, missing):
    """下载记录证明原盘身份时，已删的 BDMV 不阻止剩余文件清理。"""
    case = cleanup_case
    shutil.rmtree(case.root if missing == "whole-source" else case.root / "BDMV")

    assert _cleanup(case) == [HASH]

    assert not case.root.exists()
    assert all(record.state == 0 for record in case.records)
    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, delete_file=False, downloader=DOWNLOADER,
    )


def test_unrecorded_file_inside_bluray_rejects_entire_cleanup(cleanup_case):
    """原盘目录中的无归属文件必须保留，不能用递归删除吞掉。"""
    case = cleanup_case
    unowned = case.root / "notes.txt"
    unowned.write_bytes(b"keep unrecorded")

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    assert unowned.read_bytes() == b"keep unrecorded"
    _assert_no_cleanup(case)


def test_torrent_record_outside_source_rejects_before_any_deletion(cleanup_case):
    """同种子跨目录记录不能授权删除当前目录。"""
    case = cleanup_case
    case.records.append(SimpleNamespace(
        id=9, fullpath=str(case.sibling), downloader=DOWNLOADER,
        download_hash=HASH, state=1,
    ))

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("missing", ["hash", "records", "history", "downloader"])
def test_missing_ownership_evidence_rejects_cleanup(cleanup_case, missing):
    """种子、文件清单、下载历史及下载器都是删除的必要凭据。"""
    case = cleanup_case
    if missing == "records":
        case.plugin._downloadhis.get_files_by_hash.return_value = []
    elif missing == "history":
        case.plugin._downloadhis.get_by_hash.return_value = None
    elif missing == "downloader":
        case.history.downloader = None

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case, torrent_hash="" if missing == "hash" else HASH)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("conflict", ["file-downloader", "history-downloader", "expected-downloader"])
def test_conflicting_downloaders_reject_before_filesystem_effects(cleanup_case, conflict):
    """文件、下载历史和整理历史必须指向同一个下载器。"""
    case = cleanup_case
    expected = DOWNLOADER
    if conflict == "file-downloader":
        case.records[0].downloader = "client-b"
    elif conflict == "history-downloader":
        case.history.downloader = "client-b"
    else:
        expected = "client-b"

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case, downloader=expected)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("foreign_owner", ["different-hash", "different-downloader"])
def test_another_active_owner_of_a_recorded_path_rejects_cleanup(cleanup_case, foreign_owner):
    """即使当前任务清单一致，同一路径的其他活跃任务也必须受保护。"""
    case = cleanup_case
    shared_path = case.records[0].fullpath
    foreign = SimpleNamespace(
        id=99, fullpath=shared_path, state=1,
        download_hash="hash-b" if foreign_owner == "different-hash" else HASH,
        downloader="client-b" if foreign_owner == "different-downloader" else DOWNLOADER,
    )
    case.plugin._downloadhis.get_files_by_fullpath.side_effect = lambda *, fullpath: (
        [record for record in case.records if record.fullpath == fullpath]
        + ([foreign] if fullpath == shared_path else [])
    )

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    _assert_no_cleanup(case)


def test_inactive_foreign_file_record_does_not_block_owned_cleanup(cleanup_case):
    """其他任务已经删除的记录不再持有本地文件。"""
    case = cleanup_case
    shared_path = case.records[0].fullpath
    foreign = SimpleNamespace(
        id=99, fullpath=shared_path, state=0,
        download_hash="hash-b", downloader="client-b",
    )
    case.plugin._downloadhis.get_files_by_fullpath.side_effect = lambda *, fullpath: (
        [record for record in case.records if record.fullpath == fullpath]
        + ([foreign] if fullpath == shared_path else [])
    )

    assert _cleanup(case) == [HASH]

    assert not case.root.exists()
    assert foreign.state == 0


@pytest.mark.parametrize("position", ["root", "ancestor", "descendant"])
def test_symlinks_anywhere_in_source_path_reject_cleanup(cleanup_case, position):
    """符号链接不能把本地删除范围重定向到其他目录。"""
    case = cleanup_case
    source = str(case.root)
    if position == "root":
        alias = case.tmp_path / "movie-link"
        alias.symlink_to(case.root, target_is_directory=True)
        source = str(alias)
    elif position == "ancestor":
        alias = case.tmp_path / "downloads-link"
        alias.symlink_to(case.root.parent, target_is_directory=True)
        source = str(alias / case.root.name)
    else:
        (case.root / "keep-link").symlink_to(case.sibling)

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case, src=source)

    _assert_no_cleanup(case)


@pytest.mark.parametrize("plugin_id", ["TorrentTransfer", "IYUUAutoSeed"])
def test_known_cross_seeding_links_reject_before_cleanup(cleanup_case, plugin_id):
    """未知范围的转种或辅种关联必须交给人工处理。"""
    case = cleanup_case
    expected_key = f"{DOWNLOADER}-{HASH}" if plugin_id == "TorrentTransfer" else HASH
    payload = (
        {"to_download": "client-b", "to_download_id": "hash-b", "delete_source": False}
        if plugin_id == "TorrentTransfer"
        else [{"downloader": "client-b", "torrents": ["hash-b"]}]
    )

    def get_data(key=None, plugin_id=None):
        return payload if key == expected_key and plugin_id == plugin_id_under_test else []

    plugin_id_under_test = plugin_id
    case.plugin.get_data.side_effect = get_data

    with pytest.raises((ValueError, RuntimeError)):
        _cleanup(case)

    assert call(key=expected_key, plugin_id=plugin_id) in case.plugin.get_data.call_args_list
    _assert_no_cleanup(case)
    case.plugin.del_data.assert_not_called()


@pytest.mark.parametrize("outcome", [False, None, OSError("downloader unavailable")])
def test_downloader_failure_preserves_all_retry_records_in_sync(cleanup_case, outcome):
    """跨系统第二步失败时保留整理与下载记录，随后可安全重试。"""
    case = cleanup_case
    case.plugin.chain.remove_torrents.side_effect = outcome if isinstance(outcome, Exception) else None
    case.plugin.chain.remove_torrents.return_value = outcome

    _sync(case)

    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, delete_file=False, downloader=DOWNLOADER,
    )
    case.plugin._transferhis.delete.assert_not_called()
    case.plugin._downloadhis.delete_file_by_fullpath.assert_not_called()
    assert all(record.state == 1 for record in case.records)
    assert case.sibling.read_bytes() == b"keep sibling"


def test_sync_removes_transfer_history_only_after_confirmed_cleanup(cleanup_case):
    """整理历史最后提交，源文件、下载器和文件记录必须先完成。"""
    case = cleanup_case

    _sync(case)

    assert not case.root.exists()
    case.plugin.chain.remove_torrents.assert_called_once_with(
        hashs=HASH, delete_file=False, downloader=DOWNLOADER,
    )
    assert all(record.state == 0 for record in case.records)
    case.plugin._transferhis.delete.assert_called_once_with(case.transfer.id)
    assert case.events[-1] == "transfer-history"


def test_new_unrecorded_file_during_cleanup_survives_and_preserves_retry_history(
    cleanup_case, monkeypatch,
):
    """预检之后新出现的文件不能被递归删除，未完成清理保留重试依据。"""
    case = cleanup_case
    unowned = case.root / "new-arrival.txt"
    real_unlink = Path.unlink

    def unlink_with_new_file(path, *args, **kwargs):
        if str(path) in {record.fullpath for record in case.records} and not unowned.exists():
            unowned.write_bytes(b"arrived after preflight")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_new_file)

    _sync(case)

    assert unowned.read_bytes() == b"arrived after preflight"
    assert case.sibling.read_bytes() == b"keep sibling"
    case.plugin.chain.remove_torrents.assert_not_called()
    case.plugin._downloadhis.delete_file_by_fullpath.assert_not_called()
    case.plugin._transferhis.delete.assert_not_called()
    assert all(record.state == 1 for record in case.records)


def test_nonlocal_source_cannot_be_treated_as_a_local_bluray(cleanup_case):
    """云存储记录中的路径不能授权删除同名本地文件。"""
    case = cleanup_case
    case.transfer.src_storage = "u115"

    _sync(case)

    _assert_no_cleanup(case)
    case.plugin._transferhis.delete.assert_not_called()
