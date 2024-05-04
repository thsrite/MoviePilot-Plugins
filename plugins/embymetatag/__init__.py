from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.modules.emby import Emby
from app.utils.http import RequestUtils


class EmbyMetaTag(_PluginBase):
    # 插件名称
    plugin_name = "Emby媒体标签"
    # 插件描述
    plugin_desc = "自动给媒体库媒体添加标签。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/tag.png"
    # 插件版本
    plugin_version = "1.0"
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
    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY
    _EMBY_USER = Emby().get_user()
    _scheduler: Optional[BackgroundScheduler] = None

    _tags = {}

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._tag_confs = config.get("tag_confs")

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

            if self._tag_confs:
                tag_confs = self._tag_confs.split("\n")
                for tag_conf in tag_confs:
                    if tag_conf:
                        tag_conf = tag_conf.split("#")
                        if len(tag_conf) == 2:
                            librarys = tag_conf[0].split(',')
                            for library in librarys:
                                self._tags[library] = tag_conf[1].split(',')

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
            }
        )

    def auto_tag(self):
        """
        给设定媒体库打标签
        """
        if "emby" not in settings.MEDIASERVER:
            logger.error("未配置Emby媒体服务器")
            return

        if not self._tags or len(self._tags.keys()) == 0:
            logger.error("未配置Emby媒体标签")
            return

        # 获取emby 媒体库
        librarys = Emby().get_librarys()
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
            library_items = Emby().get_items(library.id)
            if not library_items:
                continue

            for library_item in library_items:
                if not library_item:
                    continue
                # 获取item的tag
                item_tags = self.__get_item_tags(library_item.item_id) or []

                # 获取缺少的tag
                add_tags = []
                for library_tag in library_tags:
                    if not item_tags or library_tag not in item_tags:
                        add_tags.append(library_tag)

                # 添加标签
                if add_tags:
                    tags = [{"Name": str(add_tag)} for add_tag in add_tags]
                    tags = {"Tags": tags}
                    add_flag = self.__add_tag(library_item.item_id, tags)
                    logger.info(f"添加标签成功：{library.name} {library_item.title} {tags} {add_flag}")

        logger.info("Emby媒体标签任务完成")

    def __add_tag(self, itemid: str, tags: dict):
        req_url = "%semby/Items/%s/Tags/Add?api_key=%s" % (self._EMBY_HOST, itemid, self._EMBY_APIKEY)
        try:
            with RequestUtils(content_type="application/json").post_res(url=req_url, json=tags) as res:
                if res and res.status_code == 204:
                    logger.info(f"{req_url} {res}")
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

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

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
                                    'md': 6
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
                                            'model': 'tag_confs',
                                            'label': '标签配置',
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
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '定时刷新Emby媒体库媒体，添加自定义标签。'
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
