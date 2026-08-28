from __future__ import annotations

import ast
import json
import stat
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import cloudstrm as cloudstrm_module
from app.plugins.cloudstrm import CloudStrm


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cloudstrm" / "__init__.py"


def _plugin(tmp_path: Path, monitor_confs: str, **config) -> CloudStrm:
    """创建使用隔离索引文件的插件实例。"""
    plugin = CloudStrm()
    plugin.update_config = Mock()
    plugin.init_plugin(
        {
            "enabled": True,
            "cron": "",
            "rebuild_cron": "",
            "onlyonce": False,
            "rebuild": False,
            "copy_files": False,
            "https": False,
            "monitor_confs": monitor_confs,
            **config,
        }
    )
    plugin._cloud_files_json = tmp_path / "cloud_files.json"
    return plugin


def _index(path: Path) -> list[str]:
    """读取索引文件并返回排序后的源文件列表。"""
    return json.loads(path.read_text(encoding="utf-8"))


def test_v3_manifest_and_import_contracts() -> None:
    """V3 索引、版本和 SDK 导入边界必须一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudStrm"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["CloudStrm"]

    assert manifest["version"] == CloudStrm.plugin_version == "5.0.0"
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["release"] is True
    assert manifest["history"]["v5.0.0"]
    assert legacy_manifest["v3"] is False

    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
    }.issubset(imports)
    forbidden_prefixes = (
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
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "app.log" not in imports
    assert "BackgroundScheduler" not in source


def test_scan_maps_components_and_skips_hidden_directories(tmp_path: Path) -> None:
    """扫描必须按路径组件映射，并过滤隐藏、回收站和 extrafanart 目录。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    library = Path("/media/cloud")
    (source / "Show").mkdir(parents=True)
    (source / "Show" / "Movie.mp4").write_bytes(b"video")
    (source / ".hidden.mp4").write_bytes(b"hidden")
    (source / ".hidden" / "inside.mp4").parent.mkdir()
    (source / ".hidden" / "inside.mp4").write_bytes(b"hidden")
    (source / "@Recycle" / "deleted.mp4").parent.mkdir()
    (source / "@Recycle" / "deleted.mp4").write_bytes(b"recycle")
    (source / "#recycle" / "deleted.mp4").parent.mkdir()
    (source / "#recycle" / "deleted.mp4").write_bytes(b"recycle")
    (source / "@eaDir" / "metadata.mp4").parent.mkdir()
    (source / "@eaDir" / "metadata.mp4").write_bytes(b"metadata")
    (source / "extrafanart" / "fanart.mp4").parent.mkdir()
    (source / "extrafanart" / "fanart.mp4").write_bytes(b"fanart")

    plugin = _plugin(tmp_path, f"{source}#{target}#{library}")

    assert plugin.scan() is True
    assert (target / "Show" / "Movie.strm").read_text(encoding="utf-8") == (
        "/media/cloud/Show/Movie.mp4"
    )
    assert not (target / ".hidden.strm").exists()
    assert not (target / ".hidden").exists()
    assert not (target / "@Recycle").exists()
    assert not (target / "#recycle").exists()
    assert not (target / "@eaDir").exists()
    assert not (target / "extrafanart").exists()
    assert _index(plugin._cloud_files_json) == [str(source / "Show" / "Movie.mp4")]


def test_cloud_url_mapping_requires_component_boundary(tmp_path: Path) -> None:
    """云盘 URL 只能从配置的挂载根按组件生成，不能使用字符串前缀替换。"""
    source = tmp_path / "cloud"
    target = tmp_path / "target"
    cloud_path = source
    source.mkdir()
    (source / "Movie Name.mp4").write_bytes(b"video")
    plugin = _plugin(
        tmp_path,
        f"{source}#{target}#alist#{cloud_path}#alist.example.test",
    )

    assert plugin.scan() is True
    assert (target / "Movie Name.strm").read_text(encoding="utf-8") == (
        "http://alist.example.test/d/%2FMovie%20Name.mp4"
    )

    mirrored = tmp_path / "cloudish" / "Movie Name.mp4"
    assert plugin._CloudStrm__build_strm_content(
        plugin._monitors[0], mirrored, target / "Movie Name.mp4"
    ) is None


