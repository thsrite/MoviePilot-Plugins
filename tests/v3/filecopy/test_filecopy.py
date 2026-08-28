from __future__ import annotations

import ast
import datetime
import json
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import filecopy as filecopy_module
from app.plugins.filecopy import FileCopy
from app.runtime.extensions.plugin.projection import PluginProjection


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    source = (REPOSITORY_ROOT / "plugins.v3/filecopy/__init__.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


@pytest.fixture
def plugin(tmp_path: Path) -> FileCopy:
    """构造使用临时源目录和目标目录的插件实例。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    instance = FileCopy()
    instance.init_plugin(
        {
            "enabled": True,
            "monitor_dirs": f"{source}:{target}",
            "rmt_mediaext": ".nfo",
            "delay": "invalid",
        }
    )
    return instance


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["FileCopy"]
    legacy_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["FileCopy"]

    assert manifest["version"] == FileCopy.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_v1["v3"] is False

    imports = _imports()
    assert {"app.sdk.config", "app.sdk.logging", "app.sdk.utilities"}.issubset(
        imports
    )
    source = (REPOSITORY_ROOT / "plugins.v3/filecopy/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "BackgroundScheduler" not in source
    assert "app.core" not in source
    assert "app.log" not in source
    assert "app.utils" not in source
    assert ".replace(" not in source


def test_get_service_uses_host_scheduler_and_keeps_once_pending(tmp_path: Path) -> None:
    """周期和一次性任务均应交由宿主注册，一次性状态执行前保持待消费。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "cron": "*/5 * * * *",
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )

    services = plugin.get_service()
    assert len(services) == 2
    cron_service = next(service for service in services if service["id"] == "FileCopy")
    once_service = next(
        service for service in services if service["id"] == "FileCopyOnce"
    )
    assert cron_service["kwargs"] == {}
    assert once_service["trigger"] == "date"
    assert isinstance(once_service["kwargs"]["run_date"], datetime.datetime)
    assert once_service["func"] == plugin._run_once_copy
    assert plugin._onlyonce is True


def test_onlyonce_only_plugin_is_visible_through_host_projection(tmp_path: Path) -> None:
    """仅请求一次性执行时，宿主投影仍应暴露 date 服务。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "enabled": False,
            "onlyonce": True,
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )

    projection = PluginProjection({"FileCopy": plugin})

    assert plugin.get_state() is True
    services = projection.services("FileCopy")
    assert [service["id"] for service in services] == ["FileCopyOnce"]


def test_enabled_plugin_declares_one_startup_date_service(tmp_path: Path) -> None:
    """启用插件后应保留旧版的延迟初始全量扫描，但只声明一个 date 服务。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "enabled": True,
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )

    services = plugin.get_service()
    assert [service["id"] for service in services] == ["FileCopyStartup"]
    assert services[0]["trigger"] == "date"
    assert isinstance(services[0]["kwargs"]["run_date"], datetime.datetime)


