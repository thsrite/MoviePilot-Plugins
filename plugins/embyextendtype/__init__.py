import datetime
import threading
from typing import List, Tuple, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.modules.emby import Emby
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.http import RequestUtils

lock = threading.Lock()


class EmbyExtendType(_PluginBase):
    # 插件名称
    plugin_name = "Emby视频类型检查"
    # 插件描述
    plugin_desc = "定期检查Emby媒体库中是否包含指定的视频类型，发送通知。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/extendtype.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyextendtype_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _notify = False
    _cron = None
    _librarys = None
    _extend = None
    _msgtype = None

    # 退出事件
    _event = threading.Event()

    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._librarys = config.get("librarys") or []
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._extend = config.get("extend")
            self._msgtype = config.get("msgtype")

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 启用目录监控
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.check_extend,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="Emby视频类型检查")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("文件复制服务启动，立即运行一次")
                self._scheduler.add_job(name="Emby视频类型检查", func=self.check_extend, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def check_extend(self):
        """
        检查媒体库中是否包含指定的视频类型
        """
        if not self._extend:
            logger.error("视频类型为空，不进行检查")
            return

        if not self._librarys:
            logger.error("媒体库为空，不进行检查")
            return

        logger.info(f"开始检查媒体库 {self._librarys} 中是否包含 {self._extend} 类型")
        for library in self._librarys:
            library_name, library_id = library.split(" ")
            logger.info(f"开始检查媒体库 {library_name} 中是否包含 {self._extend} 类型")
            library_extends = self.__get_extend_type(library_id)
            if library_extends:
                for extend in self._extend.split(","):
                    if extend in [item.get("Name") for item in library_extends]:
                        logger.info(f"媒体库 {library_name} 中包含 {extend} 类型")
                        # 发送通知
                        if self._notify:
                            mtype = NotificationType.Manual
                            if self._msgtype:
                                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
                            self.post_message(title="Emby视频类型检查",
                                              mtype=mtype,
                                              text=f"媒体库 {library_name} 命中 {extend} 视频类型")
            logger.info(f"媒体库 {library_name} 中全部视频类型检查完毕")

        logger.info(f"媒体库 {self._librarys} 中全部视频类型检查完毕")

    def __get_extend_type(self, parent_id) -> list:
        """
        获取媒体库视频类型
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"%semby/ExtendedVideoTypes?ParentId=%s&Recursive=true&IncludeItemTypes=Episode,Movie&Limit=10&api_key=%s" % (
            self._EMBY_HOST, parent_id, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.json().get("Items")
                else:
                    logger.info(f"获取媒体库视频类型失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接ExtendedVideoTypes出错：" + str(e))
            return []

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "librarys": self._librarys,
            "cron": self._cron,
            "extend": self._extend,
            "notify": self._notify,
            "msgtype": self._msgtype,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        librarys = Emby().get_librarys()
        library_items = [{'title': library.name, 'value': f'{library.name} {library.id}'} for library in librarys]

        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
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
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时全量同步周期',
                                            'placeholder': '5位cron表达式，留空关闭'
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
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'msgtype',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions
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
                                            'model': 'extend',
                                            'label': '视频类型',
                                            'placeholder': '多个英文逗号拼接'
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'librarys',
                                            'label': '媒体库',
                                            'items': library_items
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cron": "",
            "extend": "",
            "librarys": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
