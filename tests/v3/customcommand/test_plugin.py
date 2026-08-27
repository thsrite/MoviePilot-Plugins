from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from apscheduler.triggers.cron import CronTrigger
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import customcommand as customcommand_module
from app.plugins.customcommand import CommandResult, CommandTask, CustomCommand
from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.types import MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "customcommand" / "__init__.py"


@pytest.fixture
def plugin() -> CustomCommand:
    """构造不访问真实配置与数据目录的插件实例。"""
    instance = CustomCommand()
    instance.systemmessage = Mock()
    instance.get_data = Mock(return_value=[])
    instance.save_data = Mock()
    return instance


def test_manifest_and_strict_v3_import_contract() -> None:
    """版本、代际路由和稳定 SDK 导入必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CustomCommand"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["CustomCommand"]

    assert manifest["version"] == CustomCommand.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy["v3"] is False

    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"app.sdk.config", "app.sdk.logging"}.issubset(imports)
    forbidden = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden) for module in imports)
    assert "BackgroundScheduler" not in PLUGIN_PATH.read_text(encoding="utf-8")


def test_services_isolate_invalid_lines_and_use_host_scheduler(
    plugin: CustomCommand,
) -> None:
    """错误行不得阻断合法 cron，随机延时在注册阶段完成校验。"""
    plugin.init_plugin({
        "enabled": True,
        "time_confs": (
            "备份#0 1 * * *#echo ok#1-3\n"
            "错误行\n"
            "坏cron#not cron#echo bad\n"
            "坏延时#0 2 * * *#echo bad#3-1\n"
            "# 注释"
        ),
    })

    services = plugin.get_service()

    assert len(services) == 1
    service = services[0]
    assert service["id"] == "CustomCommand.1"
    assert service["name"] == "备份（随机延时 1-3 秒）"
    assert isinstance(service["trigger"], CronTrigger)
    assert str(service["trigger"].timezone) == "Asia/Shanghai"
    assert service["func"] == plugin._run_periodic_task
    assert service["kwargs"] == {}
    assert service["func_kwargs"]["task"].command == "echo ok"
    assert plugin.systemmessage.put.call_count == 3


def test_onlyonce_projects_date_service_and_preserves_periodic_services(
    plugin: CustomCommand,
) -> None:
    """一次性状态必须可被宿主投影，同时不隐藏已启用的周期任务。"""
    plugin.init_plugin({
        "enabled": True,
        "onlyonce": True,
        "time_confs": "任务#0 1 * * *#echo ok",
    })

    services = PluginProjection({"CustomCommand": plugin}).services()

    assert [service["id"] for service in services] == [
        "CustomCommand.Once",
        "CustomCommand.1",
    ]
    assert services[0]["trigger"] == "date"
    assert isinstance(services[0]["kwargs"]["run_date"], datetime)
    assert plugin._run_once is True
    assert plugin._onlyonce is True


def test_onlyonce_waits_for_slot_then_consumes_full_config(
    plugin: CustomCommand,
) -> None:
    """一次性任务必须先取得执行槽，持久化成功后再执行全部任务。"""
    plugin.init_plugin({
        "enabled": True,
        "onlyonce": True,
        "notify": True,
        "msgtype": "Manual",
        "history_days": 5,
        "notify_keywords": "ok",
        "time_confs": "一#0 1 * * *#echo one\n二#0 2 * * *#echo two#1-9",
    })
    plugin.update_config = Mock(return_value=True)
    plugin._run_task = Mock(
        return_value=CommandResult(returncode=0, stdout="ok\n")
    )
    plugin._run_lock.acquire()
    result: dict[str, object] = {}
    started = threading.Event()

    def invoke() -> None:
        started.set()
        result["value"] = plugin._run_once_tasks()

    worker = threading.Thread(target=invoke)
    worker.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    plugin.update_config.assert_not_called()

    plugin._run_lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["value"] is True
    plugin.update_config.assert_called_once_with({
        "enabled": True,
        "onlyonce": False,
        "notify": True,
        "msgtype": "Manual",
        "time_confs": "一#0 1 * * *#echo one\n二#0 2 * * *#echo two#1-9",
        "history_days": 5,
        "notify_keywords": "ok",
        "clear": False,
    })
    assert [call.args[0].name for call in plugin._run_task.call_args_list] == ["一", "二"]
    assert all(call.kwargs == {"apply_delay": False} for call in plugin._run_task.call_args_list)
    assert plugin._run_once is False
    assert plugin._onlyonce is False


@pytest.mark.parametrize("persisted", [False, RuntimeError("storage unavailable")])
def test_onlyonce_persistence_failure_keeps_pending_state(
    plugin: CustomCommand,
    persisted: object,
) -> None:
    """配置回写失败时不得执行或消费一次性任务。"""
    plugin.init_plugin({
        "onlyonce": True,
        "time_confs": "任务#0 1 * * *#echo ok",
    })
    if isinstance(persisted, Exception):
        plugin.update_config = Mock(side_effect=persisted)
    else:
        plugin.update_config = Mock(return_value=persisted)
    plugin._run_task = Mock()

    result = plugin._run_once_tasks()

    assert result[0] is False
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin._run_task.assert_not_called()


def test_process_streams_output_and_keeps_stdout_last_line(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """两个输出管道应持续消费，历史显示保持 stdout 最后一行优先。"""
    process = SimpleNamespace(
        returncode=3,
        stdout=io.StringIO("first\nlast\n"),
        stderr=io.StringIO("error\n"),
        wait=Mock(return_value=None),
    )
    popen = Mock(return_value=process)
    monkeypatch.setattr(customcommand_module.subprocess, "Popen", popen)

    result = plugin._execute_process("demo")

    assert result.success is False
    assert result.message == "last"
    assert popen.call_args.args == ("demo",)
    assert popen.call_args.kwargs == {
        "shell": True,
        "stdout": customcommand_module.subprocess.PIPE,
        "stderr": customcommand_module.subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        **(
            {"creationflags": getattr(customcommand_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        ),
    }
    process.wait.assert_called_once_with()


def test_output_is_truncated_without_unbounded_result(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """超长 stdout/stderr 只保留有限尾部并标注截断。"""
    process = SimpleNamespace(
        returncode=0,
        stdout=io.StringIO("head\n" + "x" * (customcommand_module.MAX_OUTPUT_SIZE + 100) + "\nlast\n"),
        stderr=io.StringIO("error\n" + "y" * (customcommand_module.MAX_OUTPUT_SIZE + 100)),
        wait=Mock(return_value=None),
    )
    monkeypatch.setattr(customcommand_module.subprocess, "Popen", Mock(return_value=process))

    result = plugin._execute_process("demo")

    assert len(result.stdout) <= customcommand_module.MAX_OUTPUT_SIZE
    assert len(result.stderr) <= customcommand_module.MAX_OUTPUT_SIZE
    assert result.stdout.startswith("[输出已截断]\n")
    assert result.stderr.startswith("[输出已截断]\n")
    assert result.stdout.endswith("last\n")


def test_stop_service_terminates_long_command_and_clears_handle(
    plugin: CustomCommand,
) -> None:
    """长命令应可由 stop_service 收敛，且执行结束后不残留句柄。"""
    command = subprocess.list2cmdline([
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    ])
    result: dict[str, CommandResult] = {}

    worker = threading.Thread(
        target=lambda: result.update(value=plugin._execute_process(command)),
    )
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with plugin._process_lock:
                if plugin._process is not None:
                    break
            time.sleep(0.01)
        with plugin._process_lock:
            assert plugin._process is not None

        plugin.stop_service()
        worker.join(timeout=5)

        assert not worker.is_alive()
        assert plugin._process is None
        assert result["value"].returncode != 0
    finally:
        plugin.stop_service()
        worker.join(timeout=5)


def test_natural_exit_does_not_terminate_or_clear_replacement_process(
    plugin: CustomCommand,
) -> None:
    """自然退出的旧进程不应误杀或清除后来登记的新进程。"""
    old_process = SimpleNamespace(
        pid=111,
        poll=Mock(return_value=0),
        terminate=Mock(),
        kill=Mock(),
    )
    new_process = SimpleNamespace(pid=222)
    with plugin._process_lock:
        plugin._process = old_process

    plugin.stop_service()
    with plugin._process_lock:
        plugin._process = new_process
    plugin._clear_process(old_process)

    old_process.terminate.assert_not_called()
    old_process.kill.assert_not_called()
    assert plugin._process is new_process


def test_stop_service_escalates_after_timeout_and_clears_handle(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """首次终止未收敛时应升级信号，并在有限等待后清理句柄。"""
    process = SimpleNamespace(
        pid=123,
        poll=Mock(return_value=None),
        wait=Mock(
            side_effect=[
                subprocess.TimeoutExpired("demo", customcommand_module.PROCESS_STOP_TIMEOUT),
                None,
            ]
        ),
    )
    killpg = Mock()
    monkeypatch.setattr(customcommand_module.os, "killpg", killpg)
    with plugin._process_lock:
        plugin._process = process

    plugin.stop_service()

    assert killpg.call_args_list == [
        call(123, customcommand_module.signal.SIGTERM),
        call(123, customcommand_module.signal.SIGKILL),
    ]
    assert process.wait.call_args_list == [
        call(timeout=customcommand_module.PROCESS_STOP_TIMEOUT),
        call(timeout=customcommand_module.PROCESS_STOP_TIMEOUT),
    ]
    assert plugin._process is None


def test_stop_service_keeps_handle_when_force_kill_does_not_converge(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """两轮停止都超时时保留句柄，使后续生命周期仍能重试收敛。"""
    timeout = subprocess.TimeoutExpired(
        "demo", customcommand_module.PROCESS_STOP_TIMEOUT
    )
    process = SimpleNamespace(
        pid=321,
        poll=Mock(return_value=None),
        wait=Mock(side_effect=[timeout, timeout]),
    )
    monkeypatch.setattr(customcommand_module.os, "killpg", Mock())
    with plugin._process_lock:
        plugin._process = process

    assert plugin.stop_service() is False

    assert plugin._process is process


def test_disable_cancels_task_during_delay_before_process_start(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """任务取得执行槽但尚未创建进程时，禁用必须中断延时并阻止启动。"""
    plugin.init_plugin({"enabled": True})
    execute = Mock()
    monkeypatch.setattr(plugin, "_execute_process", execute)
    monkeypatch.setattr(customcommand_module.random, "randint", Mock(return_value=60))
    task = CommandTask(
        name="delayed",
        cron="0 0 * * *",
        command="echo late",
        random_delay="60-60",
        line_number=1,
    )
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(value=plugin._run_periodic_task(task))
    )
    worker.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and plugin._run_lock.acquire(blocking=False):
        plugin._run_lock.release()
        time.sleep(0.01)

    plugin.init_plugin({"enabled": False})
    worker.join(timeout=2)

    assert not worker.is_alive()
    execute.assert_not_called()
    assert result["value"] == (False, "任务已停止")


def test_init_plugin_stops_running_process_before_disabling(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """同实例禁用必须走真实停止路径收敛旧进程并清理句柄。"""
    process = SimpleNamespace(
        pid=456,
        poll=Mock(return_value=None),
        wait=Mock(return_value=None),
    )
    terminate = Mock()
    monkeypatch.setattr(plugin, "_terminate_process", terminate)
    with plugin._process_lock:
        plugin._process = process

    plugin.init_plugin({"enabled": False, "time_confs": "任务#0 1 * * *#echo ok"})

    terminate.assert_called_once_with(process, force=False)
    process.wait.assert_called_once_with(timeout=customcommand_module.PROCESS_STOP_TIMEOUT)
    assert plugin._process is None
    assert plugin.get_state() is False


def test_init_plugin_rejects_config_switch_when_process_does_not_stop(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """旧进程未收敛时不得清除停止态或应用下一套命令配置。"""
    plugin._enabled = True
    plugin._time_confs = "old#0 0 * * *#echo old"
    timeout = subprocess.TimeoutExpired(
        "demo", customcommand_module.PROCESS_STOP_TIMEOUT
    )
    process = SimpleNamespace(
        pid=654,
        poll=Mock(return_value=None),
        wait=Mock(side_effect=[timeout, timeout]),
    )
    monkeypatch.setattr(customcommand_module.os, "killpg", Mock())
    with plugin._process_lock:
        plugin._process = process

    plugin.init_plugin(
        {"enabled": True, "time_confs": "new#0 1 * * *#echo new"}
    )

    assert plugin._process is process
    assert plugin._stop_event.is_set()
    assert plugin._time_confs == "old#0 0 * * *#echo old"


def test_run_task_saves_history_and_filters_notification(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CustomCommand,
) -> None:
    """执行结果应写入历史，并按关键词和消息类型发送通知。"""
    plugin.init_plugin({
        "notify": True,
        "msgtype": "Plugin",
        "notify_keywords": "completed",
        "history_days": 30,
    })
    plugin.post_message = Mock()
    monkeypatch.setattr(
        plugin,
        "_execute_process",
        Mock(return_value=CommandResult(returncode=0, stdout="completed\n")),
    )
    task = customcommand_module.CommandTask(1, "备份", "0 1 * * *", "echo ok")

    result = plugin._run_task(task, apply_delay=False)

    assert result.success is True
    saved = plugin.save_data.call_args.kwargs["value"]
    assert saved[-1]["name"] == "备份"
    assert saved[-1]["command"] == "echo ok"
    assert saved[-1]["result"] == "completed"
    plugin.post_message.assert_called_once_with(
        title="备份",
        mtype=MessageType.Plugin,
        text="completed",
    )


def test_periodic_single_flight_skips_overlap(plugin: CustomCommand) -> None:
    """周期任务重叠时不得启动第二个命令。"""
    task = customcommand_module.CommandTask(1, "备份", "0 1 * * *", "echo ok")
    plugin._run_task = Mock()
    assert plugin._run_lock.acquire(blocking=False) is True
    try:
        result = plugin._run_periodic_task(task)
    finally:
        plugin._run_lock.release()

    assert result[0] is False
    assert "已跳过" in result[1]
    plugin._run_task.assert_not_called()


def test_once_isolates_task_side_effect_failure(plugin: CustomCommand) -> None:
    """单个任务的历史或通知异常不得阻断其它已消费的一次性任务。"""
    plugin.init_plugin({
        "onlyonce": True,
        "time_confs": "一#0 1 * * *#echo one\n二#0 2 * * *#echo two",
    })
    plugin.update_config = Mock(return_value=True)
    plugin._run_task = Mock(
        side_effect=[RuntimeError("history unavailable"), CommandResult(returncode=0)]
    )

    result = plugin._run_once_tasks()

    assert result == (False, "一次性任务执行失败：一")
    assert plugin._run_task.call_count == 2
    assert plugin._run_lock.acquire(blocking=False) is True
    plugin._run_lock.release()


def test_clear_history_and_static_surfaces(plugin: CustomCommand) -> None:
    """清理开关应立即消费，静态接口和页面保持稳定结构。"""
    plugin.del_data = Mock()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin({"clear": True, "history_days": "invalid"})

    plugin.del_data.assert_called_once_with("history")
    assert plugin.update_config.call_args.args[0]["clear"] is False
    assert plugin.update_config.call_args.args[0]["history_days"] == 30
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page()[0]["text"] == "暂无数据"
    assert plugin.get_form()[1]["history_days"] == 30
    assert plugin.stop_service() is None
