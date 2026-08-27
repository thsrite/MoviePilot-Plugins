from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
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
from app.plugins import commandexecute as commandexecute_module
from app.plugins.commandexecute import CommandExecute
from app.schemas.types import EventType, MessageChannel
from app.sdk.events import Event


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "commandexecute" / "__init__.py"

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
    """为插件基类提供隔离 Chain 上下文。"""
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


@pytest.fixture
def plugin() -> CommandExecute:
    """构造不访问真实配置数据库的插件实例。"""
    return CommandExecute()


def _imports() -> set[str]:
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _is_live_process(pid: int) -> bool:
    """判断真实派生进程是否仍在运行，忽略已退出但尚未回收的僵尸态。"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if os.name == "nt":
        return True
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = probe.stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引、源码版本和旧代回退开关必须一致。"""
    manifest = json.loads((REPOSITORY_ROOT / "package.v3.json").read_text())[
        "CommandExecute"
    ]
    package_manifest = json.loads((REPOSITORY_ROOT / "package.json").read_text())[
        "CommandExecute"
    ]
    legacy_manifest = json.loads((REPOSITORY_ROOT / "package.v2.json").read_text())[
        "CommandExecute"
    ]

    assert manifest["version"] == CommandExecute.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v2.0.0"]
    assert package_manifest["v3"] is False
    assert legacy_manifest["v3"] is False


