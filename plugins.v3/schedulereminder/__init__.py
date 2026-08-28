"""按用户配置的 Cron 计划发送日程提醒。"""

from __future__ import annotations

from typing import Any, Optional

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.logging import logger


class ScheduleReminder(_PluginBase):
    """把提醒配置投影为由宿主统一管理的定时服务。"""

    # 插件市场元数据和宿主加载约束。
    plugin_name = "日程提醒"
    plugin_desc = "自定义提醒事项、提醒时间。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/reminder.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "schedulereminder_"
    plugin_order = 32
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件配置状态。"""
        super().__init__()
        self._enabled = False
        self._confs = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取提醒配置；调度生命周期由宿主服务目录负责。"""
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._confs = str(config.get("confs") or "").strip()

    def __send_notify(self, theme: str) -> None:
        """发送一条手动处理类型的日程提醒。"""
        self.post_message(
            mtype=MessageType.Manual,
            title="日程提醒",
            text=theme,
        )

    @staticmethod
    def __parse_config(line: str) -> Optional[tuple[str, str]]:
        """解析 ``提醒内容:五段 Cron``，提醒内容允许包含冒号。"""
        theme, separator, cron = line.rpartition(":")
        theme = theme.strip()
        cron = " ".join(cron.split())
        if not separator or not theme or not cron:
            return None
        return theme, cron

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不暴露额外 HTTP API。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """为每条有效配置注册一个宿主 Cron 服务。"""
        if not self._enabled or not self._confs:
            return []

        services: list[dict[str, Any]] = []
        timezone = pytz.timezone(str(settings.TZ))
        for index, raw_line in enumerate(self._confs.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = self.__parse_config(line)
            if parsed is None:
                message = f"提醒配置第 {index} 行格式错误，已跳过"
                logger.warning(message)
                self.systemmessage.put(message)
                continue

            theme, cron = parsed
            try:
                trigger = CronTrigger.from_crontab(cron, timezone=timezone)
            except (TypeError, ValueError) as error:
                message = f"提醒配置第 {index} 行 Cron 错误：{error}"
                logger.error(message)
                self.systemmessage.put(message)
                continue

            services.append(
                {
                    "id": f"ScheduleReminder.{index}",
                    "name": f"{theme}提醒",
                    "trigger": trigger,
                    "func": self.__send_notify,
                    "kwargs": {},
                    "func_kwargs": {"theme": theme},
                }
            )
        return services

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回插件配置表单和默认值。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "confs",
                                            "label": "提醒事项",
                                            "rows": 5,
                                            "placeholder": "提醒内容:cron",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "提醒事项格式为：提醒内容:提醒时间cron表达式（一行一条）。需开启（手动处理通知）通知类型",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False, "confs": ""}

    def get_page(self) -> list[dict]:
        """本插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """本插件不持有宿主调度器之外的服务资源。"""
        return None
