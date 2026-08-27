from __future__ import annotations

import ast
import json
import os
import sys
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
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins import syncdownloadfiles as syncdownloadfiles_module
from app.plugins.syncdownloadfiles import SyncDownloadFiles
from app.runtime.extensions.plugin.projection import PluginProjection

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "syncdownloadfiles" / "__init__.py"

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
            "transfer_execution",
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复未配置状态。"""
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
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退和宿主 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SyncDownloadFiles"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["SyncDownloadFiles"]

    assert manifest["version"] == SyncDownloadFiles.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.db.oper.downloadhistory",
        "app.db.oper.transferhistory",
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.services",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db.downloadhistory_oper",
        "app.db.transferhistory_oper",
        "app.db.models",
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
    assert "response_model" not in source
    assert "db_query" not in source


def test_plugin_lifecycle_returns_explicit_empty_capabilities(monkeypatch) -> None:
    """没有命令、API 和详情页时，V3 生命周期接口应返回空列表。"""
    downloader_helper = Mock()
    downloader_helper.get_configs.return_value = {}
    monkeypatch.setattr(
        syncdownloadfiles_module, "DownloaderHelper", lambda: downloader_helper
    )
    monkeypatch.setattr(syncdownloadfiles_module, "DownloadHistoryOper", Mock)
    monkeypatch.setattr(syncdownloadfiles_module, "TransferHistoryOper", Mock)

    plugin = SyncDownloadFiles()
    plugin.init_plugin({"enabled": True, "time": "2"})

    assert plugin.get_state() is True
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_page() == []
    assert plugin.get_service() == [
        {
            "id": "SyncDownloadFiles",
            "name": "同步下载器文件记录服务",
            "trigger": "interval",
            "func": plugin.sync,
            "kwargs": {"seconds": 7200.0},
        }
    ]
    assert not hasattr(plugin, "downloadhis")
    assert not hasattr(plugin, "transferhis")
    assert plugin.stop_service() is None


def test_onlyonce_registers_host_managed_date_service(monkeypatch) -> None:
    """立即运行只注册宿主任务，不得在初始化线程访问下载器。"""
    downloader_helper = Mock()
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloaderHelper",
        lambda: downloader_helper,
    )

    plugin = SyncDownloadFiles()
    plugin._SyncDownloadFiles__sync = Mock()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin({"onlyonce": True, "downloaders": ["qb"]})

    plugin._SyncDownloadFiles__sync.assert_not_called()
    downloader_helper.get_service.assert_not_called()
    assert plugin.get_state() is True
    services = PluginProjection(
        {"SyncDownloadFiles": plugin},
        log=Mock(),
    ).services()
    assert len(services) == 1
    assert services[0]["id"] == "SyncDownloadFiles.Once"
    assert services[0]["trigger"] == "date"
    assert "run_date" in services[0]["kwargs"]
    plugin.update_config.assert_called_once()

    services[0]["func"]()

    plugin._SyncDownloadFiles__sync.assert_called_once_with()
    assert plugin.get_state() is False
    assert PluginProjection(
        {"SyncDownloadFiles": plugin},
        log=Mock(),
    ).services() == []


def test_invalid_interval_disables_only_periodic_service(monkeypatch) -> None:
    """非法时间间隔应失败关闭周期服务，同时保留一次性触发能力。"""
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloaderHelper",
        Mock,
    )
    plugin = SyncDownloadFiles()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin({
        "enabled": True,
        "onlyonce": True,
        "time": "invalid",
        "downloaders": ["qb"],
    })

    assert plugin.get_state() is True
    services = plugin.get_service()
    assert [service["id"] for service in services] == ["SyncDownloadFiles.Once"]


def test_service_infos_filters_inactive_downloaders() -> None:
    """下载器服务目录应复用 SDK Helper，并排除未连接实例。"""
    active_instance = Mock()
    active_instance.is_inactive.return_value = False
    inactive_instance = Mock()
    inactive_instance.is_inactive.return_value = True
    active_info = SimpleNamespace(
        instance=active_instance,
        config=SimpleNamespace(type="transmission"),
    )
    inactive_info = SimpleNamespace(
        instance=inactive_instance,
        config=SimpleNamespace(type="qbittorrent"),
    )
    helper = Mock()
    helper.get_services.return_value = {
        "active": active_info,
        "inactive": inactive_info,
    }

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["active", "inactive"]

    assert plugin.service_infos == {"active": active_info}
    helper.get_services.assert_called_once_with(
        name_filters=["active", "inactive"]
    )
    inactive_instance.is_inactive.assert_called_once_with()


def test_sync_skips_unavailable_downloaders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已选下载器消失或断开时，同步任务应跳过而不是中断整个服务。"""
    helper = Mock()
    helper.get_service.return_value = None
    download_history = Mock()
    transfer_history = Mock()
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "TransferHistoryOper",
        lambda: transfer_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["missing"]
    plugin.get_data = Mock(return_value=None)
    plugin.save_data = Mock()

    plugin.sync()

    download_history.get_files_by_hash.assert_not_called()
    plugin.save_data.assert_not_called()