def test_v3_source_uses_only_stable_event_and_logging_sdk() -> None:
    """事件和日志必须来自 V3 SDK，不能回退到旧入口。"""
    imports = _imports()
    assert {"app.sdk.events", "app.sdk.logging"}.issubset(imports)
    forbidden_prefixes = (
        "app.core",
        "app.helper",
        "app.log",
        "app.runtime",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "subprocess.Popen" in source
    assert "subprocess.run" not in source
    assert "stdout=subprocess.PIPE" in source
    assert "stderr=subprocess.PIPE" in source
    assert "text=True" in source
    assert "timeout=" in source
    assert "start_new_session" in source
    assert "os.killpg" in source


def test_execute_command_merges_output_and_reports_return_code(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CommandExecute,
) -> None:
    """命令结果应合并 stdout/stderr 并保留返回码。"""
    process = SimpleNamespace(
        pid=10_000_000,
        returncode=3,
        communicate=Mock(return_value=("标准输出\n", "标准错误\n")),
    )
    runner = Mock(return_value=process)
    monkeypatch.setattr(commandexecute_module.subprocess, "Popen", runner)
    monkeypatch.setattr(plugin, "_process_group_id", lambda _process: None)

    result = plugin.execute_command("printf output")

    assert result.success is False
    assert result.output == "标准输出\n标准错误"
    assert result.returncode == 3
    runner.assert_called_once_with(
        "printf output",
        shell=True,
        stdout=commandexecute_module.subprocess.PIPE,
        stderr=commandexecute_module.subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process.communicate.assert_called_once_with(timeout=CommandExecute.COMMAND_TIMEOUT)


def test_execute_command_reports_timeout_and_empty_output(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CommandExecute,
) -> None:
    """超时和空输出均应转成可读的失败结果。"""
    process = SimpleNamespace(
        pid=10_000_001,
        returncode=None,
        communicate=Mock(
            side_effect=[
                commandexecute_module.subprocess.TimeoutExpired(
                    "sleep 1", 60, output="", stderr=""
                ),
                ("", ""),
            ]
        ),
        wait=Mock(return_value=None),
        poll=Mock(side_effect=[None, None]),
        kill=Mock(),
    )
    monkeypatch.setattr(commandexecute_module.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(plugin, "_process_group_id", lambda _process: 10_000_001)
    kill_group = Mock()
    monkeypatch.setattr(
        commandexecute_module.os,
        "killpg",
        kill_group,
    )

    result = plugin.execute_command("sleep 1")

    assert result.timed_out is True
    assert result.returncode is None
    assert result.output == ""
    assert "超时" in plugin._format_result(result)
    assert "（无输出）" in plugin._format_result(result)
    assert kill_group.call_count == 2
    process.wait.assert_called_once_with(timeout=CommandExecute.PROCESS_TERMINATION_GRACE)


def test_event_reply_distinguishes_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CommandExecute,
) -> None:
    """交互命令成功和非零返回码必须使用不同通知标题。"""
    plugin.post_message = Mock()
    monkeypatch.setattr(
        plugin,
        "_execute_one",
        Mock(
            side_effect=[
                commandexecute_module.CommandExecutionResult(
                    command="echo ok", output="ok", returncode=0
                ),
                commandexecute_module.CommandExecutionResult(
                    command="false", output="bad", returncode=1
                ),
            ]
        ),
    )

    success = plugin.execute(
        Event(
            EventType.PluginAction,
            {
                "action": "command_execute",
                "arg_str": "echo ok",
                "channel": MessageChannel.Telegram,
                "user": "user-1",
            },
        )
    )
    failure = plugin.execute(
        Event(
            EventType.PluginAction,
            {
                "action": "command_execute",
                "arg_str": "false",
                "channel": MessageChannel.Telegram,
                "user": "user-1",
            },
        )
    )

    assert success is not None and success.success is True
    assert failure is not None and failure.success is False
    assert [call.kwargs["title"] for call in plugin.post_message.call_args_list] == [
        "命令执行成功",
        "命令执行失败",
    ]
    assert all(call.kwargs["text"].startswith("```plaintext\n") for call in plugin.post_message.call_args_list)


def test_single_flight_skips_overlapping_command(
    monkeypatch: pytest.MonkeyPatch,
    plugin: CommandExecute,
) -> None:
    """并发执行时第二次触发不得启动新的子进程。"""
    runner = Mock(
        return_value=commandexecute_module.CommandExecutionResult(
            command="echo blocked", returncode=0
        )
    )
    monkeypatch.setattr(plugin, "_execute_one", runner)
    assert plugin._run_lock.acquire(blocking=False) is True
    try:
        result = plugin.execute_command("echo blocked")
    finally:
        plugin._run_lock.release()

    assert result.skipped is True
    runner.assert_not_called()


def test_onlyonce_uses_host_date_service_and_consumes_at_execution(
    plugin: CommandExecute,
) -> None:
    """一次性命令应延迟到宿主 date 服务执行，并在执行入口原子消费。"""
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin({"onlyonce": True, "command": "\n echo one\n\n echo two "})

    services = plugin.get_service()

    assert len(services) == 1
    service = services[0]
    assert service["id"] == "CommandExecute.Once"
    assert service["trigger"] == "date"
    assert service["func"] == plugin._run_once_execute
    assert isinstance(service["kwargs"]["run_date"], datetime)
    assert plugin._onlyonce is True
    assert plugin._run_once is True

    plugin._execute_configured_commands = Mock(return_value=[])
    assert service["func"]() == (False, "一次性命令配置为空，未执行任何命令")
    assert plugin._onlyonce is False
    assert plugin._run_once is False
    plugin.update_config.assert_called_once_with({"onlyonce": False, "command": "\n echo one\n\n echo two "})
    assert service["func"]() == (False, "一次性命令未处于待执行状态，或状态保存失败")
    plugin.update_config.assert_called_once()


def test_onlyonce_waits_for_interactive_command_before_consuming(
    plugin: CommandExecute,
) -> None:
    """一次性任务必须等待 /cmd 释放执行槽，不能丢弃待执行意图。"""
    plugin.init_plugin({"onlyonce": True, "command": "echo once"})
    plugin.update_config = Mock(return_value=True)
    plugin._execute_configured_commands = Mock(
        return_value=[
            commandexecute_module.CommandExecutionResult(
                command="echo once", returncode=0
            )
        ]
    )
    plugin._run_lock.acquire()
    result: dict[str, Any] = {}
    started = threading.Event()

    def run_once() -> None:
        started.set()
        result["value"] = plugin._run_once_execute()

    worker = threading.Thread(target=run_once)
    worker.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    assert "value" not in result
    plugin.update_config.assert_not_called()

    plugin._run_lock.release()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result["value"] is True
    plugin.update_config.assert_called_once_with(
        {"onlyonce": False, "command": "echo once"}
    )
    plugin._execute_configured_commands.assert_called_once_with(
        "echo once", lock_held=True
    )


def test_onlyonce_reports_command_failure_to_scheduler(
    plugin: CommandExecute,
) -> None:
    """一次性命令非零退出时按调度器约定返回失败消息。"""
    plugin.init_plugin({"onlyonce": True, "command": "false"})
    plugin.update_config = Mock(return_value=True)
    plugin._execute_configured_commands = Mock(
        return_value=[
            commandexecute_module.CommandExecutionResult(
                command="false", returncode=1
            )
        ]
    )

    result = plugin._run_once_execute()

    assert result == (False, "一次性命令执行失败")
    assert plugin._onlyonce is False
    assert plugin._run_once is False


@pytest.mark.parametrize("update_result", [False, RuntimeError("storage unavailable")])
def test_onlyonce_persistence_failure_keeps_memory_state_and_skips_execution(
    plugin: CommandExecute,
    update_result: object,
) -> None:
    """状态回写失败时保留待执行标志，避免内存与存储状态分叉。"""
    plugin.init_plugin({"onlyonce": True, "command": "echo once"})
    if isinstance(update_result, Exception):
        plugin.update_config = Mock(side_effect=update_result)
    else:
        plugin.update_config = Mock(return_value=update_result)
    plugin._execute_configured_commands = Mock()

    result = plugin._run_once_execute()

    assert result[0] is False
    assert "状态保存失败" in result[1]
    assert plugin._onlyonce is True
    assert plugin._run_once is True
    plugin._execute_configured_commands.assert_not_called()


def test_timeout_terminates_real_derived_process(
    plugin: CommandExecute,
    tmp_path: Path,
) -> None:
    """真实派生子进程不得在 shell 超时后继续运行。"""
    plugin.COMMAND_TIMEOUT = 0.2
    child_pid_path = tmp_path / "child.pid"
    survivor_path = tmp_path / "survived"
    child_code = (
        "import os,time;"
        f"open({str(child_pid_path)!r}, 'w', encoding='utf-8').write(str(os.getpid()));"
        "time.sleep(1);"
        f"open({str(survivor_path)!r}, 'w', encoding='utf-8').write('survived')"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]);"
        "time.sleep(10)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"

    result = plugin.execute_command(command)

    assert result.timed_out is True
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while _is_live_process(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_live_process(child_pid)
    time.sleep(1.1)
    assert not survivor_path.exists()
