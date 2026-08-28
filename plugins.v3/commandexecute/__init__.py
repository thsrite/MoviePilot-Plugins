"""提供受宿主调度器管理的容器命令执行能力。"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz

from app.plugins import _PluginBase
from app.schemas.types import EventType, MessageChannel
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger


@dataclass(frozen=True)
class CommandExecutionResult:
    """保存单条命令的可审计执行结果。"""

    command: str
    output: str = ""
    returncode: Optional[int] = None
    timed_out: bool = False
    error: Optional[str] = None
    skipped: bool = False

    @property
    def success(self) -> bool:
        """只有命令正常退出且返回码为零时才视为成功。"""
        return (
            not self.timed_out
            and not self.skipped
            and self.error is None
            and self.returncode == 0
        )


class CommandExecute(_PluginBase):
    """在宿主容器中执行显式 shell 命令并返回完整结果。"""

    plugin_name = "命令执行器"
    plugin_desc = "自定义容器命令执行。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/command.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "commandexecute_"
    plugin_order = 99
    auth_level = 1

    # 命令能力是明确的 shell 能力，超时上限避免子进程长期占用宿主执行器。
    COMMAND_TIMEOUT = 60
    # 给进程组一个短暂的优雅退出窗口，随后用强制信号收口。
    PROCESS_TERMINATION_GRACE = 1

    def __init__(self) -> None:
        super().__init__()
        self._onlyonce = False
        self._run_once = False
        self._command = ""
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置；一次性任务由宿主 date 服务在执行时消费。"""
        config = dict(config or {})
        self._command = str(config.get("command") or "")
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce

    def get_state(self) -> bool:
        """命令执行器始终可用，待执行的一次性任务也保持可见。"""
        return True

    @staticmethod
    def _normalise_stream(value: Any) -> str:
        """将 subprocess 文本或字节输出转换为可合并的文本。"""
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @classmethod
    def _merge_output(cls, stdout: Any, stderr: Any) -> str:
        """按 stdout、stderr 的固定顺序合并两个捕获流，保留内部空行。"""
        streams = [
            cls._normalise_stream(stdout),
            cls._normalise_stream(stderr),
        ]
        return "\n".join(stream.rstrip("\r\n") for stream in streams if stream)

    @staticmethod
    def _popen_options() -> Dict[str, Any]:
        """为命令建立独立进程组，确保超时可回收派生进程。"""
        options: Dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        return options

    @staticmethod
    def _process_group_id(process: subprocess.Popen[Any]) -> Optional[int]:
        """读取独立会话的进程组 ID，供父 shell 退出后的清理使用。"""
        if os.name == "nt":
            return None
        try:
            return os.getpgid(process.pid)
        except (AttributeError, OSError):
            # start_new_session 建立的会话以 Popen 返回的 PID 为进程组 ID；
            # 读取失败时仍保留该 ID，避免父 shell 退出后无法清理派生进程。
            return process.pid

    @classmethod
    def _terminate_process_group(
            cls,
            process: subprocess.Popen[Any],
            process_group_id: Optional[int] = None,
    ) -> None:
        """终止并回收命令的整个进程组，而不是只杀掉 shell 父进程。"""
        if os.name == "nt":
            # CTRL_BREAK 允许进程自行清理；taskkill 的 /T 作为兜底覆盖不响应
            # 控制事件的派生进程。两者都针对独立的 CREATE_NEW_PROCESS_GROUP。
            try:
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            except (OSError, ValueError):
                pass
            try:
                killer = subprocess.Popen(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    killer.communicate(timeout=cls.PROCESS_TERMINATION_GRACE)
                except subprocess.TimeoutExpired:
                    killer.kill()
                    killer.communicate()
            except OSError:
                pass
            try:
                process.wait(timeout=cls.PROCESS_TERMINATION_GRACE)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if process.poll() is None:
                process.kill()
            return

        if process_group_id is None:
            process_group_id = cls._process_group_id(process)
        if process_group_id is None:
            # 进程已退出且无法取得其会话 ID 时，不能猜测 PID，避免误杀复用的
            # 进程；父进程仍会由 _reap_process 回收。
            return

        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            process.wait(timeout=cls.PROCESS_TERMINATION_GRACE)
        except (subprocess.TimeoutExpired, OSError):
            pass

        # 父 shell 可能已经退出但子进程仍存活，因此即使 wait 成功也必须
        # 再次针对原进程组发送 SIGKILL，避免管道和外部副作用继续存在。
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        if process.poll() is None:
            process.kill()

    @classmethod
    def _reap_process(cls, process: subprocess.Popen[Any]) -> Tuple[Any, Any]:
        """等待进程退出并收集管道，避免超时清理遗留僵尸进程。"""
        try:
            return process.communicate(timeout=cls.PROCESS_TERMINATION_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.communicate()

    def _execute_one(self, command: str) -> CommandExecutionResult:
        """执行一条 shell 命令并记录返回码、输出和超时状态。"""
        process: Optional[subprocess.Popen[Any]] = None
        process_group_id: Optional[int] = None
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._popen_options(),
            )
            process_group_id = self._process_group_id(process)
            try:
                stdout, stderr = process.communicate(timeout=self.COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                try:
                    self._terminate_process_group(process, process_group_id)
                except Exception as cleanup_error:  # noqa: BLE001 - 清理仍需返回超时结果
                    logger.error("命令进程组清理失败：%s，%s", command, cleanup_error)
                try:
                    stdout, stderr = self._reap_process(process)
                except Exception as cleanup_error:  # noqa: BLE001 - 保留已捕获的超时结果
                    logger.error("命令进程回收失败：%s，%s", command, cleanup_error)
                    stdout, stderr = error.stdout, error.stderr
                output = self._merge_output(stdout, stderr)
                if not output:
                    output = self._merge_output(error.stdout, error.stderr)
                result = CommandExecutionResult(
                    command=command,
                    output=output,
                    timed_out=True,
                    error=f"命令执行超时（{self.COMMAND_TIMEOUT}秒）",
                )
                logger.error("命令执行超时：%s", command)
                return result
        except subprocess.TimeoutExpired as error:
            # Popen 本身通常不会抛出 TimeoutExpired；保留该分支以兼容
            # 测试替身及未来的 subprocess 实现，并确保异常仍有可读结果。
            output = self._merge_output(error.stdout, error.stderr)
            result = CommandExecutionResult(
                command=command,
                output=output,
                timed_out=True,
                error=f"命令执行超时（{self.COMMAND_TIMEOUT}秒）",
            )
            logger.error("命令执行超时：%s", command)
            return result
        except Exception as error:  # noqa: BLE001 - 子进程边界需将启动错误转为结果
            if process is not None:
                try:
                    self._terminate_process_group(process, process_group_id)
                except Exception as cleanup_error:  # noqa: BLE001 - 不覆盖原始启动错误
                    logger.error("命令进程组清理失败：%s，%s", command, cleanup_error)
                try:
                    self._reap_process(process)
                except Exception as cleanup_error:  # noqa: BLE001 - 不覆盖原始启动错误
                    logger.error("命令进程回收失败：%s，%s", command, cleanup_error)
            result = CommandExecutionResult(
                command=command,
                error=(
                    f"命令启动失败：{error}"
                    if process is None
                    else f"命令执行失败：{error}"
                ),
            )
            logger.error(
                "命令%s：%s，%s",
                "启动失败" if process is None else "执行失败",
                command,
                error,
            )
            return result

        output = self._merge_output(stdout, stderr)
        result = CommandExecutionResult(
            command=command,
            output=output,
            returncode=process.returncode if process is not None else None,
        )
        if result.success:
            logger.info("命令执行成功：%s，返回码=%s", command, result.returncode)
        else:
            logger.error("命令执行失败：%s，返回码=%s", command, result.returncode)
        if output:
            logger.info("命令输出：%s", output)
        return result

    def execute_command(self, command: str) -> CommandExecutionResult:
        """串行执行一条命令；已有命令运行时拒绝并发触发。"""
        if not isinstance(command, str) or not command.strip():
            return CommandExecutionResult(
                command=str(command or ""),
                error="命令不能为空",
            )
        if not self._run_lock.acquire(blocking=False):
            return CommandExecutionResult(
                command=command,
                error="已有命令正在执行，本次触发已跳过",
                skipped=True,
            )
        try:
            return self._execute_one(command)
        finally:
            self._run_lock.release()

    def _configured_commands(self, command: Optional[str] = None) -> List[str]:
        """拆分配置中的逐行命令并跳过空行。"""
        value = self._command if command is None else command
        return [line.strip() for line in str(value or "").splitlines() if line.strip()]

    def _execute_configured_commands(
            self,
            command: Optional[str] = None,
            *,
            lock_held: bool = False,
    ) -> List[CommandExecutionResult]:
        """在同一个 single-flight 窗口中顺序执行配置里的命令。"""
        commands = self._configured_commands(command)
        if not commands:
            return []
        if not lock_held and not self._run_lock.acquire(blocking=False):
            return [
                CommandExecutionResult(
                    command="\n".join(commands),
                    error="已有命令正在执行，本次触发已跳过",
                    skipped=True,
                )
            ]
        try:
            return [self._execute_one(item) for item in commands]
        finally:
            if not lock_held:
                self._run_lock.release()

    def _consume_once(self) -> Optional[str]:
        """持久化一次性开关成功后再消费内存状态，失败时保留待执行意图。"""
        with self._once_lock:
            if not self._run_once:
                return None
            command = self._command
            try:
                persisted = self.update_config({
                    "onlyonce": False,
                    "command": command,
                })
            except Exception as error:  # noqa: BLE001 - 状态持久化失败不应重复执行命令
                logger.error("保存一次性命令状态失败：%s", error)
                return None
            if persisted is False:
                logger.error("保存一次性命令状态失败：宿主拒绝更新配置")
                return None
            self._run_once = False
            self._onlyonce = False
            return command

    def _run_once_execute(self) -> bool | Tuple[bool, str]:
        """等待命令执行槽后消费一次性标志，并按宿主合同返回失败消息。"""
        # 一次性任务不能因 /cmd 正在执行而被非阻塞跳过；持有同一把锁覆盖
        # 状态消费和全部命令，确保每个成功消费的意图最终都对应一次执行。
        self._run_lock.acquire()
        try:
            command = self._consume_once()
            if command is None:
                return False, "一次性命令未处于待执行状态，或状态保存失败"
            results = self._execute_configured_commands(command, lock_held=True)
            if not results:
                logger.error("一次性命令配置为空，未执行任何命令")
                return False, "一次性命令配置为空，未执行任何命令"
            if not all(result.success for result in results):
                logger.error("一次性命令执行失败")
                return False, "一次性命令执行失败"
            return True
        except Exception as error:  # noqa: BLE001 - 调度器需收到标准失败结果
            logger.error("一次性命令执行失败：%s", error)
            return False, f"一次性命令执行失败：{error}"
        finally:
            self._run_lock.release()

    @staticmethod
    def _format_result(result: CommandExecutionResult) -> str:
        """格式化面向用户的结果，明确状态、返回码、超时和空输出。"""
        status = "成功" if result.success else "失败"
        returncode = "无" if result.returncode is None else str(result.returncode)
        lines = [
            f"命令：{result.command}",
            f"状态：{status}",
            f"返回码：{returncode}",
        ]
        if result.timed_out:
            lines.append("原因：执行超时")
        if result.error:
            lines.append(f"原因：{result.error}")
        lines.extend(["输出：", result.output or "（无输出）"])
        return "\n".join(lines)

    @staticmethod
    def _is_telegram(channel: Any) -> bool:
        """兼容事件载荷中的枚举渠道和值渠道。"""
        return getattr(channel, "value", channel) == MessageChannel.Telegram.value

    def _reply(self, event_data: dict, result: CommandExecutionResult) -> None:
        """向交互命令发起者返回与执行状态一致的通知。"""
        channel = event_data.get("channel")
        text = self._format_result(result)
        if self._is_telegram(channel):
            text = f"```plaintext\n{text}\n```"
        self.post_message(
            channel=channel,
            title="命令执行成功" if result.success else "命令执行失败",
            text=text,
            userid=event_data.get("user"),
        )

    @eventmanager.register(EventType.PluginAction)
    def execute(self, event: Optional[Event] = None) -> Optional[CommandExecutionResult]:
        """响应 `/cmd` 插件动作并回复准确的成功或失败状态。"""
        if event is None or not isinstance(event.event_data, dict):
            return None
        event_data = event.event_data
        if event_data.get("action") != "command_execute":
            return None
        args = event_data.get("arg_str")
        if not isinstance(args, str) or not args.strip():
            result = CommandExecutionResult(command=str(args or ""), error="命令不能为空")
        else:
            result = self.execute_command(args)
        self._reply(event_data, result)
        return result

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令。"""
        return [{
            "cmd": "/cmd",
            "event": EventType.PluginAction,
            "desc": "自定义命令执行",
            "category": "",
            "data": {"action": "command_execute"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """插件不暴露额外 HTTP API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """通过 V3 宿主 date 服务注册一次性命令任务。"""
        if not self._run_once:
            return []
        timezone = pytz.timezone(str(settings.TZ))
        return [{
            "id": "CommandExecute.Once",
            "name": "命令执行器（立即运行）",
            "trigger": "date",
            "func": self._run_once_execute,
            "kwargs": {
                "run_date": datetime.now(tz=timezone) + timedelta(seconds=3),
            },
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回命令开关和逐行命令配置表单。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "执行命令",
                                    },
                                }],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "command",
                                        "rows": "2",
                                        "label": "command命令",
                                        "placeholder": "一行一条",
                                    },
                                }],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {"type": "info", "variant": "tonal"},
                                    "content": [{
                                        "component": "span",
                                        "text": "执行日志将会输出到控制台，请谨慎操作。",
                                    }],
                                }],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {"type": "info", "variant": "tonal"},
                                    "content": [{
                                        "component": "span",
                                        "text": "可使用交互命令/cmd ls",
                                    }],
                                }],
                            }
                        ],
                    },
                ],
            }
        ], {"onlyonce": False, "command": ""}

    def get_page(self) -> List[dict]:
        """插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """插件没有自建后台资源，服务生命周期由宿主调度器管理。"""
        return None
