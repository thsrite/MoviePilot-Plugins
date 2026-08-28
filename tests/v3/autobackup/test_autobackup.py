from __future__ import annotations

import ast
import json
import os
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins import autobackup as autobackup_module
from app.plugins.autobackup import AutoBackup


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "autobackup" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _plugin(**attrs) -> AutoBackup:
    """构造不触发宿主配置写入的测试插件实例。"""
    plugin = object.__new__(AutoBackup)
    defaults = {
        "_enabled": True,
        "_cron": None,
        "_cnt": 0,
        "_onlyonce": False,
        "_notify": False,
        "_back_path": "",
        "_webdav_enabled": False,
        "_webdav_hostname": "https://dav.example/backup",
        "_webdav_login": "user",
        "_webdav_password": "password",
        "_webdav_digest_auth": False,
        "_webdav_max_count": 0,
        "_webdav_notify": False,
        "_webdav_disable_check": False,
        "_webdav_client": None,
        "_scheduler": None,
    }
    defaults.update(attrs)
    for name, value in defaults.items():
        setattr(plugin, name, value)
    return plugin


def test_v3_manifest_and_import_contracts() -> None:
    """V3 索引、旧代回退开关和宿主 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["AutoBackup"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["AutoBackup"]

    assert manifest["version"] == AutoBackup.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {"app.sdk.config", "app.sdk.database", "app.sdk.logging"}.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
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
    assert "create_backup()" in source
    assert "db_query" not in source
    assert "Session" not in source
    assert "pg_dump" not in source
    assert "user.db" not in source
    assert "NotificationType" not in source


def test_v3_plugin_initializes_and_declares_response_model() -> None:
    """插件应能在隔离宿主配置后初始化，并声明准确的 Bearer API 响应。"""
    plugin = AutoBackup()
    plugin.init_plugin({})

    api = plugin.get_api()
    assert plugin.get_state() is False
    assert api[0]["auth"] == "bear"
    assert api[0]["response_model"] is schemas.Response[None]
    assert api[0]["endpoint"] == plugin.backup
    assert plugin.get_command() == []
    assert plugin.get_service() == []
    assert plugin.get_page() is None
    plugin.stop_service()


def test_backup_file_uses_host_artifact_and_archives_configuration(tmp_path, monkeypatch) -> None:
    """归档必须使用宿主快照，并保留配置文件而不直接复制活动数据库。"""
    config_path = tmp_path / "config"
    config_path.mkdir()
    (config_path / "category.yaml").write_text("movie: 电影\n", encoding="utf-8")
    (config_path / "app.env").write_text("SUPERUSER_PASSWORD=secret\n", encoding="utf-8")
    (config_path / "user.db").write_bytes(b"active database must not be copied")
    cookies = config_path / "cookies"
    cookies.mkdir()
    (cookies / "site.cookie").write_text("cookie", encoding="utf-8")

    artifact_path = tmp_path / "moviepilot_v3.0.0_sqlite_20260827_120000.db"
    artifact_path.write_bytes(b"validated sqlite snapshot")
    artifact = SimpleNamespace(name=artifact_path.name, path=artifact_path)
    monkeypatch.setattr(autobackup_module, "create_backup", Mock(return_value=artifact))
    monkeypatch.setattr(
        autobackup_module,
        "settings",
        SimpleNamespace(CONFIG_PATH=config_path, PLUGIN_DATA_PATH=tmp_path / "plugins"),
    )

    destination = tmp_path / "archives"
    archive = AutoBackup.backup_file(destination)

    assert archive is not None
    assert Path(archive).is_file()
    with zipfile.ZipFile(archive) as archive_file:
        assert set(archive_file.namelist()) == {
            artifact_path.name,
            "category.yaml",
            "app.env",
            "cookies/",
            "cookies/site.cookie",
        }
        assert archive_file.read(artifact_path.name) == b"validated sqlite snapshot"
        assert "user.db" not in archive_file.namelist()
    assert not [path for path in destination.glob("bk_*") if path.is_dir()]


def test_backup_returns_v3_response(monkeypatch) -> None:
    """手动备份 API 应返回标准三段式响应，而不是旧版二元组。"""
    plugin = _plugin()
    monkeypatch.setattr(
        plugin,
        "_AutoBackup__backup",
        Mock(return_value=(True, "备份完成")),
    )

    response = plugin.backup()

    assert isinstance(response, schemas.Response)
    assert response.success is True
    assert response.message == "备份完成"
    assert response.data is None


def test_backup_response_is_failed_when_webdav_upload_fails(tmp_path, monkeypatch) -> None:
    """WebDAV 上传失败时整体任务必须返回失败，不能把远程路径当成布尔值。"""
    archive = tmp_path / "bk_20260827120000.zip"
    archive.write_bytes(b"backup archive")
    plugin = _plugin(
        _back_path=str(tmp_path),
        _webdav_enabled=True,
        _notify=False,
    )
    monkeypatch.setattr(plugin, "backup_file", Mock(return_value=str(archive)))
    monkeypatch.setattr(
        plugin,
        "_AutoBackup__upload_to_webdav",
        Mock(return_value=(False, "上传失败")),
    )

    response = plugin.backup()

    assert response.success is False
    assert "上传失败" in response.message


def test_upload_uses_local_archive_and_checks_remote_file(tmp_path) -> None:
    """WebDAV 上传应传递完整本地归档路径并验证同名远程文件。"""
    archive = tmp_path / "bk_20260827120000.zip"
    archive.write_bytes(b"backup archive")
    client = MagicMock()
    client.check.return_value = True
    plugin = _plugin(_webdav_client=client)

    success, remote_path = plugin._AutoBackup__upload_to_webdav(str(archive))

    assert success is True
    assert remote_path == "https://dav.example/backup/bk_20260827120000.zip"
    client.list.assert_called_once_with("/")
    client.upload_sync.assert_called_once_with(
        remote_path=archive.name,
        local_path=str(archive),
    )
    client.check.assert_called_once_with(archive.name)


def test_local_retention_only_removes_valid_backup_archives(tmp_path) -> None:
    """本地清理只处理本插件归档，并保留配置要求的最新份数。"""
    old = tmp_path / "bk_20260825120000.zip"
    current = tmp_path / "bk_20260826120000.zip"
    newest = tmp_path / "bk_20260827120000.zip"
    for index, path in enumerate((old, current, newest)):
        path.write_bytes(b"archive")
        timestamp = time.time() - (3 - index) * 60
        os.utime(path, (timestamp, timestamp))
    unrelated = tmp_path / "moviepilot_v3.0.0_sqlite_20260827_120000.db"
    unrelated.write_bytes(b"host backup")

    plugin = _plugin(_cnt=2)
    count, deleted = plugin._AutoBackup__clean_local_backups(tmp_path)

    assert (count, deleted) == (3, 1)
    assert not old.exists()
    assert current.exists() and newest.exists() and unrelated.exists()


def test_remote_retention_ignores_unrelated_files() -> None:
    """WebDAV 清理只删除符合本插件命名合同的旧归档。"""
    client = MagicMock()
    client.list.return_value = [
        "/bk_20260825120000.zip",
        "/bk_20260826120000.zip",
        "/bk_20260827120000.zip",
        "/other.zip",
    ]
    plugin = _plugin(_webdav_client=client)

    plugin._AutoBackup__clean_old_webdav_backups(2)

    client.clean.assert_called_once_with("/bk_20260825120000.zip")
