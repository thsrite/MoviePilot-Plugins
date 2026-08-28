from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import downloadtorrent as downloadtorrent_module
from app.plugins.downloadtorrent import DownloadTorrent
from app.schemas.types import EventType, SystemConfigKey
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "downloadtorrent" / "__init__.py"

def _imports() -> set[str]:
    """返回插件源码显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _service(instance: Mock, service_type: str) -> SimpleNamespace:
    """构造下载器 Helper 返回的最小服务投影。"""
    return SimpleNamespace(instance=instance, type=service_type)


def test_v3_source_imports_and_manifest_contract() -> None:
    """V3 源码、索引版本和旧代回退开关必须满足严格迁移合同。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["DownloadTorrent"]
    package_v2 = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["DownloadTorrent"]
    package_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["DownloadTorrent"]

    assert manifest["version"] == DownloadTorrent.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert package_v2["v3"] is False
    assert package_v1["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.services",
        "app.sdk.utilities",
    }.issubset(imports)
    assert "app.schemas" not in imports
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
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
    assert "app.log" not in source
    assert "DirectoryHelper" not in source
    assert "event_data.get(\"args\")" not in source
    assert "arg_str" in source


def test_real_v3_import_and_lifecycle_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件可在真实 V3 bootstrap 下导入并完成无任务初始化。"""
    helper = Mock()
    helper.get_configs.return_value = {}
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", Mock)

    plugin = DownloadTorrent()
    plugin.init_plugin({})

    assert plugin.get_state() is False
    assert plugin.get_api() == []
    assert plugin.get_page() == []
    assert plugin.stop_service() is None


def test_init_consumes_urls_before_download_and_continues_after_mixed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次性链接在成功、失败和异常混合时都应被清空并只保存稳定字段。"""
    helper = Mock()
    helper.get_configs.return_value = {}
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", Mock)

    plugin = DownloadTorrent()
    order = []
    plugin.update_config = Mock(
        side_effect=lambda config: order.append(("save", config)) or True
    )

    def record_download(url: str):
        order.append(("download", url))
        if url.endswith("/a"):
            return "站点", "成功"
        raise RuntimeError("网络失败")

    download = Mock(side_effect=record_download)
    monkeypatch.setattr(plugin, "_DownloadTorrent__download_torrent", download)

    plugin.init_plugin(
        {
            "enabled": True,
            "is_paused": True,
            "downloader": "qb-main",
            "save_path": "/custom",
            "mp_path": "/downloads",
            "torrent_urls": "https://tracker.example/a\nhttps://tracker.example/b",
        }
    )

    download.assert_has_calls([
        (("https://tracker.example/a",), {}),
        (("https://tracker.example/b",), {}),
    ])
    assert order[0][0] == "save"
    assert [item[0] for item in order] == ["save", "download", "download"]
    saved_config = plugin.update_config.call_args.args[0]
    assert saved_config == {
        "downloader": "qb-main",
        "save_path": "/custom",
        "enabled": True,
        "mp_path": "/downloads",
        "is_paused": True,
    }
    assert "torrent_urls" not in saved_config


@pytest.mark.parametrize("save_result", [False, RuntimeError("save failed")])
def test_init_does_not_download_when_trigger_consumption_fails(
    monkeypatch: pytest.MonkeyPatch,
    save_result: Any,
) -> None:
    """一次性链接无法先持久化消费时不得产生下载副作用。"""
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", Mock)
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", Mock)
    plugin = DownloadTorrent()
    if isinstance(save_result, Exception):
        plugin.update_config = Mock(side_effect=save_result)
    else:
        plugin.update_config = Mock(return_value=save_result)
    download = Mock()
    monkeypatch.setattr(plugin, "_DownloadTorrent__download_torrent", download)

    plugin.init_plugin(
        {"torrent_urls": "https://tracker.example/download?passkey=secret"}
    )

    download.assert_not_called()


