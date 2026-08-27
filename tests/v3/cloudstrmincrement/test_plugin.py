from __future__ import annotations

import ast
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins.cloudstrmincrement import CloudStrmIncrement
from app.runtime.events import Event
from app.schemas.types import EventType


PLUGIN_PATH = (
    REPOSITORY_ROOT / "plugins.v3" / "cloudstrmincrement" / "__init__.py"
)


def _plugin(monitor_confs: str, **config) -> CloudStrmIncrement:
    """创建使用指定目录映射的启用插件。"""
    plugin = CloudStrmIncrement()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin(
        {
            "enabled": True,
            "cron": "",
            "onlyonce": False,
            "copy_files": False,
            "del_source": True,
            "https": False,
            "monitor_confs": monitor_confs,
            "no_del_dirs": "",
            "rmt_mediaext": ".mp4,.mkv",
            **config,
        }
    )
    return plugin


def test_v3_manifest_and_sdk_import_contracts() -> None:
    """V3 版本、独立配置身份和公开 SDK 导入必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudStrmIncrement"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["CloudStrmIncrement"]

    assert manifest["version"] == CloudStrmIncrement.plugin_version == "2.0.0"
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["release"] is True
    assert legacy["v3"] is False
    assert CloudStrmIncrement.plugin_config_prefix == "cloudstrm_"

    imports = {
        node.module
        for node in ast.walk(ast.parse(PLUGIN_PATH.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"app.sdk.config", "app.sdk.events", "app.sdk.logging"}.issubset(imports)
    forbidden = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden) for module in imports)
    assert "app.log" not in imports
    assert "BackgroundScheduler" not in PLUGIN_PATH.read_text(encoding="utf-8")


def test_scan_archives_media_then_deletes_increment_and_keeps_named_parent(
    tmp_path: Path,
) -> None:
    """输出成功后才删除增量文件，并停止清理配置为保留的空目录。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment_file = increment / "Keep" / "Show" / "Movie.mp4"
    increment_file.parent.mkdir(parents=True)
    increment_file.write_bytes(b"video")
    plugin = _plugin(
        f"{increment}#{source}#{target}#/media/cloud",
        no_del_dirs="keep",
    )

    assert plugin.scan() is True

    archived = source / "Keep" / "Show" / "Movie.mp4"
    assert archived.read_bytes() == b"video"
    assert (target / "Keep" / "Show" / "Movie.strm").read_text(
        encoding="utf-8"
    ) == "/media/cloud/Keep/Show/Movie.mp4"
    assert not increment_file.exists()
    assert not (increment / "Keep" / "Show").exists()
    assert (increment / "Keep").is_dir()


def test_existing_archive_is_not_overwritten_or_deleted(tmp_path: Path) -> None:
    """源树已有同名文件时必须保留两侧数据并失败关闭。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    source.mkdir()
    increment_file = increment / "Movie.mp4"
    source_file = source / "Movie.mp4"
    increment_file.write_bytes(b"new")
    source_file.write_bytes(b"old")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")

    assert plugin.scan() is False
    assert increment_file.read_bytes() == b"new"
    assert source_file.read_bytes() == b"old"
    assert not (target / "Movie.strm").exists()


def test_copy_files_preserves_increment_when_source_deletion_disabled(
    tmp_path: Path,
) -> None:
    """非媒体文件可归档到两级目标，关闭源删除时保留增量副本。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment_file = increment / "Show" / "poster.jpg"
    increment_file.parent.mkdir(parents=True)
    increment_file.write_bytes(b"image")
    plugin = _plugin(
        f"{increment}#{source}#{target}#/media",
        copy_files=True,
        del_source=False,
    )

    assert plugin.scan() is True
    assert increment_file.read_bytes() == b"image"
    assert (source / "Show" / "poster.jpg").read_bytes() == b"image"
    assert (target / "Show" / "poster.jpg").read_bytes() == b"image"


def test_copy_failure_never_deletes_increment_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档复制失败时不得删除增量源或创建媒体库输出。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    increment_file = increment / "Movie.mp4"
    increment_file.write_bytes(b"video")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")
    monkeypatch.setattr(
        plugin,
        "_CloudStrmIncrement__copy_without_overwrite",
        Mock(return_value=False),
    )

    assert plugin.scan() is False
    assert increment_file.exists()
    assert not source.exists()
    assert not target.exists()


