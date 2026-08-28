from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins import cloudsyncdel as cloudsyncdel_module
from app.plugins.cloudsyncdel import CloudSyncDel
from app.schemas.types import EventType, MediaSource
from app.sdk.events import Event


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cloudsyncdel" / "__init__.py"


def _imports() -> set[str]:
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _event(path: Path, **extra) -> Event:
    data = {
        "action": "networkdisk_del",
        "media_path": str(path),
        "media_name": "示例电影",
        "media_type": "Movie",
        "media_source": MediaSource.Douban.value,
        "media_id": "1295644",
        "season_num": None,
        "episode_num": None,
    }
    data.update(extra)
    return Event(EventType.PluginAction, data)


def test_v3_manifest_and_import_contract() -> None:
    """V3 索引、旧代回退和 SDK 导入边界应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudSyncDel"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["CloudSyncDel"]

    assert manifest["version"] == CloudSyncDel.plugin_version == "2.0.0"
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
        "app.sdk.network",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
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
    assert "tmdb_id" not in source
    assert "apikey" not in source
    assert "logger.warn(" not in source


def test_plugin_imports_and_reinitializes_state() -> None:
    """插件应完成 V3 生命周期，并在热重载时清理旧映射。"""
    plugin = CloudSyncDel()
    plugin.init_plugin({})
    assert plugin.get_state() is False
    assert plugin.get_api()[0]["auth"] == "bear"
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]

    plugin.init_plugin({
        "enabled": True,
        "path": "/source:/cloud",
        "local_path": "/source:/local",
    })
    assert plugin.get_state() is True
    assert plugin._cloud_paths == {"/source": "/cloud"}
    assert plugin._local_paths == {"/source": "/local"}

    plugin.init_plugin({})
    assert plugin._cloud_paths == {}
    assert plugin._local_paths == {}
    assert plugin.stop_service() is None


def test_history_identity_migration_is_idempotent() -> None:
    """可恢复的旧历史应补齐 TMDB 身份，重复初始化不得重复写入。"""
    history = [{
        "title": "示例电影",
        "unique": "示例电影 550",
        "path": "/source/movie.mkv",
    }]
    plugin = CloudSyncDel()
    plugin.get_data = Mock(return_value=history)
    plugin.save_data = Mock()

    plugin.init_plugin({})

    assert history[0]["media_source"] == MediaSource.TMDB.value
    assert history[0]["media_id"] == "550"
    plugin.save_data.assert_called_once_with("history", history)

    plugin.save_data.reset_mock()
    plugin.init_plugin({})
    plugin.save_data.assert_not_called()


def test_invalid_history_identity_is_preserved() -> None:
    """无法可靠判断来源和 ID 的旧历史应原样保留。"""
    history = [{"title": "示例电影", "unique": "legacy-record"}]
    plugin = CloudSyncDel()
    plugin.get_data = Mock(return_value=history)
    plugin.save_data = Mock()

    plugin.init_plugin({})

    assert history == [{"title": "示例电影", "unique": "legacy-record"}]
    plugin.save_data.assert_not_called()


def test_mapping_mismatch_does_not_fallback_to_original_path(tmp_path: Path) -> None:
    """事件路径未匹配映射时，原路径绝不能被当作删除目标。"""
    source_root = tmp_path / "source"
    cloud_root = tmp_path / "cloud"
    source_root.mkdir()
    cloud_root.mkdir()
    original = tmp_path / "source-other" / "movie.mkv"
    original.parent.mkdir()
    original.write_text("keep", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "path": f"{source_root}:{cloud_root}",
    })
    plugin.eventmanager = Mock()
    plugin.clouddisk_del(_event(original))

    assert original.exists()
    plugin.eventmanager.send_event.assert_not_called()


def test_empty_directory_cleanup_stays_inside_mapping_root(tmp_path: Path) -> None:
    """删除媒体后的空目录清理只能到达映射目标根，不能删除其父目录。"""
    source_root = tmp_path / "source"
    target_parent = tmp_path / "targets"
    target_root = target_parent / "cloud"
    media_dir = target_root / "movies"
    source_root.mkdir()
    media_dir.mkdir(parents=True)
    target_parent.mkdir(exist_ok=True)
    media_file = media_dir / "movie.mkv"
    media_file.write_text("remove", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "path": f"{source_root}:{target_root}",
    })
    plugin._save_history = Mock()
    plugin.clouddisk_del(_event(source_root / "movies" / "movie.mkv"))

    assert not media_file.exists()
    assert not media_dir.exists()
    assert target_root.exists()
    assert target_parent.exists()


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """映射目标中的符号链接指向根外时必须停止删除。"""
    source_root = tmp_path / "source"
    target_root = tmp_path / "cloud"
    outside = tmp_path / "outside"
    source_root.mkdir()
    target_root.mkdir()
    outside.mkdir()
    escaped_file = outside / "movie.mkv"
    escaped_file.write_text("keep", encoding="utf-8")
    (target_root / "escape").symlink_to(outside, target_is_directory=True)

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "path": f"{source_root}:{target_root}",
    })
    plugin.clouddisk_del(_event(source_root / "escape" / "movie.mkv"))

    assert escaped_file.exists()


@pytest.mark.parametrize("leaf_symlink", [True, False])
def test_unsafe_local_mapping_never_falls_back_to_cloud_delete(
    tmp_path: Path,
    leaf_symlink: bool,
) -> None:
    """本地映射命中后发现符号链接越界时，整个删除链必须立即终止。"""
    source_root = tmp_path / "source"
    local_root = tmp_path / "local"
    cloud_root = tmp_path / "cloud"
    outside = tmp_path / "outside"
    source_root.mkdir()
    local_root.mkdir()
    cloud_root.mkdir()
    outside.mkdir()

    local_link = None
    if leaf_symlink:
        outside_file = outside / "movie.mkv"
        outside_file.write_text("keep", encoding="utf-8")
        local_link = local_root / "movie.mkv"
        local_link.symlink_to(outside_file)
        source_path = source_root / "movie.mkv"
        cloud_file = cloud_root / "movie.mkv"
    else:
        escaped_directory = outside / "escape"
        escaped_directory.mkdir()
        outside_file = escaped_directory / "movie.mkv"
        outside_file.write_text("keep", encoding="utf-8")
        (local_root / "escape").symlink_to(escaped_directory, target_is_directory=True)
        source_path = source_root / "escape" / "movie.mkv"
        cloud_file = cloud_root / "escape" / "movie.mkv"

    cloud_file.parent.mkdir(parents=True, exist_ok=True)
    cloud_file.write_text("keep", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "local_path": f"{source_root}:{local_root}",
        "path": f"{source_root}:{cloud_root}",
    })
    plugin.eventmanager = Mock()
    plugin.clouddisk_del(_event(source_path))

    assert outside_file.exists()
    assert cloud_file.exists()
    if leaf_symlink:
        assert local_link and not local_link.is_symlink()
        plugin.eventmanager.send_event.assert_called_once()
    else:
        assert (local_root / "escape").is_symlink()
        plugin.eventmanager.send_event.assert_not_called()


def test_local_delete_forwards_complete_media_identity(tmp_path: Path) -> None:
    """本地删除回调必须继续携带来源和来源原生 ID。"""
    source_root = tmp_path / "source"
    local_root = tmp_path / "local"
    local_media = local_root / "movie.mkv"
    source_root.mkdir()
    local_media.parent.mkdir()
    local_media.write_text("remove", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "local_path": f"{source_root}:{local_root}",
    })
    plugin.eventmanager = Mock()
    plugin.clouddisk_del(_event(source_root / "movie.mkv"))

    assert not local_media.exists()
    plugin.eventmanager.send_event.assert_called_once()
    event_type, payload = plugin.eventmanager.send_event.call_args.args
    assert event_type is EventType.PluginAction
    assert payload["action"] == "media_sync_del"
    assert payload["media_source"] == MediaSource.Douban.value
    assert payload["media_id"] == "1295644"
    assert "tmdb_id" not in payload


def test_local_directory_delete_forwards_complete_media_identity(tmp_path: Path) -> None:
    """本地目录删除也必须通知下游，并携带完整媒体身份。"""
    source_root = tmp_path / "source"
    local_root = tmp_path / "local"
    local_directory = local_root / "Season 01"
    source_root.mkdir()
    local_directory.mkdir(parents=True)
    (local_directory / "episode.mkv").write_text("remove", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "local_path": f"{source_root}:{local_root}",
    })
    plugin.eventmanager = Mock()
    plugin.clouddisk_del(_event(source_root / "Season 01"))

    assert not local_directory.exists()
    plugin.eventmanager.send_event.assert_called_once()
    _, payload = plugin.eventmanager.send_event.call_args.args
    assert payload["action"] == "media_sync_del"
    assert payload["media_source"] == MediaSource.Douban.value
    assert payload["media_id"] == "1295644"


def test_local_directory_symlink_forwards_complete_media_identity(tmp_path: Path) -> None:
    """映射根内目录符号链接只解除链接，并继续向下游转发完整媒体身份。"""
    source_root = tmp_path / "source"
    local_root = tmp_path / "local"
    local_directory = local_root / "target"
    local_link = local_root / "Season 01"
    source_root.mkdir()
    local_directory.mkdir(parents=True)
    (local_directory / "episode.mkv").write_text("keep", encoding="utf-8")
    local_link.symlink_to(local_directory, target_is_directory=True)

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "local_path": f"{source_root}:{local_root}",
    })
    plugin.eventmanager = Mock()
    plugin.clouddisk_del(_event(source_root / "Season 01"))

    assert not local_link.exists()
    assert local_directory.exists()
    plugin.eventmanager.send_event.assert_called_once()
    _, payload = plugin.eventmanager.send_event.call_args.args
    assert payload["action"] == "media_sync_del"
    assert payload["media_source"] == MediaSource.Douban.value
    assert payload["media_id"] == "1295644"


def test_missing_media_identity_fails_closed(tmp_path: Path) -> None:
    """缺少完整媒体身份时不得删除已映射文件。"""
    source_root = tmp_path / "source"
    cloud_root = tmp_path / "cloud"
    source_root.mkdir()
    cloud_file = cloud_root / "movie.mkv"
    cloud_file.parent.mkdir(parents=True)
    cloud_file.write_text("keep", encoding="utf-8")

    plugin = CloudSyncDel()
    plugin.init_plugin({"enabled": True, "path": f"{source_root}:{cloud_root}"})
    plugin.clouddisk_del(_event(source_root / "movie.mkv", media_source=None, media_id=None))

    assert cloud_file.exists()


def test_cloud_callback_failure_does_not_recreate_or_escape_delete(tmp_path: Path, monkeypatch) -> None:
    """外部回调失败只记录错误，不改变已完成的根内删除结果。"""
    source_root = tmp_path / "source"
    cloud_root = tmp_path / "cloud"
    source_root.mkdir()
    cloud_root.mkdir()
    cloud_file = cloud_root / "movie.mkv"
    cloud_file.write_text("remove", encoding="utf-8")

    request = Mock()
    request.post.side_effect = RuntimeError("network unavailable")
    monkeypatch.setattr(cloudsyncdel_module, "RequestUtils", Mock(return_value=request))

    plugin = CloudSyncDel()
    plugin.init_plugin({
        "enabled": True,
        "path": f"{source_root}:{cloud_root}",
        "url": "https://example.invalid/callback",
    })
    plugin._save_history = Mock()
    plugin.clouddisk_del(_event(source_root / "movie.mkv"))

    assert not cloud_file.exists()
    request.post.assert_called_once()
    assert plugin._save_history.call_count == 1


def test_api_uses_bear_auth_and_never_exposes_token() -> None:
    """历史 API 使用宿主 Bearer 鉴权，页面参数不能携带 API token。"""
    plugin = CloudSyncDel()
    plugin.init_plugin({})
    api = plugin.get_api()[0]
    assert api["auth"] == "bear"
    assert api["response_model"] is schemas.Response[None]

    plugin.get_data = Mock(return_value=[{"unique": "one", "title": "示例"}])
    plugin.save_data = Mock()
    assert plugin.delete_history("one").success is True
    plugin.save_data.assert_called_once_with("history", [])

    plugin.get_data = Mock(return_value=[{
        "unique": "one",
        "title": "示例",
        "type": "电视剧",
        "media_source": MediaSource.TMDB.value,
        "media_id": "550",
        "season": 1,
        "episode": 2,
        "image": "https://example.invalid/poster.jpg",
        "del_time": "2026-08-28 00:00:00",
    }])
    page = plugin.get_page()
    close_button = page[0]["content"][0]["content"][0]
    assert "apikey" not in close_button["events"]["click"]["params"]
    card_body = page[0]["content"][0]["content"][1]
    assert card_body["content"][0]["component"] == "VImg"
    assert card_body["content"][0]["props"]["src"] == "https://example.invalid/poster.jpg"
    detail_text = {
        component.get("text")
        for component in card_body["content"][1]["content"]
    }
    assert "季：1" in detail_text
    assert "集：2" in detail_text