def test_sync_qbittorrent_registers_oldest_duplicate_and_backfills_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qBittorrent 应保留同名同大小任务中最早的源种子并登记视频文件。"""
    newest = {
        "added_on": 200,
        "size": 100,
        "name": "Example.Show",
        "hash": "new-hash",
        "save_path": "/downloads",
    }
    oldest = {
        "added_on": 100,
        "size": 100,
        "name": "Example.Show",
        "hash": "old-hash",
        "save_path": "/downloads",
    }
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [newest, oldest]
    downloader.get_files.return_value = [
        {
            "name": "Example.Show/episode.mkv",
            "priority": 1,
            "progress": 1,
        },
        {
            "name": "Example.Show/readme.txt",
            "priority": 1,
            "progress": 1,
        },
    ]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="qbittorrent"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []
    transfer_history = Mock()
    transfer_history.get_by_src.return_value = SimpleNamespace(
        id=7,
        download_hash=None,
    )

    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "TransferHistoryOper",
        lambda: transfer_history,
    )
    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["qb"]
    plugin._history = True
    plugin._dirs = "/downloads:/mapped"
    plugin.get_data = Mock(side_effect=AssertionError("旧同步时间不得参与过滤"))
    plugin.save_data = Mock()

    plugin.sync()

    download_history.get_files_by_hash.assert_called_once_with("old-hash")
    downloader.get_files.assert_called_once_with(tid="old-hash")
    transfer_history.get_by_src.assert_called_once_with(
        "/mapped/Example.Show/episode.mkv"
    )
    transfer_history.update_download_hash.assert_called_once_with(
        historyid=7,
        download_hash="old-hash",
    )
    download_history.add_files.assert_called_once_with(
        [
            {
                "download_hash": "old-hash",
                "downloader": "qb",
                "fullpath": "/mapped/Example.Show/episode.mkv",
                "savepath": "/mapped/Example.Show",
                "filepath": "episode.mkv",
                "torrentname": "Example.Show",
                "state": 1,
            }
        ]
    )
    plugin.save_data.assert_called_once()
    assert plugin.save_data.call_args.args[0] == "last_sync_time_qb"


@pytest.mark.parametrize(
    ("downloader_type", "torrent", "torrent_file", "torrent_hash", "torrent_id"),
    [
        (
            "qbittorrent",
            {
                "added_on": 100,
                "size": 100,
                "name": "movie.mkv",
                "hash": "qb-single-hash",
                "save_path": "/downloads",
            },
            {"name": "movie.mkv", "priority": 1, "progress": 1},
            "qb-single-hash",
            "qb-single-hash",
        ),
        (
            "transmission",
            SimpleNamespace(
                added_date="2026-08-28 00:00:00",
                total_size=100,
                name="movie.mkv",
                hashString="tr-single-hash",
                download_dir="/downloads",
                id=42,
            ),
            SimpleNamespace(
                completed=100,
                name="movie.mkv",
                selected=True,
                size=100,
            ),
            "tr-single-hash",
            42,
        ),
    ],
)
def test_sync_single_file_uses_download_directory_as_savepath(
    monkeypatch: pytest.MonkeyPatch,
    downloader_type: str,
    torrent: object,
    torrent_file: object,
    torrent_hash: str,
    torrent_id: object,
) -> None:
    """单文件任务应保存下载目录和文件名，不能把文件本身当作目录。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = [torrent_file]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type=downloader_type),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["primary"]
    plugin.save_data = Mock()

    plugin.sync()

    downloader.get_files.assert_called_once_with(tid=torrent_id)
    download_history.add_files.assert_called_once_with(
        [
            {
                "download_hash": torrent_hash,
                "downloader": "primary",
                "fullpath": "/downloads/movie.mkv",
                "savepath": "/downloads",
                "filepath": "movie.mkv",
                "torrentname": "movie.mkv",
                "state": 1,
            }
        ]
    )


