"""提供由宿主调度器管理的自定义命令任务。"""

from __future__ import annotations

import os
import random
import re
import signal
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.logging import logger


MAX_OUTPUT_SIZE = 64 * 1024
PROCESS_STOP_TIMEOUT = 1.0
OUTPUT_READ_SIZE = 4096


class _BoundedOutput:
    """保留命令输出的有限尾部，避免长时间运行任务耗尽内存。"""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._chunks: deque[str] = deque()
        self._size = 0
        self._truncated = False
        self._lock = threading.Lock()

    def append(self, chunk: str) -> None:
        """追加一段输出，并在超限时丢弃最早内容。"""
        if not chunk:
            return
        with self._lock:
            if len(chunk) > self._limit:
                self._chunks.clear()
                self._chunks.append(chunk[-self._limit:])
                self._size = self._limit
                self._truncated = True
                return
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self._limit:
                removed = self._chunks.popleft()
                overflow = self._size - self._limit
                if len(removed) <= overflow:
                    self._size -= len(removed)
                else:
                    self._chunks.appendleft(removed[overflow:])
                    self._size -= overflow
                self._truncated = True

    def getvalue(self) -> str:
        """返回不超过限制的输出尾部，并标识发生过截断。"""
        with self._lock:
            value = "".join(self._chunks)
            if not self._truncated:
                return value
            marker = "[输出已截断]\n"
            return marker + value[-(self._limit - len(marker)):]


@dataclass(frozen=True)
class CommandTask:
    """保存一行配置解析出的宿主任务参数。"""

    line_number: int
    name: str
    cron: str
    command: str
    random_delay: Optional[str] = None


@dataclass(frozen=True)
class CommandResult:
    """保存命令执行状态以及供历史和通知使用的输出。"""

    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    skipped: bool = False

    @property
    def success(self) -> bool:
        """命令正常启动并以零返回码退出时才视为成功。"""
        return not self.skipped and self.error is None and self.returncode == 0

    @property
    def message(self) -> str:
        """保持旧版的 stdout 最后一行优先语义。"""
        stdout_lines = [line.strip() for line in self.stdout.splitlines() if line.strip()]
        if stdout_lines:
            return stdout_lines[-1]
        stderr_lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        if stderr_lines:
            return stderr_lines[-1]
        return self.error or ""