def test_existing_targets_are_never_overwritten(tmp_path: Path) -> None:
    """目标文件或 STRM 已存在时只跳过，不覆盖已有内容。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    (target).mkdir()
    strm = target / "Movie.strm"
    strm.write_text("keep", encoding="utf-8")

    plugin = _plugin(tmp_path, f"{source}#{target}#/media")

    assert plugin.scan() is True
    assert strm.read_text(encoding="utf-8") == "keep"
    assert _index(plugin._cloud_files_json) == [str(source / "Movie.mp4")]


def test_failed_generation_does_not_poison_index(tmp_path: Path) -> None:
    """URL 配置不匹配时不得把未生成成功的文件写入处理索引。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    plugin = _plugin(
        tmp_path,
        f"{source}#{target}#cd2#{tmp_path / 'different-root'}#cd2.example.test",
    )

    assert plugin.scan() is False
    assert not target.exists()
    assert not plugin._cloud_files_json.exists()


def test_rebuild_replaces_index_without_deleting_outputs(tmp_path: Path) -> None:
    """重建只替换源文件索引，不删除已有目标文件。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    old_source = source / "Old.mp4"
    old_source.write_bytes(b"old")
    plugin = _plugin(tmp_path, f"{source}#{target}#/media")

    assert plugin.scan() is True
    old_output = target / "Old.strm"
    assert old_output.exists()
    old_source.unlink()
    new_source = source / "New.mp4"
    new_source.write_bytes(b"new")
    plugin._rebuild = True

    assert plugin.scan() is True
    assert _index(plugin._cloud_files_json) == [str(new_source)]
    assert old_output.exists()
    assert plugin._rebuild is False


def test_index_write_is_atomic_and_json_is_valid(tmp_path: Path) -> None:
    """索引写入完成后应为有效 JSON，且不留下临时文件。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    plugin = _plugin(tmp_path, f"{source}#{target}#/media")

    assert plugin.scan() is True
    assert _index(plugin._cloud_files_json) == [str(source / "Movie.mp4")]
    assert not list(plugin._cloud_files_json.parent.glob(f".{plugin._cloud_files_json.name}.*"))
    assert stat.S_IMODE((target / "Movie.strm").stat().st_mode) == 0o644


def test_temporary_file_setup_failures_return_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """临时文件创建失败必须收敛为失败结果，不能穿透宿主调度器。"""
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    plugin = _plugin(tmp_path, "")
    monkeypatch.setattr(
        cloudstrm_module.tempfile,
        "mkstemp",
        Mock(side_effect=PermissionError("denied")),
    )

    assert plugin._CloudStrm__atomic_create_text(
        tmp_path / "target" / "Movie.strm", "content"
    ) is False
    assert plugin._CloudStrm__copy_without_overwrite(
        source, tmp_path / "copy" / "source.txt"
    ) is False
    assert plugin._CloudStrm__write_index({str(source)}) is False


def test_host_services_cover_cron_rebuild_and_onlyonce(tmp_path: Path) -> None:
    """周期、重建和一次性任务应由宿主服务投影统一注册。"""
    source = tmp_path / "source"
    source.mkdir()
    plugin = _plugin(
        tmp_path,
        f"{source}#{tmp_path / 'target'}#/media",
        cron="0 0 * * *",
        rebuild_cron="0 1 * * *",
        onlyonce=True,
    )

    services = plugin.get_service()
    assert {service["id"] for service in services} == {
        "CloudStrm",
        "CloudStrmRebuild",
        "CloudStrmOnce",
    }
    assert plugin.get_state() is True
    assert all(
        isinstance(service["trigger"], CronTrigger)
        and str(service["trigger"].timezone) == "Asia/Shanghai"
        for service in services
        if service["id"] != "CloudStrmOnce"
    )
    once = next(service for service in services if service["id"] == "CloudStrmOnce")
    plugin.update_config = Mock(return_value=True)
    assert once["func"]() is True
    assert plugin._onlyonce is False
    assert plugin._run_once is False