def test_qbittorrent_download_uses_cookie_and_custom_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qBittorrent 分支应使用站点 Cookie、自定义路径和暂停开关。"""
    helper = Mock()
    helper.get_service.return_value = _service(
        instance := Mock(), "qbittorrent"
    )
    instance.is_inactive.return_value = False
    instance.add_torrent.return_value = (True, ["qb-hash"])
    helper.is_downloader.side_effect = lambda kind, service: kind == "qbittorrent"
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = {
        "name": "示例站",
        "cookie": "cookie-value",
    }
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)

    plugin = DownloadTorrent()
    plugin.init_plugin(
        {
            "enabled": True,
            "is_paused": True,
            "downloader": "qb-main",
            "save_path": "/custom",
        }
    )
    result = plugin._DownloadTorrent__download_torrent(
        "https://tracker.example/torrent?id=1"
    )

    assert result[0] == "示例站"
    assert "成功" in result[1]
    instance.add_torrent.assert_called_once_with(
        content="https://tracker.example/torrent?id=1",
        download_dir="/custom",
        is_paused=True,
        cookie="cookie-value",
    )


def test_qbittorrent_failure_tuple_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qBittorrent 返回失败元组时不得被当作成功。"""
    helper = Mock()
    helper.get_service.return_value = _service(
        instance := Mock(), "qbittorrent"
    )
    instance.is_inactive.return_value = False
    instance.add_torrent.return_value = (False, [])
    helper.is_downloader.side_effect = lambda kind, service: kind == "qbittorrent"
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = {
        "name": "示例站",
        "cookie": "cookie-value",
    }
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)

    plugin = DownloadTorrent()
    plugin.init_plugin({"downloader": "qb-main", "mp_path": "/downloads"})
    result = plugin._DownloadTorrent__download_torrent(
        "https://tracker.example/torrent?id=1-failed"
    )

    assert result[0] == "示例站"
    assert "失败" in result[1]


def test_transmission_download_returns_hash_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transmission 分支应从任务对象返回 hashString。"""
    helper = Mock()
    instance = Mock()
    instance.is_inactive.return_value = False
    instance.add_torrent.return_value = SimpleNamespace(hashString="tr-hash")
    helper.get_service.return_value = _service(instance, "transmission")
    helper.is_downloader.side_effect = lambda kind, service: kind == "transmission"
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = {
        "name": "示例站",
        "cookie": "cookie-value",
    }
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)

    plugin = DownloadTorrent()
    plugin.init_plugin({"downloader": "tr-main", "mp_path": "/downloads"})
    result = plugin._DownloadTorrent__download_torrent(
        "https://tracker.example/torrent?id=2"
    )

    assert result[0] == "示例站"
    assert "成功" in result[1]
    instance.add_torrent.assert_called_once_with(
        content="https://tracker.example/torrent?id=2",
        download_dir="/downloads",
        is_paused=False,
        cookie="cookie-value",
    )


@pytest.mark.parametrize(
    "site_value, service_value",
    [
        (None, None),
        ({"name": "示例站", "cookie": None}, None),
    ],
)
def test_download_failure_paths_return_without_downloader_call(
    monkeypatch: pytest.MonkeyPatch,
    site_value: Any,
    service_value: Any,
) -> None:
    """域名、站点 Cookie 或服务缺失时必须安全失败。"""
    helper = Mock()
    helper.get_service.return_value = service_value
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = site_value
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)

    plugin = DownloadTorrent()
    plugin.init_plugin({"downloader": "missing"})
    result = plugin._DownloadTorrent__download_torrent(
        "https://tracker.example/torrent?id=3"
    )

    assert result == (None, None)
    helper.get_service.assert_not_called()


def test_download_logs_do_not_expose_torrent_url_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败日志不得包含种子 URL 的路径、查询参数或 passkey。"""
    plugin_logger = Mock()
    monkeypatch.setattr(downloadtorrent_module, "logger", plugin_logger)
    helper = Mock()
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = None
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)
    plugin = DownloadTorrent()
    plugin.init_plugin({"downloader": "missing"})

    plugin._DownloadTorrent__download_torrent(
        "https://tracker.example/download?id=1&passkey=super-secret"
    )

    log_output = str(plugin_logger.mock_calls)
    assert "super-secret" not in log_output
    assert "/download" not in log_output


