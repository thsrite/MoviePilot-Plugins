import json
import time
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.modules.emby import Emby
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyActorSync(_PluginBase):
    # 插件名称
    plugin_name = "Emby剧集演员同步"
    # 插件描述
    plugin_desc = "同步剧演员信息到集演员信息。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/embyactorsync.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyactorsync_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    _onlyonce = False
    _enabled = False
    _librarys = None
    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_USER = Emby().get_user()
    _EMBY_APIKEY = settings.EMBY_API_KEY
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._librarys = config.get("librarys") or []

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

            # 加载模块
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby剧集演员同步服务启动，立即运行一次")
                    self._scheduler.add_job(self.sync, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby剧集演员同步")

                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "librarys": self._librarys,
            }
        )

    @eventmanager.register(EventType.PluginAction)
    def sync_actor(self, event: Event = None):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "actorsync":
                return

            args = event_data.get("args")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            args_list = args.split(" ")
            if len(args_list) != 2:
                logger.error(f"参数错误：{args_list}")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"参数错误！ /as 媒体库名 剧集名",
                                  userid=event.event_data.get("user"))
                return

            self.sync(args_list[0], args_list[1])

    def sync(self, library_name: str = None, media_name: str = None):
        """
        Emby剧集演员同步
        """
        # 获取媒体库信息
        librarys = Emby().get_librarys()

        # 匹配需要的媒体库
        for library in librarys:
            if str(library.type) != "tvshows":
                continue
            if self._librarys and library.name not in self._librarys:
                continue
            if library_name and library.name != library_name:
                continue

            logger.info(f"开始同步媒体库：{library.name}，ID：{library.id}")
            # 获取媒体库媒体列表
            library_items = self.__get_items(library.id)
            if not library_items:
                logger.error(f"获取媒体库：{library.name}的媒体列表失败")
                continue

            # 遍历媒体列表，获取媒体的ID和名称
            for item in library_items:
                if media_name and item.get("Name") != media_name:
                    continue
                item_info = self.__get_item_info(item.get("Id"))
                seasons = self.__get_items(item.get("Id"))
                for season in seasons:
                    season_items = self.__get_items(season.get("Id"))
                    for season_item in season_items:
                        retry = 0
                        while retry < 3:
                            season_item_info = self.__get_item_info(season_item.get("Id"))
                            try:
                                if season_item_info.get("People") == item_info.get("People"):
                                    logger.warn(
                                        f"媒体：{item.get('Name')} {season_item_info.get('SeasonName')} {season_item_info.get('IndexNumber')} {season_item_info.get('Name')} 演员信息已更新")
                                    retry = 3
                                    continue
                                season_item_info.update({
                                    "People": item_info.get("People")
                                })
                                season_item_info["LockedFields"].append("Cast")
                                flag = self.__update_item_info(season_item.get("Id"), season_item_info)
                                logger.info(
                                    f"更新媒体：{item.get('Name')} {season_item_info.get('SeasonName')} {season_item_info.get('IndexNumber')} {season_item_info.get('Name')} 成功：{flag}")
                                if flag:
                                    retry = 3
                                    time.sleep(0.5)
                                else:
                                    retry += 1
                            except Exception as e:
                                retry += 1
                                logger.error(
                                    f"更新媒体：{item.get('Name')} {season_item_info.get('SeasonName')} {season_item_info.get('IndexNumber')} {season_item_info.get('Name')} 信息出错：{e} 开始重试...{retry} / 3")

        logger.info(f"Emby剧集演员同步完成")

    def __update_item_info(self, item_id, data):
        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json'
        }
        res = RequestUtils(headers=headers).post(
            f"{self._EMBY_HOST}/emby/Items/{item_id}?api_key={self._EMBY_APIKEY}",
            data=json.dumps(data))
        if res and res.status_code == 204:
            return True
        return False

    def __get_items(self, parent_id) -> list:
        """
        获取媒体库媒体列表
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"%semby/Users/%s/Items?ParentId=%s&api_key=%s" % (
            self._EMBY_HOST, self._EMBY_USER, parent_id, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.json().get("Items")
                else:
                    logger.info(f"获取媒体库媒体列表失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接媒体库媒体列表Items出错：" + str(e))
            return []

    def __get_item_info(self, item_id):
        res = RequestUtils().get_res(
            f"{self._EMBY_HOST}/emby/Users/{self._EMBY_USER}/Items/{item_id}?api_key={self._EMBY_APIKEY}")
        if res and res.status_code == 200:
            return res.json()
        return {}

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/as",
                "event": EventType.PluginAction,
                "desc": "Emby剧集演员同步",
                "category": "",
                "data": {
                    "action": "actorsync"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        librarys = Emby().get_librarys()
        library_options = [{'title': library.name, 'value': library.name} for library in librarys if
                           str(library.type) == "tvshows"]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'librarys',
                                            'label': '媒体库',
                                            'items': library_options
                                        }
                                    }
                                ]
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '可选同步媒体库，不选同步所有剧集媒体库。注：只支持Emby。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "librarys": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass
