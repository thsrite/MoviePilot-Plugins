from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz

from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


@dataclass(frozen=True)
class _EmbyContext:
    """保存单个 Emby 服务的请求上下文，避免并发任务共享可变连接参数。"""

    name: str
    host: str
    user: str
    api_key: str


class EmbyActorSync(_PluginBase):
    """将剧集或季的演员信息同步到其下属集的 Emby 条目。"""

    plugin_name = "Emby剧集演员同步"
    plugin_desc = "同步剧演员信息到集演员信息。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/embyactorsync.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embyactorsync_"
    plugin_order = 32
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件状态和单实例任务互斥门禁。"""
        super().__init__()
        self._onlyonce = False
        self._run_once = False
        self._enabled = False
        self._mediaservers: List[str] = []
        self.mediaserver_helper: Optional[MediaServerHelper] = None
        self._run_lock = threading.Lock()
        self._run_once_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并准备由宿主调度器管理的一次性同步任务。"""
        self.stop_service()
        with self._run_lock:
            self.__init_plugin(config)

    def __init_plugin(self, config: Optional[dict]) -> None:
        """在没有同步任务运行时更新插件配置。"""
        self.mediaserver_helper = MediaServerHelper()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._mediaservers = [
            str(name) for name in (config.get("mediaservers") or []) if name
        ]

        if self._run_once:
            logger.info("Emby剧集演员同步服务启动，立即运行一次")
            self._onlyonce = False
            self.__update_config()

    def get_state(self) -> bool:
        """返回插件是否启用或仍有待消费的一次性任务。"""
        return self._enabled or self._run_once

    def _run_once_sync(self) -> None:
        """消费一次性服务标记，再执行一轮演员同步。"""
        with self._run_once_lock:
            if not self._run_once:
                return
            self._run_once = False
        self.sync()

    def __update_config(self) -> None:
        """保存一次性开关归一化后的插件配置。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "mediaservers": list(self._mediaservers),
            }
        )

    @eventmanager.register(EventType.PluginAction)
    def sync_actor(self, event: Optional[Event] = None) -> None:
        """响应 /as 命令，按媒体库和剧集名称执行一次同步。"""
        if not self._enabled or not event:
            return

        event_data = event.event_data
        if not isinstance(event_data, dict) or event_data.get("action") != "actorsync":
            return

        args = event_data.get("arg_str")
        if not isinstance(args, str) or not args.strip():
            logger.error("缺少参数：%s", event_data)
            return

        args_list = args.strip().split(maxsplit=1)
        if len(args_list) != 2:
            logger.error("参数错误：%s", args_list)
            self.post_message(
                channel=event_data.get("channel"),
                title="参数错误！ /as 媒体库名 剧集名",
                userid=event_data.get("user"),
            )
            return

        self.sync(args_list[0], args_list[1], event)

    def sync(
        self,
        library_name: Optional[str] = None,
        media_name: Optional[str] = None,
        event: Optional[Event] = None,
    ) -> None:
        """在选定的 Emby 服务中复制剧集演员到各集，并串行保护服务上下文。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Emby剧集演员同步任务正在运行，本次触发已跳过")
            return

        try:
            self.__sync(library_name, media_name, event)
        finally:
            self._run_lock.release()

    def __sync(
        self,
        library_name: Optional[str],
        media_name: Optional[str],
        event: Optional[Event],
    ) -> None:
        """执行一次完整同步；每个媒体服务器使用独立的不可变请求上下文。"""
        helper = self.mediaserver_helper or MediaServerHelper()
        emby_servers = helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            instance = emby_server.instance if emby_server else None
            config = emby_server.config.config if emby_server and emby_server.config else {}
            context = self.__build_context(emby_name, instance, config)
            if not context:
                continue

            logger.info("开始处理媒体服务器 %s", emby_name)
            libraries = instance.get_librarys() or []
            for library in libraries:
                if library.type != MediaType.TV.value:
                    continue
                if library_name and library.name != library_name:
                    continue

                library_items = self.__get_items(context, library.id)
                if not library_items:
                    logger.error("获取媒体库：%s 的媒体列表失败", library.name)
                    continue

                logger.info("开始同步媒体库：%s，ID：%s", library.name, library.id)
                for item in library_items:
                    if not self.__matches_media_name(item, media_name):
                        continue
                    self.__sync_series(context, item)

            logger.info("%s 剧集演员同步完成", emby_name)
            if event:
                event_data = event.event_data
                self.post_message(
                    channel=event_data.get("channel"),
                    title=f"{library_name} {media_name} 同步完成",
                    userid=event_data.get("user"),
                )

    @staticmethod
    def __build_context(
        name: str,
        instance: Any,
        config: Any,
    ) -> Optional[_EmbyContext]:
        """从服务发现结果构造单服务器上下文，缺少必要字段时跳过该服务。"""
        if not instance or not isinstance(config, dict):
            logger.warning("媒体服务器 %s 配置或实例不可用", name)
            return None

        host = str(config.get("host") or "").strip()
        api_key = str(config.get("apikey") or "").strip()
        if not host or not api_key:
            logger.warning("媒体服务器 %s 缺少 Emby 地址或 API 密钥", name)
            return None
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        host = host.rstrip("/")

        try:
            user = instance.get_user()
        except Exception as error:
            logger.error("获取媒体服务器 %s 用户失败：%s", name, error)
            return None
        if user is None or not str(user).strip():
            logger.warning("媒体服务器 %s 未找到可用 Emby 用户", name)
            return None

        return _EmbyContext(
            name=str(name),
            host=host,
            user=str(user),
            api_key=api_key,
        )

    @staticmethod
    def __matches_media_name(item: Any, media_name: Optional[str]) -> bool:
        """按旧命令约定匹配剧集标题，并兼容 Emby 标题中的年份后缀。"""
        if not media_name:
            return True
        if not isinstance(item, dict):
            return False
        item_name = str(item.get("Name") or "").strip()
        if not item_name:
            return False
        match = re.fullmatch(r"(.+?)\s+\(\d{4}\)", item_name)
        return (match.group(1) if match else item_name) == media_name

    def __sync_series(self, context: _EmbyContext, item: dict) -> None:
        """同步一个剧集及其所有季、集条目，缺失演员信息时保持远端数据不变。"""
        item_id = item.get("Id")
        if not item_id:
            logger.warning("Emby 剧集条目缺少 ID：%s", item)
            return

        logger.info("开始同步媒体：%s，ID：%s", item.get("Name"), item_id)
        item_info = self.__get_item_info(context, item_id)
        if not item_info:
            logger.error("获取媒体详情失败：%s", item_id)
            return

        for season in self.__get_items(context, item_id):
            season_id = season.get("Id") if isinstance(season, dict) else None
            if not season_id:
                continue
            season_info = self.__get_item_info(context, season_id)
            if not season_info:
                logger.error("获取季详情失败：%s", season_id)
                continue

            people = season_info.get("People") or item_info.get("People")
            if not people:
                logger.warning("媒体 %s 未找到演员信息，跳过季 %s", item_id, season_id)
                continue

            for episode in self.__get_items(context, season_id):
                episode_id = episode.get("Id") if isinstance(episode, dict) else None
                if episode_id:
                    self.__sync_episode(context, item, episode_id, people)

    def __sync_episode(
        self,
        context: _EmbyContext,
        series: dict,
        episode_id: str,
        people: Any,
    ) -> None:
        """重试单集更新，并只在成功时停止重试。"""
        for attempt in range(1, 4):
            episode_info = self.__get_item_info(context, episode_id)
            if not episode_info:
                logger.error("获取集详情失败：%s（第 %s/3 次）", episode_id, attempt)
                continue
            if episode_info.get("People") == people:
                logger.info("媒体：%s 的集演员信息已更新", series.get("Name"))
                return

            payload = dict(episode_info)
            locked_fields = payload.get("LockedFields")
            payload["LockedFields"] = (
                list(locked_fields) if isinstance(locked_fields, list) else []
            )
            if "Cast" not in payload["LockedFields"]:
                payload["LockedFields"].append("Cast")
            payload["People"] = people

            updated = self.__update_item_info(context, episode_id, payload)
            logger.info(
                "更新媒体：%s 的集 %s 成功：%s",
                series.get("Name"),
                episode_id,
                updated,
            )
            if updated:
                time.sleep(0.5)
                return
            logger.warning("更新集信息失败：%s（第 %s/3 次）", episode_id, attempt)

    def __request_json(
        self,
        context: _EmbyContext,
        path: str,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        """通过 V3 网络 SDK 请求 Emby JSON，并始终释放响应连接。"""
        response = None
        try:
            request_params = dict(params or {})
            request_params["api_key"] = context.api_key
            response = RequestUtils().get_res(
                url=f"{context.host}/{path.lstrip('/')}",
                params=request_params,
            )
            if response is None or response.status_code != 200:
                return None
            result = response.json()
            return result if isinstance(result, dict) else None
        except Exception as error:
            logger.error("请求 Emby 接口 %s 出错：%s", path, error)
            return None
        finally:
            if response is not None:
                response.close()

    def __get_items(self, context: _EmbyContext, parent_id: Any) -> List[dict]:
        """获取 Emby 父条目的直接子项。"""
        if parent_id is None:
            return []
        result = self.__request_json(
            context,
            f"emby/Users/{context.user}/Items",
            {"ParentId": parent_id},
        )
        items = result.get("Items") if result else None
        return items if isinstance(items, list) else []

    def __get_item_info(self, context: _EmbyContext, item_id: Any) -> dict:
        """获取单个 Emby 条目详情。"""
        if item_id is None:
            return {}
        result = self.__request_json(
            context,
            f"emby/Users/{context.user}/Items/{item_id}",
        )
        return result or {}

    def __update_item_info(
        self,
        context: _EmbyContext,
        item_id: Any,
        data: dict,
    ) -> bool:
        """提交单集详情更新，并始终释放 Emby 写请求响应。"""
        response = None
        try:
            response = RequestUtils(
                headers={"accept": "*/*", "Content-Type": "application/json"}
            ).post_res(
                url=f"{context.host}/emby/Items/{item_id}",
                params={"api_key": context.api_key},
                json=data,
            )
            return response is not None and response.status_code == 204
        except Exception as error:
            logger.error("更新 Emby 条目 %s 出错：%s", item_id, error)
            return False
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令。"""
        return [
            {
                "cmd": "/as",
                "event": EventType.PluginAction,
                "desc": "Emby剧集演员同步",
                "category": "",
                "data": {"action": "actorsync"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """插件不暴露额外 HTTP API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """通过 V3 宿主服务合同注册一次性演员同步任务。"""
        if not self._run_once:
            return []
        return [
            {
                "id": "EmbyActorSync.Once",
                "name": "Emby剧集演员同步（立即运行）",
                "trigger": "date",
                "func": self._run_once_sync,
                "kwargs": {
                    "run_date": datetime.now(tz=pytz.timezone(str(settings.TZ)))
                    + timedelta(seconds=3)
                },
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面和默认配置。"""
        helper = self.mediaserver_helper or MediaServerHelper()
        media_servers = [
            {"title": config.name, "value": config.name}
            for config in helper.get_configs().values()
            if config.type == "emby"
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
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
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "mediaservers",
                                            "label": "媒体服务器",
                                            "items": media_servers,
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
                                            "text": "可选同步媒体库，不选同步所有剧集媒体库。注：只支持Emby。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        """插件不提供额外详情页。"""
        return []

    def stop_service(self) -> None:
        """公共调度任务由宿主撤销，插件没有额外后台资源需要释放。"""