def test_relative_mapping_rejects_prefix_collision(tmp_path: Path) -> None:
    """路径前缀相同但不属于源目录的文件不得映射到目标目录。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    sibling = tmp_path / "source-sibling" / "movie.nfo"
    sibling.parent.mkdir(parents=True)
    sibling.touch()

    assert FileCopy._map_target_path(source, target, sibling) is None


def test_copy_preserves_relative_tree_and_does_not_overwrite(
    plugin: FileCopy, tmp_path: Path
) -> None:
    """复制使用相对目录映射，并跳过已存在的目标文件。"""
    source = tmp_path / "source"
    target = tmp_path / "target"
    source_file = source / "nested" / "movie.nfo"
    source_file.parent.mkdir()
    source_file.write_text("new", encoding="utf-8")
    target_file = target / "nested" / "movie.nfo"
    target_file.parent.mkdir()
    target_file.write_text("old", encoding="utf-8")

    assert plugin.copy_files() is True
    assert target_file.read_text(encoding="utf-8") == "old"

    target_file.unlink()
    assert plugin.copy_files() is True
    assert target_file.read_text(encoding="utf-8") == "new"


def test_copy_failure_is_reported_as_failure(plugin: FileCopy, monkeypatch) -> None:
    """宿主复制返回失败状态时，全量任务不得伪报成功。"""
    monkeypatch.setattr(
        filecopy_module.SystemUtils,
        "copy",
        lambda _source, _target: (-1, "permission denied"),
    )
    source = Path(next(iter(plugin._dirconf)))
    file_path = source / "failure.nfo"
    file_path.write_text("content", encoding="utf-8")

    assert plugin.copy_files() is False


def test_random_delay_counts_only_copy_attempts(plugin: FileCopy, monkeypatch) -> None:
    """目标已存在的文件跳过时，不应消耗复制延时计数。"""
    source = Path(next(iter(plugin._dirconf)))
    target = plugin._dirconf[str(source)]
    existing_source = source / "existing.nfo"
    existing_source.write_text("existing", encoding="utf-8")
    (target / existing_source.name).write_text("target", encoding="utf-8")
    pending_source = source / "pending.nfo"
    pending_source.write_text("pending", encoding="utf-8")
    plugin._delay = "1,1-1"

    randint = Mock(return_value=1)
    sleep = Mock()
    monkeypatch.setattr(filecopy_module.random, "randint", randint)
    monkeypatch.setattr(filecopy_module.time, "sleep", sleep)

    assert plugin.copy_files() is True
    randint.assert_called_once_with(1, 1)
    sleep.assert_called_once_with(1)


@pytest.mark.parametrize(
    "value",
    [None, "", "bad", "1", "0,1", "2,1-2-3", "2,3-1", "2,-1"],
)
def test_invalid_delay_is_ignored(value) -> None:
    """非法延时配置应被忽略，不能阻断插件初始化或复制。"""
    assert FileCopy._parse_delay(value) is None


def test_once_is_consumed_during_execution_and_lock_is_released(tmp_path: Path) -> None:
    """一次性任务执行时清除配置，异常路径也释放 single-flight 门禁。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "onlyonce": True,
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )
    plugin.update_config = Mock(return_value=True)

    assert plugin.copy_files(once=True) is True
    assert plugin._onlyonce is False
    plugin.update_config.assert_called_once()
    assert plugin._run_lock.acquire(blocking=False) is True
    plugin._run_lock.release()


def test_once_waits_for_running_copy_instead_of_being_lost(tmp_path: Path) -> None:
    """一次性 date 回调遇到周期任务时应等待锁并最终执行。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "onlyonce": True,
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )
    plugin.update_config = Mock(return_value=True)
    assert plugin._run_lock.acquire(blocking=False) is True

    finished = threading.Event()
    result: list[bool] = []

    def run_once() -> None:
        result.append(plugin.copy_files(once=True))
        finished.set()

    worker = threading.Thread(target=run_once)
    worker.start()
    try:
        assert finished.wait(timeout=0.05) is False
        assert plugin._onlyonce is True
    finally:
        plugin._run_lock.release()

    assert finished.wait(timeout=1) is True
    worker.join(timeout=1)
    assert result == [True]
    assert plugin._onlyonce is False
    plugin.update_config.assert_called_once()


@pytest.mark.parametrize("save_result", [False, RuntimeError("save failed")])
def test_once_does_not_run_when_consumption_cannot_persist(
    tmp_path: Path, save_result
) -> None:
    """一次性状态写回失败时保留请求，且向宿主返回标准失败元组。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {
            "onlyonce": True,
            "monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}",
        }
    )
    if isinstance(save_result, Exception):
        plugin.update_config = Mock(side_effect=save_result)
    else:
        plugin.update_config = Mock(return_value=save_result)

    assert plugin._run_once_copy() == (False, "一次性文件复制任务执行失败")
    assert plugin._onlyonce is True


def test_running_copy_is_skipped(tmp_path: Path) -> None:
    """并发调用只允许一个任务进入复制逻辑。"""
    plugin = FileCopy()
    plugin.init_plugin(
        {"monitor_dirs": f"{tmp_path / 'source'}:{tmp_path / 'target'}"}
    )
    assert plugin._run_lock.acquire(blocking=False) is True
    try:
        assert plugin.copy_files() is False
    finally:
        plugin._run_lock.release()