def test_output_failure_recovers_from_matching_archived_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输出瞬时失败后应复用内容一致的归档副本完成下一次事务。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    increment_file = increment / "Movie.mp4"
    increment_file.write_bytes(b"video")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")
    original = plugin._CloudStrmIncrement__atomic_create_text
    attempts = 0

    def fail_once(path: Path, content: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original(path, content)

    monkeypatch.setattr(
        plugin, "_CloudStrmIncrement__atomic_create_text", fail_once
    )

    assert plugin.scan() is False
    assert increment_file.exists()
    assert (source / "Movie.mp4").read_bytes() == b"video"
    assert plugin.scan() is True
    assert not increment_file.exists()
    assert (target / "Movie.strm").read_text(encoding="utf-8") == "/media/Movie.mp4"


def test_delete_failure_recovers_without_overwriting_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """增量删除失败后应在下一次扫描重试删除并保留归档副本。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    increment_file = increment / "Movie.mp4"
    increment_file.write_bytes(b"video")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")
    original_unlink = Path.unlink
    failed = False

    def fail_increment_once(path: Path, *args, **kwargs) -> None:
        nonlocal failed
        if path == increment_file and not failed:
            failed = True
            raise OSError("busy")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_increment_once)

    assert plugin.scan() is False
    assert increment_file.exists()
    assert (source / "Movie.mp4").read_bytes() == b"video"
    assert (target / "Movie.strm").exists()
    assert plugin.scan() is True
    assert not increment_file.exists()
    assert (source / "Movie.mp4").read_bytes() == b"video"


def test_hidden_and_symlinked_increment_paths_are_skipped(tmp_path: Path) -> None:
    """隐藏、回收站及软链接路径不得进入归档和输出目录。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    (increment / ".hidden").mkdir(parents=True)
    (increment / ".hidden" / "Movie.mp4").write_bytes(b"hidden")
    (increment / "@Recycle").mkdir()
    (increment / "@Recycle" / "Deleted.mp4").write_bytes(b"deleted")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    (increment / "linked.mp4").symlink_to(outside)
    plugin = _plugin(f"{increment}#{source}#{target}#/media")

    assert plugin.scan() is True
    assert not source.exists()
    assert not target.exists()
    assert outside.read_bytes() == b"outside"


def test_cd2_url_uses_component_relative_cloud_path(tmp_path: Path) -> None:
    """CD2 URL 必须按云盘根组件映射并使用配置的 HTTPS 协议。"""
    increment = tmp_path / "increment"
    cloud_root = tmp_path / "cloud"
    source = cloud_root / "source"
    target = tmp_path / "target"
    increment_file = increment / "Show" / "Movie Name.mp4"
    increment_file.parent.mkdir(parents=True)
    increment_file.write_bytes(b"video")
    plugin = _plugin(
        f"{increment}#{source}#{target}#cd2#{cloud_root}#localhost:19798/base",
        https=True,
    )

    assert plugin.scan() is True
    content = (target / "Show" / "Movie Name.strm").read_text(encoding="utf-8")
    assert content == (
        "https://localhost:19798/base/static/https/localhost:19798/False/"
        "%2Fsource%2FShow%2FMovie%20Name.mp4"
    )


def test_overlapping_roots_are_rejected(tmp_path: Path) -> None:
    """相互包含的增量、源和目标目录不得形成递归复制配置。"""
    increment = tmp_path / "data"
    source = increment / "source"
    target = tmp_path / "target"
    plugin = _plugin(f"{increment}#{source}#{target}#/media")

    assert plugin._monitors == []
    assert plugin.scan() is False


def test_realtime_event_processes_existing_source_file(tmp_path: Path) -> None:
    """目录监控联动事件应处理源树文件，不重复执行增量复制。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    source_file = source / "Movie.mp4"
    source_file.write_bytes(b"video")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")
    event = Event(
        EventType.PluginAction,
        {"action": "cloudstrm_file", "file_path": str(source_file)},
    )

    assert plugin.cloudstrm_file(event) is True
    assert (target / "Movie.strm").read_text(encoding="utf-8") == "/media/Movie.mp4"


def test_realtime_event_rejects_source_and_target_symlinks(tmp_path: Path) -> None:
    """联动事件不得读取源软链接，也不能把目标软链接当作已完成输出。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    outside_source = tmp_path / "outside.mp4"
    outside_source.write_bytes(b"outside")
    source_link = source / "Linked.mp4"
    source_link.symlink_to(outside_source)
    plugin = _plugin(f"{increment}#{source}#{target}#/media")

    source_event = Event(
        EventType.PluginAction,
        {"action": "cloudstrm_file", "file_path": str(source_link)},
    )
    assert plugin.cloudstrm_file(source_event) is False

    regular_source = source / "Movie.mp4"
    regular_source.write_bytes(b"video")
    outside_target = tmp_path / "outside.strm"
    outside_target.write_text("sentinel", encoding="utf-8")
    (target / "Movie.strm").symlink_to(outside_target)
    target_event = Event(
        EventType.PluginAction,
        {"action": "cloudstrm_file", "file_path": str(regular_source)},
    )
    assert plugin.cloudstrm_file(target_event) is False
    assert outside_target.read_text(encoding="utf-8") == "sentinel"

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    outside_file = outside_dir / "poster.jpg"
    outside_file.write_bytes(b"private")
    (source / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
    plugin.init_plugin(
        {
            "enabled": True,
            "copy_files": True,
            "monitor_confs": f"{increment}#{source}#{target}#/media",
        }
    )
    middle_link_event = Event(
        EventType.PluginAction,
        {
            "action": "cloudstrm_file",
            "file_path": str(source / "linked-dir" / "poster.jpg"),
        },
    )
    assert plugin.cloudstrm_file(middle_link_event) is False
    assert not (target / "linked-dir" / "poster.jpg").exists()


def test_host_services_use_timezone_and_reliable_onlyonce(tmp_path: Path) -> None:
    """cron/date 服务使用宿主时区，once 持久化成功后才执行和消费。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    (increment / "Movie.mp4").write_bytes(b"video")
    plugin = _plugin(
        f"{increment}#{source}#{target}#/media",
        enabled=False,
        onlyonce=True,
        cron="0 0 * * *",
    )

    services = plugin.get_service()
    assert len(services) == 1
    assert isinstance(services[0]["trigger"], DateTrigger)
    assert str(services[0]["trigger"].run_date.tzinfo) == "Asia/Shanghai"
    assert services[0]["func"]() is True
    assert plugin._run_once is False
    assert plugin._onlyonce is False
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False
    assert (target / "Movie.strm").exists()

    plugin.init_plugin(
        {
            "enabled": True,
            "cron": "0 0 * * *",
            "monitor_confs": f"{increment}#{source}#{target}#/media",
        }
    )
    cron = plugin.get_service()[0]["trigger"]
    assert isinstance(cron, CronTrigger)
    assert str(cron.timezone) == "Asia/Shanghai"


@pytest.mark.parametrize("persisted", [False, RuntimeError("storage unavailable")])
def test_onlyonce_persistence_failure_keeps_pending_state(
    tmp_path: Path,
    persisted: object,
) -> None:
    """once 回写失败时不得扫描、消费或删除增量文件。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    increment_file = increment / "Movie.mp4"
    increment_file.write_bytes(b"video")
    plugin = _plugin(
        f"{increment}#{source}#{target}#/media",
        enabled=False,
        onlyonce=True,
    )
    plugin.update_config = (
        Mock(side_effect=persisted)
        if isinstance(persisted, Exception)
        else Mock(return_value=persisted)
    )

    assert plugin.get_service()[0]["func"]() is False
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    assert increment_file.exists()
    assert not source.exists()


def test_stop_waits_for_running_scan_and_blocks_stale_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止返回时文件操作已收敛，过期回调不能继续处理新文件。"""
    increment = tmp_path / "increment"
    source = tmp_path / "source"
    target = tmp_path / "target"
    increment.mkdir()
    first = increment / "First.mp4"
    first.write_bytes(b"video")
    plugin = _plugin(f"{increment}#{source}#{target}#/media")
    original = plugin._CloudStrmIncrement__process_increment_file
    entered = threading.Event()
    release = threading.Event()

    def blocking_process(*args, **kwargs) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        plugin, "_CloudStrmIncrement__process_increment_file", blocking_process
    )
    scan_worker = threading.Thread(target=plugin.scan)
    scan_worker.start()
    assert entered.wait(timeout=1)
    stop_worker = threading.Thread(target=plugin.stop_service)
    stop_worker.start()
    time.sleep(0.05)
    assert stop_worker.is_alive()

    release.set()
    scan_worker.join(timeout=2)
    stop_worker.join(timeout=2)
    assert not scan_worker.is_alive()
    assert not stop_worker.is_alive()

    second = increment / "Second.mp4"
    second.write_bytes(b"video")
    assert plugin.scan() is False
    assert second.exists()
    assert not (target / "Second.strm").exists()
