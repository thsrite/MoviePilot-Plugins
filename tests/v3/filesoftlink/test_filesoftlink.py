from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins.filesoftlink import FileSoftLink
from app.schemas.types import EventType
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "filesoftlink" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


@pytest.fixture
def plugin(tmp_path: Path) -> FileSoftLink:
    """构造使用隔离目录的插件实例，避免访问本机媒体目录。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    instance = FileSoftLink()
    instance.systemconfig.get = Mock(return_value=[])
    instance.init_plugin(
        {
            "monitor_dirs": f"{source}:{target}",
            "copy_files": True,
            "force": False,
            "rmt_mediaext": ".mkv, .mp4",
        }
    )
    return instance


def test_v3_manifest_and_import_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["FileSoftLink"]
    legacy_v2 = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["FileSoftLink"]
    legacy_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["FileSoftLink"]

    assert manifest["version"] == FileSoftLink.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert legacy_v2["v3"] is False
    assert legacy_v1["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.utilities",
    }.issubset(imports)
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
    assert "from app.sdk.network import RequestUtils" not in source
    assert "logger.warn(" not in source


def test_plugin_initializes_and_declares_async_bearer_api() -> None:
    """插件应能完成 V3 生命周期初始化，并声明异步 Bearer API。"""
    plugin = FileSoftLink()
    plugin.init_plugin({})

    api = plugin.get_api()[0]
    assert plugin.plugin_version == "3.0.0"
    assert api["auth"] == "bear"
    assert api["response_model"] is schemas.Response[None]
    assert inspect.iscoroutinefunction(api["endpoint"])
    assert plugin.get_page() == []
    plugin.stop_service()


def test_file_operations_are_confined_to_configured_roots(
    plugin: FileSoftLink, tmp_path: Path
) -> None:
    """媒体软链接和非媒体复制均应使用相对路径映射，不能接受前缀碰撞。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    media = source / "nested" / "movie.MKV"
    sidecar = source / "nested" / "movie.nfo"
    media.parent.mkdir()
    media.write_bytes(b"video")
    sidecar.write_text("metadata", encoding="utf-8")

    plugin._handle_file(str(media), str(source))
    plugin._handle_file(str(sidecar), str(source))

    media_target = target / "nested" / "movie.MKV"
    sidecar_target = target / "nested" / "movie.nfo"
    assert media_target.is_symlink()
    assert media_target.resolve() == media.resolve()
    assert sidecar_target.read_text(encoding="utf-8") == "metadata"

    sibling = tmp_path / "source-sibling" / "outside.mkv"
    sibling.parent.mkdir()
    sibling.write_bytes(b"outside")
    plugin._handle_file(str(sibling), str(source))
    assert not (tmp_path / "target-sibling" / "outside.mkv").exists()


def test_force_overwrite_never_removes_target_directories(
    plugin: FileSoftLink, tmp_path: Path
) -> None:
    """强制覆盖只允许替换目标文件，不得删除目标目录。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    media = source / "movie.mkv"
    media.write_bytes(b"video")
    target_file = target / "movie.mkv"
    target_file.mkdir()
    plugin._force = True

    plugin._handle_file(str(media), str(source))

    assert target_file.is_dir()
    assert not target_file.is_symlink()


def test_target_symlink_escape_fails_closed(plugin: FileSoftLink, tmp_path: Path) -> None:
    """目标目录中的符号链接指向外部时，不得写入其解析后的路径。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    outside.mkdir()
    target_subdir = target / "nested"
    target_subdir.symlink_to(outside, target_is_directory=True)
    media = source / "nested" / "movie.mkv"
    media.parent.mkdir()
    media.write_bytes(b"video")
    plugin._force = True

    plugin._handle_file(str(media), str(source))

    assert not (outside / "movie.mkv").exists()


def test_plugin_action_uses_typed_event_snapshot() -> None:
    """插件动作必须读取 V3 事件快照，并忽略其它动作。"""
    plugin = FileSoftLink()
    plugin.sync_all = Mock(return_value=True)
    plugin.post_message = Mock()
    event = Event(
        EventType.PluginAction,
        {"action": "softlink_sync", "channel": "test", "user": "user-1"},
    )

    plugin.remote_sync(event)

    plugin.sync_all.assert_called_once_with()

    plugin.sync_all.reset_mock()
    plugin.remote_sync(
        Event(EventType.PluginAction, {"action": "unrelated"})
    )
    plugin.sync_all.assert_not_called()


def test_async_api_moves_sync_work_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """动态 API 应返回统一响应，并把本地遍历委托到线程。"""
    plugin = FileSoftLink()
    sync_all = Mock(return_value=True)
    monkeypatch.setattr(plugin, "sync_all", sync_all)

    response = asyncio.run(plugin.sync())

    assert response == schemas.Response(success=True, message="监控目录同步完成")
    sync_all.assert_called_once_with()


def test_form_defaults_match_configuration_contract() -> None:
    """表单字段和默认值必须覆盖旧配置的所有可持久化字段。"""
    form, defaults = FileSoftLink().get_form()
    assert form[0]["component"] == "VForm"
    fields = {
        child["content"][0]["props"]["model"]
        for child in form[0]["content"][0]["content"]
    }
    assert fields == set(defaults)
    assert defaults["copy_files"] is True
    assert defaults["mode"] == "compatibility"
    assert defaults["sync_interval"] == 0


def test_stop_service_retains_resources_that_did_not_converge() -> None:
    """关闭失败或超时的资源必须保留 owner，避免热重载叠加重复任务。"""
    plugin = FileSoftLink()
    observer = Mock()
    observer.is_alive.return_value = True
    scheduler = Mock()
    scheduler.remove_all_jobs.side_effect = RuntimeError("shutdown failed")
    plugin._observer = [observer]
    plugin._scheduler = scheduler

    plugin.stop_service()

    assert plugin._observer == [observer]
    assert plugin._scheduler is scheduler


def test_stop_service_releases_resources_after_shutdown() -> None:
    """监听器与调度器确认停止后应释放实例句柄。"""
    plugin = FileSoftLink()
    observer = Mock()
    observer.is_alive.return_value = False
    scheduler = Mock(running=True)
    plugin._observer = [observer]
    plugin._scheduler = scheduler

    plugin.stop_service()

    observer.stop.assert_called_once_with()
    observer.join.assert_called_once_with(timeout=10)
    scheduler.shutdown.assert_called_once_with(wait=False)
    assert plugin._observer == []
    assert plugin._scheduler is None
