"""Emby 视频类型检查插件。"""

from __future__ import annotations

import datetime
import threading
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


# 配置、调度和远端服务上下文属于同一插件生命周期，不能拆成互相独立的全局状态。
# pylint: disable=too-many-instance-attributes
class EmbyExtendType(_PluginBase):
    """定期检查选定 Emby 媒体库是否包含配置的视频类型并发送通知。"""

    plugin_name = "Emby视频类型检查"
    plugin_desc = "定期检查Emby媒体库中是否包含指定的视频类型，发送通知。"
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/extendtype.png"
    )
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embyextendtype_"
    plugin_order = 30
    auth_level = 1

    _title = "Emby视频类型检查"
    _api_path = "emby/ExtendedVideoTypes"

    def __init__(self) -> None:
        """初始化插件状态和媒体服务器服务门面。"""
        super().__init__()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._run_lock = threading.Lock()
        self._enabled = False
        self._onlyonce = False
        self._notify = False
        self._cron = ""
        self._librarys: List[str] = []
        self._extend = ""
        self._msgtype: Optional[str] = None
        self._mediaservers: List[str] = []
        self._mediaserver_helper = MediaServerHelper()
        self._emby_host = ""
        self._emby_api_key = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """停止旧任务、读取配置并按开关重建后台调度器。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify = bool(config.get("notify"))
        self._cron = str(config.get("cron") or "").strip()
        self._librarys = self._normalize_string_list(config.get("librarys"))
        self._extend = str(config.get("extend") or "").strip()
        self._msgtype = config.get("msgtype") or MessageType.Manual.name
        self._mediaservers = self._normalize_string_list(config.get("mediaservers"))

        if not (self._enabled or self._onlyonce):
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)  # pylint: disable=consider-using-with
        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self.check_extend,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name=self._title,
                )
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error(f"定时任务配置错误：{err}")
                self.systemmessage.put(f"执行周期配置错误：{err}")

        if self._onlyonce:
            logger.info(f"{self._title}服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self.check_extend,
                trigger="date",
                run_date=datetime.datetime.now(
                    tz=pytz.timezone(settings.TZ)
                ) + datetime.timedelta(seconds=3),
                name=self._title,
            )
            self._onlyonce = False
            self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def check_extend(self) -> None:
        """检查选定 Emby 服务的媒体库类型并发送命中通知。"""
        if not self._run_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
            logger.warning(f"{self._title}任务正在运行，本次触发已跳过")
            return

        try:
            self.__check_extend()
        finally:
            self._run_lock.release()

    def __check_extend(self) -> None:
        """执行一次媒体库类型检查，按服务隔离 Emby 请求上下文。"""
        extensions = self._parse_extensions(self._extend)
        if not extensions:
            logger.error("视频类型为空，不进行检查")
            return

        emby_servers = self._mediaserver_helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            if not emby_server.instance:
                logger.warning(f"Emby媒体服务器 {emby_name} 未连接")
                continue

            try:
                host, api_key = self.__set_server_context(emby_server)
            except (AttributeError, TypeError, ValueError) as err:
                logger.error(f"读取 Emby 媒体服务器 {emby_name} 配置失败：{err}")
                continue
            if not host or not api_key:
                logger.warning(f"Emby媒体服务器 {emby_name} 配置不完整")
                continue

            logger.info(f"开始处理媒体服务器 {emby_name}")
            try:
                libraries = emby_server.instance.get_librarys() or []
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error(f"获取媒体服务器 {emby_name} 媒体库失败：{err}")
                continue
            libraries = self._select_libraries(libraries)
            for library in libraries:
                library_id = str(library.id or "").strip()
                library_name = str(library.name or library_id).strip()
                if not library_id:
                    logger.warning(f"媒体服务器 {emby_name} 存在缺少 ID 的媒体库，已跳过")
                    continue

                logger.info(
                    f"开始检查媒体服务器 {emby_name} 的媒体库 {library_name} "
                    f"中是否包含 {self._extend} 类型"
                )
                library_extensions = self.__get_extend_type(library_id)
                extension_names = {
                    str(item.get("Name")).strip()
                    for item in library_extensions
                    if isinstance(item, dict) and item.get("Name")
                }
                for extension in extensions:
                    if extension not in extension_names:
                        continue
                    logger.info(f"媒体库 {library_name} 中包含 {extension} 类型")
                    if self._notify:
                        self.post_message(
                            title=self._title,
                            mtype=self._message_type(),
                            text=f"媒体库 {library_name} 命中 {extension} 视频类型",
                        )
                logger.info(f"{emby_name} 媒体库 {library_name} 中全部视频类型检查完毕")

            logger.info(f"{emby_name} 媒体库中全部视频类型检查完毕")

    def __set_server_context(self, emby_server: Any) -> Tuple[str, str]:
        """读取当前服务的 Emby 地址和 API key，并规范化地址。"""
        server_config = (
            emby_server.config.config
            if emby_server.config and emby_server.config.config
            else {}
        )
        if not isinstance(server_config, dict):
            raise TypeError("Emby 服务配置不是对象")
        host = self._normalize_host(server_config.get("host"))
        api_key = str(server_config.get("apikey") or "").strip()
        self._emby_host = host
        self._emby_api_key = api_key
        return host, api_key

    def __get_extend_type(self, parent_id: str) -> List[dict]:
        """读取一个 Emby 媒体库的 ExtendedVideoTypes 响应。"""
        if not self._emby_host or not self._emby_api_key or not parent_id:
            return []

        response = None
        try:
            response = RequestUtils().get_res(
                url=self._emby_host + self._api_path,
                params={
                    "ParentId": parent_id,
                    "Recursive": "true",
                    "IncludeItemTypes": "Episode,Movie",
                    "Limit": 10,
                    "api_key": self._emby_api_key,
                },
            )
            if response is None or response.status_code != 200:
                logger.error(f"获取媒体库 {parent_id} 视频类型失败，无法连接Emby")
                return []

            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("Items"), list):
                logger.error(f"获取媒体库 {parent_id} 视频类型失败，响应格式无效")
                return []
            return payload["Items"]
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"连接 ExtendedVideoTypes 出错：{err}")
            return []
        finally:
            if response is not None:
                response.close()

    def __update_config(self) -> None:
        """保存一次性开关归零后的插件配置。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "notify": self._notify,
                "cron": self._cron,
                "extend": self._extend,
                "msgtype": self._msgtype,
                "librarys": self._librarys,
                "mediaservers": self._mediaservers,
            }
        )

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """该插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """该插件不暴露 MoviePilot REST API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """调度器由插件生命周期管理，不重复注册宿主服务。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单和默认配置。"""
        media_servers = [
            {"title": config.name, "value": config.name}
            for config in self._mediaserver_helper.get_configs().values()
            if config.type == "emby"
        ]
        message_types = [
            {"title": message_type.value, "value": message_type.name}
            for message_type in MessageType
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("enabled", "启用插件"),
                            self._switch("onlyonce", "立即运行一次"),
                            self._switch("notify", "开启通知"),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VCronField",
                                    "props": {
                                        "model": "cron",
                                        "label": "定时全量同步周期",
                                        "placeholder": "5位cron表达式，留空关闭",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "multiple": False,
                                        "chips": True,
                                        "model": "msgtype",
                                        "label": "消息类型",
                                        "items": message_types,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "extend",
                                        "label": "视频类型",
                                        "placeholder": "多个英文逗号拼接",
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
                                "component": "VSelect",
                                "props": {
                                    "multiple": True,
                                    "chips": True,
                                    "clearable": True,
                                    "model": "mediaservers",
                                    "label": "媒体服务器",
                                    "items": media_servers,
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cron": "",
            "extend": "",
            "msgtype": MessageType.Manual.name,
            "librarys": [],
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        """该插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """停止插件调度器，避免热加载遗留后台任务。"""
        scheduler = self._scheduler
        self._scheduler = None
        if not scheduler:
            return
        try:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"退出插件失败：{err}")

    @staticmethod
    def _normalize_string_list(value: Any) -> List[str]:
        """把配置中的字符串或列表归一为非空字符串列表。"""
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        ]

    @staticmethod
    def _parse_extensions(value: str) -> List[str]:
        """解析逗号分隔的视频类型并去重保序。"""
        return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))

    def _select_libraries(self, libraries: List[Any]) -> List[Any]:
        """按历史配置筛选媒体库；未配置时保留 V2 的全库扫描行为。"""
        if not self._librarys:
            return libraries
        selected_ids = set()
        selected_names = set()
        for value in self._librarys:
            name, separator, library_id = value.rpartition(" ")
            if separator and library_id:
                selected_names.add(name.strip())
                selected_ids.add(library_id.strip())
            else:
                selected_names.add(value)
        return [
            library
            for library in libraries
            if str(library.id or "").strip() in selected_ids
            or str(library.name or "").strip() in selected_names
        ]

    @staticmethod
    def _normalize_host(value: Any) -> str:
        """把 Emby 地址规范化为带路径分隔符的 HTTP URL。"""
        host = str(value or "").strip()
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return f"{host.rstrip('/')}/"

    def _message_type(self) -> MessageType:
        """把配置中的消息类型名称转换为当前 V3 消息枚举。"""
        if isinstance(self._msgtype, MessageType):
            return self._msgtype
        configured = str(self._msgtype or "").strip()
        if configured:
            for message_type in MessageType:
                if configured in {message_type.name, message_type.value}:
                    return message_type
        return MessageType.Manual

    @staticmethod
    def _switch(model: str, label: str) -> dict:
        """构造配置表单中的开关列。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 4},
            "content": [{
                "component": "VSwitch",
                "props": {"model": model, "label": label},
            }],
        }