# 插件配置字段保持与已发布配置键一一对应，避免隐式迁移或丢字段。
# pylint: disable=too-many-instance-attributes
class CustomCommand(_PluginBase):
    """按配置的 cron 或一次性任务执行宿主 shell 命令。"""

    plugin_name = "自定义命令"
    plugin_desc = "自定义执行周期执行命令并推送结果。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/code.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "customcommand_"
    plugin_order = 39
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._run_once = False
        self._notify = False
        self._clear = False
        self._msgtype: Optional[str] = None
        self._time_confs = ""
        self._history_days = 30
        self._notify_keywords = ""
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._process_lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置；服务注册和执行统一交由宿主调度器。"""
        # 宿主配置更新会复用同一实例，切换配置前必须先收敛旧命令进程。
        if self.stop_service() is False:
            logger.error("旧命令进程未收敛，拒绝切换插件配置")
            return
        config = dict(config or {})
        with self._run_lock:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._run_once = self._onlyonce
            self._notify = bool(config.get("notify"))
            self._msgtype = config.get("msgtype")
            self._clear = bool(config.get("clear"))
            self._history_days = self._normalise_history_days(config.get("history_days"))
            self._notify_keywords = str(config.get("notify_keywords") or "")
            self._time_confs = str(config.get("time_confs") or "")
            self._stop_event.clear()

        if self._clear:
            self.del_data("history")
            self._clear = False
            self.update_config(self._config_payload())

    @staticmethod
    def _normalise_history_days(value: Any) -> int:
        """把非法保留天数收敛到兼容默认值。"""
        try:
            days = int(value or 30)
        except (TypeError, ValueError):
            return 30
        return max(days, 1)

    def _config_payload(self, *, onlyonce: Optional[bool] = None) -> dict[str, Any]:
        """返回持久化所需的完整配置，避免消费开关时丢失其它字段。"""
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "notify": self._notify,
            "msgtype": self._msgtype,
            "time_confs": self._time_confs,
            "history_days": self._history_days,
            "notify_keywords": self._notify_keywords,
            "clear": self._clear,
        }

    def _report_config_error(self, message: str) -> None:
        """同时记录日志和宿主系统消息，便于用户定位被跳过的配置。"""
        logger.error(message)
        self.systemmessage.put(message)

    def _tasks(self) -> list[CommandTask]:
        """逐行解析任务；错误行隔离处理，不影响其它合法任务。"""
        tasks: list[CommandTask] = []
        for line_number, raw_line in enumerate(self._time_confs.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("#")
            if len(parts) not in (3, 4) or not all(part.strip() for part in parts[:3]):
                self._report_config_error(f"第 {line_number} 行命令配置错误，已跳过")
                continue
            tasks.append(
                CommandTask(
                    line_number=line_number,
                    name=parts[0].strip(),
                    cron=parts[1].strip(),
                    command=parts[2].strip(),
                    random_delay=parts[3].strip() if len(parts) == 4 else None,
                )
            )
        return tasks

    @staticmethod
    def _delay_bounds(value: str) -> tuple[int, int]:
        """解析闭区间随机延时，并拒绝负数或逆序范围。"""
        parts = value.split("-", maxsplit=1)
        if len(parts) != 2:
            raise ValueError("随机延时必须使用 起始秒-结束秒 格式")
        lower, upper = (int(item.strip()) for item in parts)
        if lower < 0 or upper < lower:
            raise ValueError("随机延时必须为非负且结束秒不小于起始秒")
        return lower, upper

    @staticmethod
    def _popen_options() -> dict[str, Any]:
        """创建独立进程组，使停止命令时不会遗留 shell 子进程。"""
        if os.name == "nt":
            return {
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            }
        return {"start_new_session": True}

    @staticmethod
    def _read_output(stream: Any, output: _BoundedOutput) -> None:
        """持续消费管道并把日志和结果限制在固定内存上限内。"""
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(OUTPUT_READ_SIZE)
                if not chunk:
                    break
                output.append(chunk)
                for line in chunk.splitlines():
                    if line.strip():
                        logger.info(line.strip())
        except (OSError, ValueError) as error:
            logger.debug("读取命令输出结束：%s", error)

    def _execute_process(self, command: str) -> CommandResult:
        """持续消费两个管道，避免阻塞并限制结果输出大小。"""
        process: Optional[subprocess.Popen[str]] = None
        stdout = _BoundedOutput(MAX_OUTPUT_SIZE)
        stderr = _BoundedOutput(MAX_OUTPUT_SIZE)
        readers: list[threading.Thread] = []
        try:
            with self._process_lock:
                # 进程句柄由生命周期清理逻辑统一回收，不能通过上下文管理器提前关闭。
                # pylint: disable-next=consider-using-with
                process = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **self._popen_options(),
                )
                self._process = process
            for stream, output, name in (
                (process.stdout, stdout, "custom-command-stdout"),
                (process.stderr, stderr, "custom-command-stderr"),
            ):
                reader = threading.Thread(
                    target=self._read_output,
                    args=(stream, output),
                    name=name,
                    daemon=True,
                )
                readers.append(reader)
                reader.start()
            process.wait()
            for reader in readers:
                reader.join(timeout=PROCESS_STOP_TIMEOUT)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("命令启动失败：%s", error)
            return CommandResult(returncode=None, error=f"命令启动失败：{error}")
        finally:
            if process is not None:
                self._clear_process(process)
        if process is None:
            return CommandResult(returncode=None, error="命令启动失败：未创建进程")
        logger.info("命令执行%s，返回码=%s", "成功" if process.returncode == 0 else "失败", process.returncode)
        return CommandResult(
            returncode=process.returncode,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def _clear_process(self, process: subprocess.Popen[str]) -> None:
        """仅清理仍指向该实例的句柄，避免旧任务覆盖新任务状态。"""
        with self._process_lock:
            if self._process is process:
                self._process = None

    def _save_history(self, task: CommandTask, result: CommandResult) -> None:
        """追加执行历史并丢弃超出保留期或格式损坏的旧记录。"""
        timezone = ZoneInfo(str(settings.TZ))
        now = datetime.now(timezone)
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = [history]
        history.append({
            "name": task.name,
            "command": task.command,
            "result": result.message,
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        })
        cutoff = now - timedelta(days=self._history_days)
        retained = []
        for record in history:
            if not isinstance(record, dict):
                continue
            try:
                recorded_at = datetime.strptime(
                    str(record["time"]), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone)
            except (KeyError, TypeError, ValueError):
                continue
            if recorded_at >= cutoff:
                retained.append(record)
        self.save_data(key="history", value=retained)

    def _notify_result(self, task: CommandTask, result: CommandResult) -> None:
        """按现有通知开关、消息类型和关键词规则投递执行结果。"""
        if not self._notify or not self._msgtype:
            return
        message = result.message
        if self._notify_keywords:
            try:
                matched = re.search(self._notify_keywords, message)
            except re.error as error:
                self._report_config_error(f"通知关键词正则表达式错误：{error}")
                return
            if not matched:
                logger.info("通知关键词 %s 不匹配，跳过通知", self._notify_keywords)
                return
        message_type = MessageType.__members__.get(str(self._msgtype), MessageType.Manual)
        self.post_message(
            title=task.name,
            mtype=message_type,
            text=message,
        )

    def _run_task(self, task: CommandTask, *, apply_delay: bool) -> CommandResult:
        """执行单个任务并完成历史与通知副作用。"""
        if apply_delay and task.random_delay:
            lower, upper = self._delay_bounds(task.random_delay)
            delay = random.randint(lower, upper)
            logger.info("任务 %s 随机延时 %s 秒", task.name, delay)
            if self._stop_event.wait(delay):
                return CommandResult(returncode=None, error="任务已停止")
        if self._stop_event.is_set():
            return CommandResult(returncode=None, error="任务已停止")
        result = self._execute_process(task.command)
        self._save_history(task, result)
        self._notify_result(task, result)
        return result

    def _run_periodic_task(self, task: CommandTask) -> bool | tuple[bool, str]:
        """周期任务采用非阻塞 single-flight，重叠触发直接跳过。"""
        # 非阻塞获取决定了重叠任务的“跳过”语义，不能改用阻塞上下文。
        # pylint: disable-next=consider-using-with
        if not self._run_lock.acquire(blocking=False):
            message = f"任务 {task.name} 触发时已有命令正在执行，本次已跳过"
            logger.warning(message)
            return False, message
        try:
            if not self._enabled or self._stop_event.is_set():
                return False, "插件已停用"
            result = self._run_task(task, apply_delay=True)
            if result.success:
                return True
            return False, result.message or "命令执行失败"
        except (TypeError, ValueError) as error:
            message = f"任务 {task.name} 随机延时配置错误：{error}"
            self._report_config_error(message)
            return False, message
        except Exception as error:  # pylint: disable=broad-exception-caught
            message = f"任务 {task.name} 执行失败：{error}"
            logger.error(message)
            return False, message
        finally:
            self._run_lock.release()

    def _consume_once(self) -> Optional[list[CommandTask]]:
        """配置持久化成功后才消费一次性执行意图。"""
        with self._once_lock:
            if not self._run_once:
                return None
            tasks = self._tasks()
            if not tasks:
                return None
            try:
                persisted = self.update_config(self._config_payload(onlyonce=False))
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error("保存一次性命令状态失败：%s", error)
                return None
            if persisted is False:
                logger.error("保存一次性命令状态失败：宿主拒绝更新配置")
                return None
            self._onlyonce = False
            self._run_once = False
            return tasks

    def _run_once_tasks(self) -> bool | tuple[bool, str]:
        """等待执行槽并串行完成全部一次性任务，避免消费后丢任务。"""
        with self._run_lock:
            tasks = self._consume_once()
            if tasks is None:
                return False, "一次性任务无有效配置、已被消费或状态保存失败"
            failed = []
            for task in tasks:
                try:
                    result = self._run_task(task, apply_delay=False)
                except Exception as error:  # pylint: disable=broad-exception-caught
                    logger.error("一次性任务 %s 执行失败：%s", task.name, error)
                    failed.append(task.name)
                    continue
                if not result.success:
                    failed.append(task.name)
            if failed:
                return False, f"一次性任务执行失败：{', '.join(failed)}"
            return True

    def get_state(self) -> bool:
        """启用状态或待执行的一次性状态均需向宿主投影服务。"""
        return self._enabled or self._run_once

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不暴露 HTTP API。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """把一次性和周期任务投影为宿主调度服务。"""
        tasks = self._tasks()
        services: list[dict[str, Any]] = []
        timezone = ZoneInfo(str(settings.TZ))
        if self._run_once and tasks:
            services.append({
                "id": "CustomCommand.Once",
                "name": "自定义命令（立即运行）",
                "trigger": "date",
                "func": self._run_once_tasks,
                "kwargs": {"run_date": datetime.now(timezone) + timedelta(seconds=3)},
            })
        if not self._enabled:
            return services

        for task in tasks:
            try:
                if task.random_delay:
                    self._delay_bounds(task.random_delay)
                trigger = CronTrigger.from_crontab(task.cron, timezone=timezone)
            except (TypeError, ValueError) as error:
                self._report_config_error(
                    f"第 {task.line_number} 行定时任务配置错误：{error}"
                )
                continue
            services.append({
                "id": f"CustomCommand.{task.line_number}",
                "name": task.name + (
                    f"（随机延时 {task.random_delay} 秒）" if task.random_delay else ""
                ),
                "trigger": trigger,
                "func": self._run_periodic_task,
                "kwargs": {},
                "func_kwargs": {"task": task},
            })
        return services

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置表单和默认值。"""
        message_type_options = [
            {"title": item.value, "value": item.name} for item in MessageType
        ]
        return [{
            "component": "VForm",
            "content": [
                {
                    "component": "VRow",
                    "content": [
                        self._switch("enabled", "启用插件"),
                        self._switch("notify", "发送通知"),
                        self._switch("onlyonce", "立即运行一次"),
                        self._switch("clear", "清除历史记录"),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [{
                                "component": "VSelect",
                                "props": {
                                    "model": "msgtype",
                                    "label": "消息类型",
                                    "items": message_type_options,
                                    "multiple": False,
                                    "chips": True,
                                },
                            }],
                        },
                        self._text_field("history_days", "保留历史天数"),
                        self._text_field(
                            "notify_keywords",
                            "通知关键词",
                            "支持正则表达式，未配置时所有通知均推送",
                        ),
                    ],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VTextarea",
                            "props": {
                                "model": "time_confs",
                                "label": "执行命令",
                                "rows": 2,
                                "placeholder": (
                                    "命令名#0 9 * * *#python main.py\n"
                                    "命令名#0 9 * * *#python main.py#1-600"
                                ),
                            },
                        }],
                    }],
                },
                self._alert("命令名#cron表达式#命令"),
                self._alert("命令名#cron表达式#命令#随机延时（单位秒）"),
            ],
        }], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "clear": False,
            "time_confs": "",
            "history_days": 30,
            "notify_keywords": "",
            "msgtype": "",
        }

    @staticmethod
    def _switch(model: str, label: str) -> dict[str, Any]:
        """构造四列开关字段。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }

    @staticmethod
    def _text_field(
        model: str,
        label: str,
        placeholder: Optional[str] = None,
    ) -> dict[str, Any]:
        """构造三列文本字段。"""
        props = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 4},
            "content": [{"component": "VTextField", "props": props}],
        }

    @staticmethod
    def _alert(text: str) -> dict[str, Any]:
        """构造命令格式提示。"""
        return {
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "text": text},
                }],
            }],
        }

    def get_page(self) -> list[dict]:
        """返回按执行时间倒序排列的历史页面。"""
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "div",
                "text": "暂无数据",
                "props": {"class": "text-center"},
            }]
        if not isinstance(history, list):
            history = [history]
        history = sorted(
            (item for item in history if isinstance(item, dict)),
            key=lambda item: item.get("time") or "",
            reverse=True,
        )
        rows = [{
            "component": "tr",
            "props": {"class": "text-sm"},
            "content": [
                self._cell(item.get("time"), "whitespace-nowrap break-keep text-high-emphasis"),
                self._cell(item.get("name")),
                self._cell(item.get("result")),
            ],
        } for item in history]
        return [{
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VTable",
                    "props": {"hover": True},
                    "content": [
                        {
                            "component": "thead",
                            "content": [{
                                "component": "tr",
                                "content": [
                                    self._header("执行时间"),
                                    self._header("命令名称"),
                                    self._header("执行结果"),
                                ],
                            }],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }],
            }],
        }]

    @staticmethod
    def _cell(value: Any, css_class: Optional[str] = None) -> dict[str, Any]:
        """构造历史表格单元格。"""
        cell: dict[str, Any] = {"component": "td", "text": value or ""}
        if css_class:
            cell["props"] = {"class": css_class}
        return cell

    @staticmethod
    def _header(text: str) -> dict[str, Any]:
        """构造历史表头。"""
        return {
            "component": "th",
            "props": {"class": "text-start ps-4"},
            "text": text,
        }

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str], *, force: bool) -> None:
        """向独立进程组发送终止信号，并兼容 Windows 进程组语义。"""
        if os.name == "nt":
            ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if not force and ctrl_break_event is not None:
                try:
                    process.send_signal(ctrl_break_event)
                    return
                except (OSError, ProcessLookupError, ValueError):
                    pass
            try:
                process.kill() if force else process.terminate()
            except (OSError, ProcessLookupError, ValueError):
                return
            return
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            # 自然退出与 stop_service 并发时，进程组可能已经不存在。
            return

    def stop_service(self) -> Optional[bool]:
        """在进程锁内停止当前命令，并清理已退出的进程句柄。"""
        self._stop_event.set()
        converged = True
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is not None:
                self._clear_process(process)
                process = None
            if process is not None:
                stopped = False
                self._terminate_process(process, force=False)
                try:
                    process.wait(timeout=PROCESS_STOP_TIMEOUT)
                    stopped = True
                except subprocess.TimeoutExpired:
                    self._terminate_process(process, force=True)
                    try:
                        process.wait(timeout=PROCESS_STOP_TIMEOUT)
                        stopped = True
                    except subprocess.TimeoutExpired:
                        converged = False
                        logger.error("命令进程停止超时，PID=%s", process.pid)
                if stopped:
                    self._clear_process(process)
        if not converged:
            return False
        # 无进程窗口仍可能有已取得执行槽的任务；停止事件会使其在 Popen 前退出。
        with self._run_lock:
            pass
        return None