@pytest.mark.parametrize("persisted", [False, RuntimeError("storage unavailable")])
def test_onlyonce_persistence_failure_keeps_pending_state(
    tmp_path: Path,
    persisted: object,
) -> None:
    """一次性状态回写失败时不得扫描或消费待执行意图。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    target = tmp_path / "target"
    plugin = _plugin(
        tmp_path,
        f"{source}#{target}#/media",
        enabled=False,
        onlyonce=True,
    )
    if isinstance(persisted, Exception):
        plugin.update_config = Mock(side_effect=persisted)
    else:
        plugin.update_config = Mock(return_value=persisted)

    once = plugin.get_service()[0]
    assert once["func"]() is False

    assert plugin._onlyonce is True
    assert plugin._run_once is True
    assert not target.exists()
    assert not plugin._cloud_files_json.exists()


def test_onlyonce_waits_for_running_scan_before_consuming(tmp_path: Path) -> None:
    """date job 必须等待扫描槽，避免重叠时丢失一次性任务。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    target = tmp_path / "target"
    plugin = _plugin(
        tmp_path,
        f"{source}#{target}#/media",
        enabled=False,
        onlyonce=True,
    )
    plugin.update_config = Mock(return_value=True)
    once = plugin.get_service()[0]
    plugin._run_lock.acquire()
    result: dict[str, bool] = {}
    started = threading.Event()

    def run_once() -> None:
        started.set()
        result["value"] = once["func"]()

    worker = threading.Thread(target=run_once)
    worker.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    plugin.update_config.assert_not_called()

    plugin._run_lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["value"] is True
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False
    assert (target / "Movie.strm").exists()


def test_scan_is_single_flight(tmp_path: Path) -> None:
    """并发扫描不得重入同一索引和目标写入流程。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    plugin = _plugin(tmp_path, f"{source}#{tmp_path / 'target'}#/media")
    assert plugin._run_lock.acquire(blocking=False)
    try:
        assert plugin.scan() is False
    finally:
        plugin._run_lock.release()


def test_reload_waits_for_running_scan_before_switching_config(tmp_path: Path) -> None:
    """热重载必须在旧扫描结束后一次性切换全部运行配置。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "Movie.mp4").write_bytes(b"video")
    old_target = tmp_path / "old-target"
    new_target = tmp_path / "new-target"
    plugin = _plugin(tmp_path, f"{source}#{old_target}#/old-media")
    original_process = plugin._CloudStrm__process_file
    entered = threading.Event()
    release = threading.Event()
    observed_https: list[bool] = []

    def blocking_process(*args, **kwargs) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        observed_https.append(plugin._https)
        return original_process(*args, **kwargs)

    plugin._CloudStrm__process_file = blocking_process
    scan_worker = threading.Thread(target=plugin.scan)
    scan_worker.start()
    assert entered.wait(timeout=1)

    reload_worker = threading.Thread(
        target=plugin.init_plugin,
        args=(
            {
                "enabled": True,
                "https": True,
                "monitor_confs": f"{source}#{new_target}#/new-media",
            },
        ),
    )
    reload_worker.start()
    time.sleep(0.05)

    assert reload_worker.is_alive()
    assert plugin._https is False
    assert plugin._monitors[0].target_dir == old_target

    release.set()
    scan_worker.join(timeout=2)
    reload_worker.join(timeout=2)

    assert not scan_worker.is_alive()
    assert not reload_worker.is_alive()
    assert observed_https == [False]
    assert plugin._https is True
    assert plugin._monitors[0].target_dir == new_target


def test_stop_waits_for_running_scan_and_blocks_later_writes(tmp_path: Path) -> None:
    """停止返回时不得仍有扫描写入，过期宿主回调也必须失败关闭。"""
    source = tmp_path / "source"
    source.mkdir()
    first_file = source / "First.mp4"
    first_file.write_bytes(b"video")
    target = tmp_path / "target"
    plugin = _plugin(tmp_path, f"{source}#{target}#/media")
    original_process = plugin._CloudStrm__process_file
    entered = threading.Event()
    release = threading.Event()

    def blocking_process(*args, **kwargs) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        return original_process(*args, **kwargs)

    plugin._CloudStrm__process_file = blocking_process
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

    second_file = source / "Second.mp4"
    second_file.write_bytes(b"video")
    assert plugin.scan() is False
    assert not (target / "Second.strm").exists()
