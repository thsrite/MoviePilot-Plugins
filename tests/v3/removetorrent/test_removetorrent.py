from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import removetorrent as removetorrent_module
from app.plugins.removetorrent import RemoveTorrent


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "removetorrent" / "__init__.py"

def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _service(name: str, service_type: str, instance: Mock):
    """构造 DownloaderHelper 返回的配置名、类型和实例投影。"""
    return SimpleNamespace(name=name, type=service_type, instance=instance)


def _qb_torrent(name: str, size: int, torrent_hash: str, tracker: str) -> dict:
    return {
        "name": name,
        "size": size,
        "hash": torrent_hash,
        "tracker": tracker,
    }


def _tr_torrent(name: str, size: int, torrent_hash: str, trackers: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        total_size=size,
        hashString=torrent_hash,
        trackers=trackers,
    )


def _configure_plugin(monkeypatch, service, **config) -> RemoveTorrent:
    helper = Mock()
    helper.get_service.return_value = service
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin(
        {
            "downloader": service.name,
            "delete_type": False,
            "delete_torrent": True,
            "delete_file": False,
            "trackers": "tracker.example",
            "onlyonce": False,
            **config,
        }
    )
    return plugin


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["RemoveTorrent"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["RemoveTorrent"]

    assert manifest["version"] == RemoveTorrent.plugin_version == "2.0.0"
    assert manifest["level"] == RemoveTorrent.auth_level == 1
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {"app.sdk.logging", "app.sdk.services"}.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.modules",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "app.log" not in source
    assert "logger.warn(" not in source


def test_form_uses_downloader_configuration_names(monkeypatch) -> None:
    """配置表单应展示服务名称，而不是把下载器类型当作服务标识。"""
    helper = Mock()
    helper.get_configs.return_value = {
        "Main QB": SimpleNamespace(name="Main QB", type="qbittorrent"),
        "Main TR": SimpleNamespace(name="Main TR", type="transmission"),
        "Unsupported": SimpleNamespace(name="Unsupported", type="rtorrent"),
    }
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)

    form, defaults = RemoveTorrent().get_form()

    assert defaults["downloader"] == ""
    select = next(
        item
        for item in form[0]["content"][1]["content"]
        if item["content"][0]["component"] == "VSelect"
        and item["content"][0]["props"]["model"] == "downloader"
    )
    assert select["content"][0]["props"]["items"] == [
        {"title": "Main QB", "value": "Main QB"},
        {"title": "Main TR", "value": "Main TR"},
    ]


def test_unique_legacy_downloader_alias_is_migrated_before_run(monkeypatch) -> None:
    """旧版 qb/tr 配置仅在目标类型唯一时迁移为服务配置名。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = []
    helper = Mock()
    helper.get_configs.return_value = {
        "QB 主下载器": SimpleNamespace(name="QB 主下载器", type="qbittorrent")
    }
    helper.get_service.return_value = _service(
        "QB 主下载器", "qbittorrent", downloader
    )
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin(
        {
            "downloader": "qb",
            "delete_type": False,
            "delete_torrent": True,
            "delete_file": False,
            "trackers": "tracker.example",
            "onlyonce": True,
        }
    )

    helper.get_service.assert_called_once_with("QB 主下载器")
    assert plugin.update_config.call_args.args[0]["downloader"] == "QB 主下载器"


@pytest.mark.parametrize("alias_name", ["legacy", "qb"])
def test_ambiguous_legacy_downloader_alias_fails_closed(monkeypatch, alias_name) -> None:
    """同类型存在多个服务时不得猜测旧版 qb/tr 配置的目标实例。"""
    helper = Mock()
    helper.get_configs.return_value = {
        alias_name: SimpleNamespace(name=alias_name, type="qbittorrent"),
        "QB 二": SimpleNamespace(name="QB 二", type="qbittorrent"),
    }
    helper.get_service.return_value = _service(
        alias_name, "qbittorrent", Mock()
    )
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin(
        {
            "downloader": "qb",
            "delete_type": False,
            "delete_torrent": True,
            "delete_file": False,
            "trackers": "tracker.example",
            "onlyonce": True,
        }
    )

    helper.get_service.assert_not_called()
    assert plugin.update_config.call_args.args[0]["downloader"] == "qb"


def test_legacy_downloader_directory_failure_fails_closed(monkeypatch) -> None:
    """旧别名无法读取服务目录时不得退回同名实例。"""
    helper = Mock()
    helper.get_configs.side_effect = RuntimeError("directory unavailable")
    helper.get_service.return_value = _service("qb", "qbittorrent", Mock())
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin(
        {
            "downloader": "qb",
            "delete_type": False,
            "delete_torrent": True,
            "delete_file": False,
            "trackers": "tracker.example",
            "onlyonce": True,
        }
    )

    helper.get_service.assert_not_called()
    assert plugin.update_config.call_args.args[0]["downloader"] == "qb"


def test_qb_dry_run_deletes_only_without_auxiliary_seed(monkeypatch) -> None:
    """qb dry-run 保持无辅种判定，并且不得调用删除接口。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [
        _qb_torrent("movie", 100, "origin", "https://tracker.example/announce"),
        _qb_torrent("movie", 100, "aux", "https://other.example/announce"),
        _qb_torrent("solo", 200, "solo", "https://tracker.example/announce"),
    ]
    plugin = _configure_plugin(
        monkeypatch,
        _service("QB 主下载器", "qbittorrent", downloader),
        delete_torrent=False,
    )

    plugin._RemoveTorrent__check_feed("tracker.example")

    downloader.delete_torrents.assert_not_called()


