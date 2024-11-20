import re
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from requests import Session

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db import db_query
from app.db.models import MediaServerItem
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyMetaTag(_PluginBase):
    # 插件名称
    plugin_name = "Emby媒体标签"
    # 插件描述
    plugin_desc = "自动给媒体库媒体添加标签。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/tag.png"
    # 插件版本
    plugin_version = "1.4"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embymetatag_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _tag_confs = None
    _aac_confs = None
    _name_tag_confs = None
    _mediaservers = None

    mediaserver_helper = None
    _EMBY_HOST = None
    _EMBY_USER = None
    _EMBY_APIKEY = None
    _scheduler: Optional[BackgroundScheduler] = None

    _tags = {}
    _acc_tags = []
    _media_tags = {}
    _media_type = {}

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._tag_confs = config.get("tag_confs")
            self._aac_confs = config.get("aac_confs")
            self._name_tag_confs = config.get("name_tag_confs")
            self._mediaservers = config.get("mediaservers") or []

            self._tags = {}
            if self._tag_confs:
                tag_confs = self._tag_confs.split("\n")
                for tag_conf in tag_confs:
                    if tag_conf:
                        tag_conf = tag_conf.split("#")
                        if len(tag_conf) == 2:
                            librarys = tag_conf[0].split(',')
                            for library in librarys:
                                library_tags = self._tags.get(library) or []
                                self._tags[library] = library_tags + tag_conf[1].split(',')

            self._acc_tags = []
            if self._aac_confs:
                aac_confs = self._aac_confs.split("\n")
                for acc_conf in aac_confs:
                    if acc_conf:
                        acc_conf = acc_conf.split("#")
                        if len(acc_conf) == 2:
                            acc_regex = acc_conf[0]
                            self._acc_tags.append({"regex": acc_regex, "tags": acc_conf[1].split(',')})

            self._media_tags = {}
            self._media_type = {}
            if self._name_tag_confs:
                name_tag_confs = self._name_tag_confs.split("\n")
                for name_tag_conf in name_tag_confs:
                    if name_tag_conf:
                        name_tag_conf = name_tag_conf.split("#")
                        if len(name_tag_conf) == 3:
                            media_names = name_tag_conf[0].split(',')
                            for media_name in media_names:
                                self._media_type[media_name] = name_tag_conf[1].split(',')
                                media_tags = self._media_tags.get(media_name) or []
                                self._media_tags[media_name] = media_tags + name_tag_conf[2].split(',')

            # 加载模块
            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby媒体标签服务启动，立即运行一次")
                    self._scheduler.add_job(self.auto_tag, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby媒体标签")

                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()
                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.auto_tag,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby媒体标签")
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
                "tag_confs": self._tag_confs,
                "aac_confs": self._aac_confs,
                "name_tag_confs": self._name_tag_confs,
                "mediaservers": self._mediaservers,
            }
        )

    def auto_tag(self):
        """
        给设定媒体库打标签
        """
        if (not self._tags or len(self._tags.keys()) == 0) and (
                not self._acc_tags or len(self._acc_tags) == 0) and (
                not self._media_tags or len(self._media_tags.keys()) == 0):
            logger.error("未配置Emby媒体标签")
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

            # 媒体库标签
            if self._tags and len(self._tags.keys()) > 0:
                # 获取emby 媒体库
                librarys = emby_server.instance.get_librarys()
                if not librarys:
                    logger.error("获取媒体库失败")
                    return

                # 遍历媒体库，获取媒体库媒体
                for library in librarys:
                    # 获取媒体库标签
                    library_tags = self._tags.get(library.name)
                    if not library_tags:
                        continue

                    # 获取媒体库媒体
                    library_items = emby_server.instance.get_items(library.id)
                    if not library_items:
                        continue

                    for library_item in library_items:
                        if not library_item:
                            continue

                        # 给媒体添加tag
                        self.__add_tags(item_name=library_item.title,
                                        item_id=library_item.item_id,
                                        media_tags=library_tags,
                                        type=library.name)

            # 特殊媒体名标签
            if self._media_tags and len(self._media_tags.keys()) > 0:
                for media_name, media_tags in self._media_tags.items():
                    match_medias = []
                    # 根据Series/Movie搜索媒体
                    for media_type in self._media_type.get(media_name):
                        match_medias += self.__get_medias_by_name(media_name, media_type)

                    # 遍历媒体 补充缺失tag
                    for media in match_medias:
                        if not media:
                            continue
                        # 给媒体添加tag
                        self.__add_tags(item_name=media.get("Name"),
                                        item_id=media.get("Id"),
                                        media_tags=media_tags,
                                        type="特殊媒体")

            # 媒体音频标签
            if self._acc_tags and len(self._acc_tags) > 0:
                media_items = self.__get_media_items(db=None)
                logger.info(f"获取到同步媒体数据：{len(media_items)} 个")

                for media_item in media_items:
                    if not media_item:
                        continue

                    if media_item.item_type == "电影":
                        item_id = media_item.item_id
                    else:
                        # 获取电视剧的媒体信息
                        __media_items = self.__get_items(media_item.item_id)
                        if not __media_items:
                            continue
                        item_id = __media_items[0].get("Id")

                    # 获取item的媒体信息
                    media_accs = self.__get_item_info(item_id)
                    if not media_accs:
                        continue

                    logger.info(f"获取到媒体音频数据：{media_item.item_type} {media_item.title} {media_accs}")

                    # 遍历媒体音频 匹配正则
                    add_tags = []
                    for acc_tag in self._acc_tags:
                        acc_regex = acc_tag.get("regex")
                        acc_tags = acc_tag.get("tags")

                        match_flag = False
                        for media_acc in media_accs:
                            if re.search(acc_regex, media_acc):
                                match_flag = True
                                break
                        if not match_flag:
                            continue

                        logger.info(f"匹配到媒体音频：{media_item.item_type} {media_item.title} {acc_tags}")
                        add_tags += acc_tags

                    # 给媒体添加tag
                    logger.info(f"开始给媒体添加标签：{media_item.item_type} {media_item.title} {add_tags}")
                    self.__add_tags(item_name=media_item.title,
                                    item_id=media_item.item_id,
                                    media_tags=add_tags,
                                    type="媒体音频")

            logger.info(f"{emby_name} 媒体标签任务完成")

    def __add_tags(self, item_name, item_id, media_tags, type):
        """
        给单个项目添加标签
        """
        # 获取item的tag
        item_tags = self.__get_item_tags(item_id) or []

        # 获取缺少的tag
        add_tags = []
        for media_tag in media_tags:
            if not item_tags or media_tag not in item_tags:
                add_tags.append(media_tag)

        # 添加标签
        if add_tags:
            tags = [{"Name": str(add_tag)} for add_tag in add_tags]
            tags = {"Tags": tags}
            add_flag = self.__add_tag(item_id, tags)
            logger.info(f"{type} 添加标签成功：{item_name} {tags} {add_flag}")

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程添加媒体标签
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "emby_meta_tag":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始添加媒体标签 ...",
                              userid=event.event_data.get("user"))
        self.auto_tag()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="添加媒体标签完成！", userid=event.event_data.get("user"))

    def __add_tag(self, itemid: str, tags: dict):
        req_url = "%semby/Items/%s/Tags/Add?api_key=%s" % (self._EMBY_HOST, itemid, self._EMBY_APIKEY)
        try:
            with RequestUtils(content_type="application/json").post_res(url=req_url, json=tags) as res:
                if res and res.status_code == 204:
                    return True
        except Exception as e:
            logger.error(f"连接Items/Id/Tags/Add出错：" + str(e))
        return False

    def __get_item_tags(self, itemid: str):
        """
        获取单个项目详情
        """
        if not itemid:
            return None
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = "%semby/Users/%s/Items/%s?api_key=%s" % (self._EMBY_HOST, self._EMBY_USER, itemid, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res and res.status_code == 200:
                    item = res.json()
                    return [tag.get('Name') for tag in item.get("TagItems")]
        except Exception as e:
            logger.error(f"连接Items/Id出错：" + str(e))
        return []

    def __get_items(self, itemid: str):
        """
        获取剧集媒体信息
        """
        if not itemid:
            return None
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = "%semby/Users/%s/Items?api_key=%s&Limit=1&Recursive=true&ParentId=%s&IsFolder=false" % (
            self._EMBY_HOST, self._EMBY_USER, self._EMBY_APIKEY, itemid)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res and res.status_code == 200:
                    item = res.json()
                    return item.get("Items", {})
        except Exception as e:
            logger.error(f"连接Items/Id出错：" + str(e))
        return []

    def __get_item_info(self, itemid: str):
        """
        获取单个项目media信息
        """
        if not itemid:
            return None
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = "%semby/Items/%s/PlaybackInfo?UserId=%s&api_key=%s" % (
            self._EMBY_HOST, itemid, self._EMBY_USER, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res and res.status_code == 200:
                    item = res.json()
                    return [media_stream.get('Title') or media_stream.get('Language') for media_stream in
                            item.get("MediaSources", {})[0].get("MediaStreams", []) if
                            media_stream.get('Type') == 'Audio' and (
                                    media_stream.get('Title') or media_stream.get('Language'))]
        except Exception as e:
            logger.error(f"连接Items/Id/PlaybackInfo出错：" + str(e))
        return []

    def __get_medias_by_name(self, media_name: str, media_type: str):
        """
        搜索媒体名
        """
        if not media_name:
            return None
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = ("%semby/Users/%s/Items?IncludeItemTypes=%s&Recursive=true&SearchTerm=%s&api_key=%s") % (
            self._EMBY_HOST, self._EMBY_USER, media_type, media_name, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res and res.status_code == 200:
                    item = res.json()
                    return item.get("Items")
        except Exception as e:
            logger.error(f"连接Items/Id出错：" + str(e))
        return []

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/emby_meta_tag",
            "event": EventType.PluginAction,
            "desc": "Emby媒体标签",
            "category": "",
            "data": {
                "action": "emby_meta_tag"
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
                                    'md': 6
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
                                    'md': 6
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
                            }
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'aac_confs',
                                            'label': '媒体音频标签配置',
                                            'rows': 3,
                                            'placeholder': 'cantonese|粤语|粤语Cantonese|Cantonese#标签名,标签名'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'tag_confs',
                                            'label': '媒体库标签配置',
                                            'rows': 3,
                                            'placeholder': '媒体库名,媒体库名#标签名,标签名'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'name_tag_confs',
                                            'label': '媒体名标签配置',
                                            'rows': 3,
                                            'placeholder': '媒体名称,媒体名称#Series,Movie#标签名,标签名'
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
                                            'text': '定时刷新Emby媒体库媒体，添加媒体库、媒体名（模糊匹配）自定义标签。'
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
            "cron": "5 1 * * *",
            "tag_confs": "",
            "name_tag_confs": "",
            "aac_confs": "",
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

    @staticmethod
    @db_query
    def __get_media_items(db: Optional[Session]) -> List[MediaServerItem]:
        """
        根据下载记录hash查询下载记录
        """
        return MediaServerItem.list(db)
