"""提供由宿主调度器管理的 Docker 容器任务。"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import docker
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.logging import logger


@dataclass(frozen=True)
class DockerTask:
    """保存一行 Docker 任务配置。"""

    line_number: int
    container_names: tuple[str, ...]
    cron: str
    command: str

    @property
    def display_names(self) -> str:
        """返回用于服务名称和日志的容器名列表。"""
        return ",".join(self.container_names)


@dataclass(frozen=True)
class ContainerResult:
    """保存单个容器的任务执行结果。"""

    name: str
    command: str
    success: bool
    error: Optional[str] = None
    icon: Optional[str] = None

    @property
    def message(self) -> str:
        """返回与历史及通知一致的可读结果。"""
        status = "success" if self.success else "fail"
        suffix = f"：{self.error}" if self.error else ""
        return f"容器：{self.name} {self.command} {status}{suffix}"


# 插件配置字段保持与已发布配置键一一对应，避免隐式迁移或丢字段。
# pylint: disable=too-many-instance-attributes
class DockerManager(_PluginBase):
    """按 cron 或一次性配置控制指定 Docker 容器。"""

    plugin_name = "docker自定义任务"
    plugin_desc = "管理宿主机docker，自定义容器定时任务。"
    plugin_icon = "Docker_F.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "dockermanager_"
    plugin_order = 39
    auth_level = 1

    SUPPORTED_COMMANDS = frozenset({
        "restart",
        "start",
        "stop",
        "pause",
        "unpause",
        "update",
    })

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
        self._docker_client: Any = None
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()
        self._client_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """释放旧 client 并读取配置；调度生命周期由宿主管理。"""
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._notify = bool(config.get("notify"))
        self._msgtype = config.get("msgtype")
        self._clear = bool(config.get("clear"))
        self._time_confs = str(config.get("time_confs") or "")
        self._history_days = self._normalise_history_days(config.get("history_days"))

        if self._clear:
            self.del_data("history")
            self._clear = False
            self.update_config(self._config_payload())

    @staticmethod
    def _normalise_history_days(value: Any) -> int:
        """把非法历史保留天数收敛到兼容默认值。"""
        try:
            days = int(value or 30)
        except (TypeError, ValueError):
            return 30
        return max(days, 1)

    def _config_payload(self, *, onlyonce: Optional[bool] = None) -> dict[str, Any]:
        """返回消费开关时需要持久化的完整配置。"""
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
            "notify": self._notify,
            "msgtype": self._msgtype,
            "time_confs": self._time_confs,
            "history_days": self._history_days,
            "clear": self._clear,
        }

    def _report_config_error(self, message: str) -> None:
        """同时写入日志和宿主系统消息。"""
        logger.error(message)
        self.systemmessage.put(message)

    def _tasks(self) -> list[DockerTask]:
        """逐行解析任务，并隔离格式、容器名和命令错误。"""
        tasks: list[DockerTask] = []
        for line_number, raw_line in enumerate(self._time_confs.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("#")
            if len(parts) != 3 or not all(part.strip() for part in parts):
                self._report_config_error(f"第 {line_number} 行 Docker 任务配置错误，已跳过")
                continue
            names = tuple(dict.fromkeys(
                name.strip() for name in parts[0].split(",") if name.strip()
            ))
            command = parts[2].strip().lower()
            if not names:
                self._report_config_error(f"第 {line_number} 行没有有效容器名，已跳过")
                continue
            if command not in self.SUPPORTED_COMMANDS:
                self._report_config_error(
                    f"第 {line_number} 行 Docker 命令 {command} 不受支持，已跳过"
                )
                continue
            tasks.append(DockerTask(
                line_number=line_number,
                container_names=names,
                cron=parts[1].strip(),
                command=command,
            ))
        return tasks

    def _get_client(self) -> Any:
        """按宿主配置惰性创建并复用 Docker client。"""
        with self._client_lock:
            if self._docker_client is not None:
                return self._docker_client
            base_url = str(settings.DOCKER_CLIENT_API or "").strip()
            if not base_url:
                raise RuntimeError("宿主未配置 DOCKER_CLIENT_API")
            self._docker_client = docker.DockerClient(base_url=base_url)
            return self._docker_client

    @staticmethod
    def _container_attrs(container: Any) -> dict[str, Any]:
        """把 Docker SDK 的动态 attrs 边界规整为字典。"""
        attrs = getattr(container, "attrs", None)
        return attrs if isinstance(attrs, dict) else {}

    @classmethod
    def _container_name(cls, container: Any) -> Optional[str]:
        """优先沿用 HOST_CONTAINERNAME，缺失时回退到 Docker 标准名称。"""
        attrs = cls._container_attrs(container)
        config = attrs.get("Config")
        config = config if isinstance(config, dict) else {}
        envs = config.get("Env")
        if isinstance(envs, list):
            for item in envs:
                if not isinstance(item, str):
                    continue
                key, separator, value = item.partition("=")
                if separator and key == "HOST_CONTAINERNAME" and value:
                    return value
        name = getattr(container, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
        raw_name = attrs.get("Name")
        if isinstance(raw_name, str) and raw_name.strip("/"):
            return raw_name.strip("/")
        return None

    @classmethod
    def _container_icon(cls, container: Any) -> Optional[str]:
        """读取可选 Unraid 图标，缺失或非 HTTP 地址时不用于通知。"""
        attrs = cls._container_attrs(container)
        config = attrs.get("Config")
        config = config if isinstance(config, dict) else {}
        labels = config.get("Labels")
        labels = labels if isinstance(labels, dict) else {}
        icon = labels.get("net.unraid.docker.icon")
        if isinstance(icon, str) and icon.startswith(("http://", "https://")):
            return icon
        return None

    @staticmethod
    def _execute_container(
        container: Any,
        name: str,
        command: str,
        icon: Optional[str],
    ) -> ContainerResult:
        """执行经过白名单校验的容器方法，并把 SDK 异常转为结果。"""
        try:
            operation = getattr(container, command)
            operation()
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("容器 %s 执行 %s 失败：%s", name, command, error)
            return ContainerResult(
                name=name,
                command=command,
                success=False,
                error=str(error),
                icon=icon,
            )
        logger.info("容器 %s 执行 %s 成功", name, command)
        return ContainerResult(
            name=name,
            command=command,
            success=True,
            icon=icon,
        )

    def _save_history(self, results: list[ContainerResult]) -> None:
        """批量追加容器结果并清理损坏或过期的旧记录。"""
        timezone = ZoneInfo(str(settings.TZ))
        now = datetime.now(timezone)
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = [history]
        for result in results:
            history.append({
                "name": result.name,
                "command": result.command,
                "result": "success" if result.success else "fail",
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

    def _notify_results(self, task: DockerTask, results: list[ContainerResult]) -> None:
        """按原通知开关发送本次任务的聚合结果。"""
        if not self._notify or not self._msgtype:
            return
        message_type = MessageType.__members__.get(str(self._msgtype), MessageType.Manual)
        image = None
        if len(task.container_names) == 1 and results:
            image = results[0].icon
        self.post_message(
            title="docker任务通知",
            mtype=message_type,
            text="\n".join(result.message for result in results),
            image=image,
        )

    def _run_task(self, task: DockerTask) -> list[ContainerResult]:
        """执行一个任务中的全部容器，并显式报告未找到的目标。"""
        try:
            containers = self._get_client().containers.list(all=True)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("获取 Docker 容器列表失败：%s", error)
            results = [
                ContainerResult(name=name, command=task.command, success=False, error=str(error))
                for name in task.container_names
            ]
            self._save_history(results)
            self._notify_results(task, results)
            return results

        by_name: dict[str, Any] = {}
        for container in containers or []:
            name = self._container_name(container)
            if name and name not in by_name:
                by_name[name] = container

        results = []
        for name in task.container_names:
            container = by_name.get(name)
            if container is None:
                result = ContainerResult(
                    name=name,
                    command=task.command,
                    success=False,
                    error="未找到容器",
                )
                logger.error(result.message)
            else:
                result = self._execute_container(
                    container,
                    name,
                    task.command,
                    self._container_icon(container),
                )
            results.append(result)
        self._save_history(results)
        self._notify_results(task, results)
        return results

    def _run_periodic_task(self, task: DockerTask) -> bool | tuple[bool, str]:
        """周期任务采用非阻塞 single-flight，重叠触发直接跳过。"""
        # 非阻塞获取决定了重叠任务的“跳过”语义，不能改用阻塞上下文。
        # pylint: disable-next=consider-using-with
        if not self._run_lock.acquire(blocking=False):
            message = f"Docker 任务 {task.display_names} 触发重叠，本次已跳过"
            logger.warning(message)
            return False, message
        try:
            results = self._run_task(task)
            if results and all(result.success for result in results):
                return True
            return False, "Docker 任务存在失败容器"
        except Exception as error:  # pylint: disable=broad-exception-caught
            message = f"Docker 任务执行失败：{error}"
            logger.error(message)
            return False, message
        finally:
            self._run_lock.release()

    def _consume_once(self) -> Optional[list[DockerTask]]:
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
                logger.error("保存一次性 Docker 任务状态失败：%s", error)
                return None
            if persisted is False:
                logger.error("保存一次性 Docker 任务状态失败：宿主拒绝更新配置")
                return None
            self._onlyonce = False
            self._run_once = False
            return tasks

    def _run_once_tasks(self) -> bool | tuple[bool, str]:
        """等待执行槽并串行完成全部一次性 Docker 任务。"""
        with self._run_lock:
            tasks = self._consume_once()
            if tasks is None:
                return False, "一次性 Docker 任务无有效配置、已被消费或状态保存失败"
            failed = []
            for task in tasks:
                try:
                    results = self._run_task(task)
                except Exception as error:  # pylint: disable=broad-exception-caught
                    logger.error("一次性 Docker 任务 %s 失败：%s", task.display_names, error)
                    failed.extend(task.container_names)
                    continue
                failed.extend(result.name for result in results if not result.success)
            if failed:
                return False, f"一次性 Docker 任务执行失败：{', '.join(failed)}"
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
        """把一次性和周期 Docker 任务投影为宿主调度服务。"""
        tasks = self._tasks()
        timezone = ZoneInfo(str(settings.TZ))
        services: list[dict[str, Any]] = []
        if self._run_once and tasks:
            services.append({
                "id": "DockerManager.Once",
                "name": "Docker 自定义任务（立即运行）",
                "trigger": "date",
                "func": self._run_once_tasks,
                "kwargs": {"run_date": datetime.now(timezone) + timedelta(seconds=3)},
            })
        if not self._enabled:
            return services
        for task in tasks:
            try:
                trigger = CronTrigger.from_crontab(task.cron, timezone=timezone)
            except (TypeError, ValueError) as error:
                self._report_config_error(
                    f"第 {task.line_number} 行 Docker 定时配置错误：{error}"
                )
                continue
            services.append({
                "id": f"DockerManager.{task.line_number}",
                "name": f"{task.display_names} {task.command}",
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
                            "props": {"cols": 12, "md": 6},
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
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [{
                                "component": "VTextField",
                                "props": {
                                    "model": "history_days",
                                    "label": "保留历史天数",
                                },
                            }],
                        },
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
                                    "容器名#cron表达式#restart/start/stop/"
                                    "pause/unpause/update"
                                ),
                            },
                        }],
                    }],
                },
                {
                    "component": "VRow",
                    "content": [{
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [{
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": (
                                    "容器名(多个容器名,拼接)#cron表达式#"
                                    "restart/start/stop/pause/unpause/update"
                                ),
                            },
                        }],
                    }],
                },
            ],
        }], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "clear": False,
            "time_confs": "",
            "history_days": 30,
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
                self._cell(item.get("time")),
                self._cell(item.get("name")),
                self._cell(item.get("command")),
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
                                    self._header("容器名称"),
                                    self._header("命令"),
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
    def _cell(value: Any) -> dict[str, Any]:
        """构造历史表格单元格。"""
        return {"component": "td", "text": value or ""}

    @staticmethod
    def _header(text: str) -> dict[str, Any]:
        """构造历史表头。"""
        return {
            "component": "th",
            "props": {"class": "text-start ps-4"},
            "text": text,
        }

    def stop_service(self) -> None:
        """等待正在执行的任务结束，再关闭并清空 Docker client。"""
        with self._run_lock:
            with self._client_lock:
                client = self._docker_client
                self._docker_client = None
            if client is None:
                return
            try:
                client.close()
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error("关闭 Docker client 失败：%s", error)