def test_qb_delete_with_auxiliary_seed_passes_file_flag(monkeypatch) -> None:
    """qb 删除模式应只删除命中且存在辅种的种子，并传递删除文件开关。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [
        _qb_torrent("movie", 100, "origin", "https://tracker.example/announce"),
        _qb_torrent("movie", 100, "aux", "https://other.example/announce"),
        _qb_torrent("solo", 200, "solo", "https://tracker.example/announce"),
    ]
    plugin = _configure_plugin(
        monkeypatch,
        _service("QB 主下载器", "qbittorrent", downloader),
        delete_type=True,
        delete_file=True,
    )

    plugin._RemoveTorrent__check_feed("tracker.example")

    downloader.delete_torrents.assert_called_once_with(delete_file=True, ids="origin")


def test_transmission_delete_without_auxiliary_seed_uses_current_shape(monkeypatch) -> None:
    """Transmission 应读取当前对象的 total_size/hashString/trackers 形态。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [
        _tr_torrent(
            "movie",
            100,
            "origin",
            [{"announce": "https://tracker.example/announce"}],
        ),
        _tr_torrent(
            "movie",
            100,
            "aux",
            [{"announce": "https://other.example/announce"}],
        ),
        _tr_torrent(
            "solo",
            200,
            "solo",
            [{"announce": "https://tracker.example/announce"}],
        ),
    ]
    plugin = _configure_plugin(
        monkeypatch,
        _service("TR 主下载器", "transmission", downloader),
    )

    plugin._RemoveTorrent__check_feed("tracker.example")

    downloader.delete_torrents.assert_called_once_with(delete_file=False, ids="solo")


def test_transmission_tracker_list_strings_are_supported(monkeypatch) -> None:
    """Transmission 的 tracker_list 字符串列表也应参与站点匹配。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [
        SimpleNamespace(
            name="solo",
            total_size=200,
            hashString="solo",
            tracker_list=["https://tracker.example/announce"],
        )
    ]
    plugin = _configure_plugin(
        monkeypatch,
        _service("TR 主下载器", "transmission", downloader),
    )

    plugin._RemoveTorrent__check_feed("tracker.example")

    downloader.delete_torrents.assert_called_once_with(delete_file=False, ids="solo")


@pytest.mark.parametrize(
    "service",
    [
        None,
        SimpleNamespace(name="unknown", type="rtorrent", instance=Mock()),
    ],
)
def test_missing_or_unknown_service_fails_closed(monkeypatch, service) -> None:
    """缺失服务和未知下载器类型均不得查询或删除。"""
    helper = Mock()
    helper.get_service.return_value = service
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    plugin._downloader = "missing"
    plugin._trackers = "tracker.example"
    plugin._delete_torrent = True

    plugin._RemoveTorrent__check_feed("tracker.example")

    if service is not None:
        service.instance.get_completed_torrents.assert_not_called()


def test_inactive_or_query_failure_fails_closed(monkeypatch) -> None:
    """未连接下载器和查询异常都不得进入删除分支。"""
    for query_result in (None, RuntimeError("query failed")):
        downloader = Mock()
        downloader.is_inactive.return_value = query_result is None
        if isinstance(query_result, Exception):
            downloader.get_completed_torrents.side_effect = query_result
        else:
            downloader.get_completed_torrents.return_value = query_result
        plugin = _configure_plugin(
            monkeypatch,
            _service("QB 主下载器", "qbittorrent", downloader),
        )

        plugin._RemoveTorrent__check_feed("tracker.example")

        downloader.delete_torrents.assert_not_called()


@pytest.mark.parametrize("delete_result", [False, RuntimeError("delete failed")])
def test_delete_failure_does_not_report_success(monkeypatch, delete_result) -> None:
    """删除返回失败或抛异常时均保持失败关闭。"""
    plugin_logger = Mock()
    monkeypatch.setattr(removetorrent_module, "logger", plugin_logger)
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = [
        _qb_torrent("solo", 200, "solo", "https://tracker.example/announce"),
    ]
    if isinstance(delete_result, Exception):
        downloader.delete_torrents.side_effect = delete_result
    else:
        downloader.delete_torrents.return_value = delete_result
    plugin = _configure_plugin(
        monkeypatch,
        _service("QB 主下载器", "qbittorrent", downloader),
    )

    plugin._RemoveTorrent__check_feed("tracker.example")

    downloader.delete_torrents.assert_called_once_with(delete_file=False, ids="solo")
    assert not any(
        "已删除" in str(log_call.args[0])
        for log_call in plugin_logger.info.call_args_list
    )


def test_onlyonce_is_saved_before_query(monkeypatch) -> None:
    """一次性开关必须在任何查询或删除前持久化为关闭状态。"""
    downloader = Mock()
    downloader.is_inactive.return_value = False
    downloader.get_completed_torrents.return_value = []
    helper = Mock()
    helper.get_service.return_value = _service("QB 主下载器", "qbittorrent", downloader)
    monkeypatch.setattr(removetorrent_module, "DownloaderHelper", lambda: helper)
    plugin = RemoveTorrent()
    order = []
    plugin.update_config = Mock(side_effect=lambda config: order.append(("save", config)) or True)
    original_query = downloader.get_completed_torrents
    downloader.get_completed_torrents = Mock(
        side_effect=lambda: order.append(("query", None)) or original_query()
    )

    plugin.init_plugin(
        {
            "downloader": "QB 主下载器",
            "delete_type": False,
            "delete_torrent": True,
            "delete_file": False,
            "trackers": "tracker.example",
            "onlyonce": True,
        }
    )

    assert order[0][0] == "save"
    assert order[0][1]["onlyonce"] is False
    assert order[1][0] == "query"
    assert plugin._onlyonce is False