def test_downloader_exception_is_reported_as_site_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载器异常不得冒泡到命令分发，并保留站点上下文。"""
    helper = Mock()
    instance = Mock()
    instance.is_inactive.return_value = False
    instance.add_torrent.side_effect = RuntimeError("connection lost")
    helper.get_service.return_value = _service(instance, "qbittorrent")
    helper.is_downloader.side_effect = lambda kind, service: kind == "qbittorrent"
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)
    sites_helper = Mock()
    sites_helper.get_indexer.return_value = {
        "name": "示例站",
        "cookie": "cookie",
    }
    monkeypatch.setattr(downloadtorrent_module, "SitesHelper", lambda: sites_helper)

    plugin = DownloadTorrent()
    plugin.init_plugin({"downloader": "qb-main"})
    result = plugin._DownloadTorrent__download_torrent("https://tracker.example/torrent?id=4")

    assert result[0] == "示例站"
    assert "失败" in result[1]


def test_remote_command_uses_v3_arg_str_and_posts_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """远程命令必须读取 `arg_str` 并把结果发回原渠道。"""
    plugin = DownloadTorrent()
    plugin._DownloadTorrent__download_torrent = Mock(return_value=("示例站", "种子添加下载成功"))
    plugin.post_message = Mock()

    plugin.remote_sync_one(
        Event(
            EventType.PluginAction,
            {
                "action": "download_torrent",
                "arg_str": "https://tracker.example/torrent?id=5",
                "channel": "telegram",
                "user": "user-1",
            },
        )
    )

    plugin._DownloadTorrent__download_torrent.assert_called_once_with(
        "https://tracker.example/torrent?id=5"
    )
    plugin.post_message.assert_called_once_with(
        channel="telegram",
        title="示例站 种子添加下载成功",
        userid="user-1",
    )


def test_get_form_projects_local_download_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置表单应从系统配置投影本地目录，不导入内部 DirectoryHelper。"""
    helper = Mock()
    helper.get_configs.return_value = {
        "qb-main": SimpleNamespace(name="qb-main", type="qbittorrent"),
        "rt-main": SimpleNamespace(name="rt-main", type="rtorrent"),
    }
    monkeypatch.setattr(downloadtorrent_module, "DownloaderHelper", lambda: helper)

    plugin = DownloadTorrent()
    plugin._downloader_helper = helper
    plugin.systemconfig = Mock()
    plugin.systemconfig.get.return_value = [
        {
            "name": "后备下载",
            "storage": "local",
            "download_path": "/downloads-backup",
            "priority": 20,
        },
        {
            "name": "本地下载",
            "storage": "local",
            "download_path": "/downloads",
            "priority": 10,
        },
        {"name": "远程下载", "storage": "alist", "download_path": "/remote"},
    ]

    form, defaults = plugin.get_form()
    form_text = json.dumps(form, ensure_ascii=False)

    assert "本地下载" in form_text
    assert "/downloads" in form_text
    assert "远程下载" not in form_text
    assert "qb-main" in form_text
    assert "rt-main" not in form_text
    assert form_text.index("本地下载") < form_text.index("后备下载")
    assert defaults["downloader"] == ""
    assert defaults["torrent_urls"] == ""
    plugin.systemconfig.get.assert_called_once_with(SystemConfigKey.Directories)


def test_command_contract() -> None:
    """命令元数据必须保持 `/dt` 和插件动作名称。"""
    command = DownloadTorrent.get_command()
    assert command == [
        {
            "cmd": "/dt",
            "event": EventType.PluginAction,
            "desc": "种子下载",
            "category": "",
            "data": {"action": "download_torrent"},
        }
    ]
