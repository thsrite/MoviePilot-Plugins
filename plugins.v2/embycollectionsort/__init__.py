import json
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.event import eventmanager, Event
from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils

lock = threading.Lock()


class EmbyCollectionSort(_PluginBase):
    # 插件名称
    plugin_name = "Emby合集媒体排序"
    # 插件描述
    plugin_desc = "Emby保留按照加入时间倒序的前提下，把合集中的媒体按照发布日期排序，修改加入时间已到达顺序排列的目的。"
    # 插件图标
    plugin_icon = "Element_A.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embycollectionsort_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _sort_type = None
    _collection_library_id = None
    _mediaservers = None

    mediaserver_helper = None
    _EMBY_HOST = None
    _EMBY_USER = None
    _EMBY_APIKEY = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._sort_type = config.get("sort_type") or "asc"
            self._collection_library_id = config.get("collection_library_id")
            self._mediaservers = config.get("mediaservers") or []

            # 加载模块
            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby合集媒体排序刷新服务启动，立即运行一次")
                    self._scheduler.add_job(self.collection_sort, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby合集媒体排序")

                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()
                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.collection_sort,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby合集媒体排序")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "enabled": self._enabled,
                "sort_type": self._sort_type,
                "collection_library_id": self._collection_library_id,
                "mediaservers": self._mediaservers,
            }
        )

    def collection_sort(self):
        """
        更改合集媒体入库时间
        """
        if not self._collection_library_id:
            logger.error("未配置合集所在媒体库")
            return

        emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            logger.info(f"开始处理媒体服务器 {emby_name}")
            self._EMBY_USER = emby_server.instance.get_user()
            self._EMBY_APIKEY = emby_server.config.config.get("apikey")
            self._EMBY_HOST = emby_server.config.config.get("host")
            if not self._EMBY_HOST.endswith("/"):
                self._EMBY_HOST += "/"
            if not self._EMBY_HOST.startswith("http"):
                self._EMBY_HOST = "http://" + self._EMBY_HOST

            # 获取合集列表
            collections = self.__get_items(self._collection_library_id)
            handle_times = []

            for collection in collections:
                logger.info(f"开始处理合集: {collection.get('Name')} {collection.get('Id')}")
                items = self.__get_items(collection.get("Id"))
                item_dict = []
                for item in items:
                    item_info = self.__get_item_info(item.get("Id"))
                    item_dict.append({"Name": item.get("Name"), "Id": item.get("Id"), "item_info": item_info})

                # 按照发布时间排序
                sorted_items = sorted(item_dict, key=lambda x: x.get("item_info").get("PremiereDate"),
                                      reverse=self._sort_type == "降序")
                # 初始化时间
                current_time = datetime.strptime(sorted_items[0]["item_info"]["DateCreated"], "%Y-%m-%dT%H:%M:%S.%f0Z")

                # 更新每个 item 的 DateCreated，规则为
                updated_items = []

                while sorted_items:
                    sub_update_items = []

                    for item in sorted_items:
                        with lock:
                            new_date_created = current_time.strftime("%Y-%m-%dT%H:%M:%S.%f0Z")
                            # 时间相同，跳过
                            if str(new_date_created) == str(item['item_info']['DateCreated']):
                                logger.debug(
                                    f"合集媒体: {item.get('Name')} 原入库时间 {item['item_info']['DateCreated']} 新入库时间 {new_date_created} 时间相同，跳过")
                                handle_times.append(str(current_time))
                                sub_update_items.append(str(current_time))
                                # 时间减一秒，用于下一个 item 的更新
                                current_time -= timedelta(seconds=1)
                                continue

                            if str(current_time) in handle_times:
                                logger.warn(
                                    f"合集媒体: {item.get('Name')} {current_time} 时间已被占用，开始增加 {len(sorted_items) + 1} 秒，重新尝试处理")
                                # 处理完成的 items 从列表中移除
                                handle_times = [str(_time) for _time in handle_times if _time not in sub_update_items]
                                # 如果时间已被占用，增加 len(sorted_items) + 1 秒
                                current_time += timedelta(seconds=len(sorted_items) + 1)
                                # 重置已处理的 items 列表和 handle_times 集合
                                updated_items.clear()
                                # 时间已被占用，跳出 for 循环
                                break

                            logger.debug(
                                f"合集媒体: {item.get('Name')} 原入库时间 {item['item_info']['DateCreated']} 新入库时间 {new_date_created}")
                            item["item_info"]["DateCreated"] = new_date_created
                            updated_items.append(item["item_info"])
                            handle_times.append(str(current_time))
                            sub_update_items.append(str(current_time))
                            # 时间减一秒，用于下一个 item 的更新
                            current_time -= timedelta(seconds=1)
                    else:
                        # 所有 item 处理完成，跳出 while 循环
                        break
                    time.sleep(1)

                if not updated_items:
                    logger.warn(f"合集: {collection.get('Name')} {collection.get('Id')} 无需更新入库时间")
                    continue

                logger.debug(f"获取合集排序后最新的入库时间: {current_time}")

                # 更新入库时间
                for item_info in updated_items:
                    update_flag = self.__update_item_info(item_info.get("Id"), item_info)
                    if update_flag:
                        logger.info(f"{item_info.get('Name')} 更新入库时间到{item_info.get('DateCreated')}成功")
                    else:
                        logger.error(f"{item_info.get('Name')} 更新入库时间到{item_info.get('DateCreated')}失败")

                logger.info(f"合集处理完成: {collection.get('Name')} {collection.get('Id')}")

            logger.info(f"更新 {emby_name} 合集媒体排序完成")

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程刷新媒体库
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "collection_sort":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始更新Emby合集媒体排序 ...",
                              userid=event.event_data.get("user"))
        self.collection_sort()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="更新Emby合集媒体排序完成！", userid=event.event_data.get("user"))

    def __get_items(self, parent_id):
        res = RequestUtils().get_res(
            f"{self._EMBY_HOST}/emby/Users/{self._EMBY_USER}/Items?ParentId={parent_id}&api_key={self._EMBY_APIKEY}")
        if res and res.status_code == 200:
            results = res.json().get("Items") or []
            return results
        return []

    def __get_item_info(self, item_id):
        res = RequestUtils().get_res(
            f"{self._EMBY_HOST}/emby/Users/{self._EMBY_USER}/Items/{item_id}?api_key={self._EMBY_APIKEY}")
        if res and res.status_code == 200:
            return res.json()
        return {}

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

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/collection_sort",
            "event": EventType.PluginAction,
            "desc": "更新Emby合集媒体排序",
            "category": "",
            "data": {
                "action": "collection_sort"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'collection_library_id',
                                            'label': '合集媒体库ID'
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'sort_type',
                                            'label': '发布日期',
                                            'items': [
                                                {'title': '升序', 'value': '升序'},
                                                {'title': '降序', 'value': '降序'},
                                            ]
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values() if
                                                      config.type == "emby"]
                                        }
                                    }
                                ]
                            }
                        ]
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
                                            'text': '保留按照加入时间倒序的前提下，把合集中的媒体放一块，不用到处找。注：只支持Emby。'
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
            "sort_type": "降序",
            "cron": "5 1 * * *",
            "collection_library_id": "",
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