@pytest.mark.parametrize(
    ("raw_mapping", "download_dir", "expected"),
    [
        ("/downloads:/mapped", "/downloads/show", "/mapped/show"),
        (r"C:\downloads:D:\mapped", r"C:\downloads\show", "D:/mapped/show"),
        (r"/downloads:D:\mapped", "/downloads/show", "D:/mapped/show"),
        ("/downloads:/mapped", "/downloads-extra/show", "/downloads-extra/show"),
    ],
)
def test_path_mapping_supports_explicit_platform_boundaries(
    raw_mapping: str,
    download_dir: str,
    expected: str,
) -> None:
    """目录映射应支持常见平台路径，且只替换完整目录段前缀。"""
    plugin = SyncDownloadFiles()
    plugin._dirs = raw_mapping

    mappings = plugin._SyncDownloadFiles__parse_path_mappings()

    assert mappings is not None
    assert plugin._SyncDownloadFiles__map_download_dir(
        download_dir,
        mappings,
    ) == expected


def test_sync_transmission_filters_incomplete_and_non_video_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transmission 应按文件完成度过滤，并只登记媒体后缀文件。"""
    torrent = SimpleNamespace(
        added_date="2026-08-28 00:00:00",
        total_size=100,
        name="Example.Show",
        hashString="tr-hash",
        download_dir="/downloads",
        id=42,
    )
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = [
        SimpleNamespace(
            completed=100,
            name="Example.Show/episode.mkv",
            selected=True,
            size=100,
        ),
        SimpleNamespace(
            completed=50,
            name="Example.Show/incomplete.mkv",
            selected=True,
            size=100,
        ),
        SimpleNamespace(
            completed=10,
            name="Example.Show/poster.jpg",
            selected=True,
            size=10,
        ),
    ]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="transmission"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []

    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )
    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["transmission"]
    plugin._dirs = "/downloads:/mapped"
    plugin.get_data = Mock(return_value=None)
    plugin.save_data = Mock()

    plugin.sync()

    downloader.get_files.assert_called_once_with(tid=42)
    download_history.add_files.assert_called_once_with(
        [
            {
                "download_hash": "tr-hash",
                "downloader": "transmission",
                "fullpath": "/mapped/Example.Show/episode.mkv",
                "savepath": "/mapped/Example.Show",
                "filepath": "episode.mkv",
                "torrentname": "Example.Show",
                "state": 1,
            }
        ]
    )


def test_sync_keeps_checkpoint_when_file_state_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动态文件对象缺少完成度时不得登记记录或推进同步游标。"""
    torrent = {
        "added_on": 100,
        "size": 100,
        "name": "Example.Show",
        "hash": "hash-with-unknown-file",
        "save_path": "/downloads",
    }
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = [
        {"name": "Example.Show/episode.mkv"},
    ]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="qbittorrent"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["qb"]
    plugin.get_data = Mock(return_value=None)
    plugin.save_data = Mock()

    plugin.sync()

    download_history.add_files.assert_not_called()
    plugin.save_data.assert_not_called()


@pytest.mark.parametrize(
    "mapping",
    [
        "source:ambiguous:target",
        r"C:\downloads",
        "D:/media",
    ],
)
def test_sync_rejects_malformed_path_mapping_before_accessing_database(
    monkeypatch: pytest.MonkeyPatch,
    mapping: str,
) -> None:
    """目录映射格式不明确时不得产生任何数据库或下载器副作用。"""
    download_history_factory = Mock()
    transfer_history_factory = Mock()
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        download_history_factory,
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "TransferHistoryOper",
        transfer_history_factory,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = Mock()
    plugin._downloaders = ["qb"]
    plugin._dirs = mapping
    plugin.save_data = Mock()

    plugin.sync()

    download_history_factory.assert_not_called()
    transfer_history_factory.assert_not_called()
    plugin.downloader_helper.get_service.assert_not_called()
    plugin.save_data.assert_not_called()


def test_sync_does_not_write_partial_torrent_on_unknown_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一任务任一文件状态未知时，已验证文件也不得提前写库。"""
    torrent = {
        "added_on": 100,
        "size": 200,
        "name": "Example.Show",
        "hash": "mixed-files-hash",
        "save_path": "/downloads",
    }
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = [
        {
            "name": "Example.Show/episode-1.mkv",
            "priority": 1,
            "progress": 1,
        },
        {"name": "Example.Show/episode-2.mkv"},
    ]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="qbittorrent"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["qb"]
    plugin.save_data = Mock()

    plugin.sync()

    download_history.add_files.assert_not_called()
    plugin.save_data.assert_not_called()


def test_sync_completes_missing_rows_after_partial_database_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有部分文件行时应只补缺项，不能把整个 hash 误判为完成。"""
    torrent = {
        "added_on": 100,
        "size": 200,
        "name": "Example.Show",
        "hash": "partial-hash",
        "save_path": "/downloads",
    }
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = [
        {
            "name": "Example.Show/episode-1.mkv",
            "priority": 1,
            "progress": 1,
        },
        {
            "name": "Example.Show/episode-2.mkv",
            "priority": 1,
            "progress": 1,
        },
    ]
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="qbittorrent"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = [
        SimpleNamespace(fullpath="/downloads/Example.Show/episode-1.mkv")
    ]
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "settings",
        SimpleNamespace(RMT_MEDIAEXT=[".mkv"]),
    )
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["qb"]
    plugin.save_data = Mock()

    plugin.sync()

    download_history.add_files.assert_called_once_with(
        [
            {
                "download_hash": "partial-hash",
                "downloader": "qb",
                "fullpath": "/downloads/Example.Show/episode-2.mkv",
                "savepath": "/downloads/Example.Show",
                "filepath": "episode-2.mkv",
                "torrentname": "Example.Show",
                "state": 1,
            }
        ]
    )


def test_sync_skips_overlapping_trigger_before_creating_opers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """date 与 interval 重叠时，后到的普通触发不得并发访问数据库。"""
    download_history_factory = Mock()
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        download_history_factory,
    )
    plugin = SyncDownloadFiles()
    plugin._downloaders = ["qb"]
    plugin._sync_lock.acquire()
    try:
        plugin.sync()
    finally:
        plugin._sync_lock.release()

    download_history_factory.assert_not_called()


def test_sync_keeps_checkpoint_when_file_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载器返回未知文件清单时应保留游标，让任务在下轮重试。"""
    torrent = SimpleNamespace(
        added_date="2026-08-28 00:00:00",
        total_size=100,
        name="Example.Show",
        hashString="tr-hash",
        download_dir="/downloads",
        id=42,
    )
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [torrent]
    downloader.get_files.return_value = None
    helper = Mock()
    helper.get_service.return_value = SimpleNamespace(
        instance=downloader,
        config=SimpleNamespace(type="transmission"),
    )
    download_history = Mock()
    download_history.get_files_by_hash.return_value = []
    monkeypatch.setattr(
        syncdownloadfiles_module,
        "DownloadHistoryOper",
        lambda: download_history,
    )

    plugin = SyncDownloadFiles()
    plugin.downloader_helper = helper
    plugin._downloaders = ["transmission"]
    plugin.get_data = Mock(return_value=None)
    plugin.save_data = Mock()

    plugin.sync()

    download_history.add_files.assert_not_called()
    plugin.save_data.assert_not_called()
