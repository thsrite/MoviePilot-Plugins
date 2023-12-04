from app.core.config import settings
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas import NotificationType


class ScheduleReminder(_PluginBase):
    # 插件名称
    plugin_name = "日程提醒"
    # 插件描述
    plugin_desc = "自定义提醒事项、提醒时间。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/reminder.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "schedulereminder_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _confs = None
    siteoper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.siteoper = SiteOper()

        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._confs = config.get("confs")

            if self._enabled and self._confs:
                # 周期运行
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 读取目录配置
                confs = self._confs.split("\n")
                if not confs:
                    return
                for conf in confs:
                    if str(conf).count(":") != 1:
                        logger.warn(f"{conf} 格式错误，跳过处理")
                        continue
                    try:
                        self._scheduler.add_job(func=self.__send_notify,
                                                trigger=CronTrigger.from_crontab(str(conf).split(":")[1]),
                                                name=f"{str(conf).split(':')[0]}提醒",
                                                kwargs={"theme": str(conf).split(":")[0]})
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __send_notify(self, theme: str):
        """
        同步站点cookie到cookiecloud
        """
        self.post_message(mtype=NotificationType.Manual,
                          title="日程提醒",
                          text=theme)

    def get_state(self) -> bool:
        return self._enabled

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
                'component': 'VForm',
                'content': [
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'confs',
                                            'label': '提醒事项',
                                            'rows': 5,
                                            'placeholder': '提醒内容:cron'
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '提醒事项格式为：提醒内容:提醒时间cron表达式（一行一条）。'
                                                    '需开启（手动处理通知）通知类型'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "confs": "",
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
