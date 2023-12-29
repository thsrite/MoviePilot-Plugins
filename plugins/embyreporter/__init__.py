from datetime import datetime, timedelta

import pytz
from telegram.bot import Bot, Request
from telegram import ParseMode
from app.core.config import settings
from app.modules.wechat import WeChat
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins.embyreporter.emby import EmbyService
from app.plugins.embyreporter.ranks_draw import RanksDraw


class EmbyReporter(_PluginBase):
    # 插件名称
    plugin_name = "Emby观影报告"
    # 插件描述
    plugin_desc = "推送Emby观影报告，需Emby安装Playback Report 插件。"
    # 插件图标
    plugin_icon = "Pydiocells_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyreporter_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _res_dir = None
    _cron = None
    _days = None
    _type = None
    _mp_host = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._res_dir = config.get("res_dir")
            self._days = config.get("days") or 7
            self._type = config.get("type") or "tg"
            self._mp_host = config.get("mp_host")

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby观影报告服务启动，立即运行一次")
                    self._scheduler.add_job(self.__report, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby观影报告")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__report,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby观影报告")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __report(self):
        """
        发送Emby观影报告
        """
        # 初始化对象
        emby = EmbyService(settings.EMBY_HOST, settings.EMBY_API_KEY)
        draw = RanksDraw(emby, self._res_dir)

        # 获取数据
        success, movies = emby.get_report(types=emby.PLAYBACK_REPORTING_TYPE_MOVIE, days=self._days, limit=5)
        if not success:
            exit(movies)
        success, tvshows = emby.get_report(types=emby.PLAYBACK_REPORTING_TYPE_TVSHOWS, days=self._days, limit=5)
        if not success:
            exit(tvshows)

        # 绘制海报
        draw.draw(movies, tvshows)
        report_path = draw.save()

        # 发送海报
        if not self._type:
            return

        report_text = f"🌟*过去{self._days}日观影排行*\r\n\r\n"
        if str(self._type) == "tg":
            proxy = Request(proxy_url=settings.PROXY_HOST)
            bot = Bot(token=settings.TELEGRAM_TOKEN, request=proxy)
            bot.send_photo(
                chat_id=settings.TELEGRAM_CHAT_ID,
                photo=open(report_path, "rb"),
                caption=report_text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            logger.info("Emby观影记录推送Telegram成功")
        else:
            # 本地路径转为url
            if not self._mp_host:
                return

            report_url = self._mp_host + report_path.replace("/public", "")
            WeChat().send_msg(title=report_text,
                              image=report_url)
            logger.info("Emby观影记录推送微信应用成功")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "mp_host": self._mp_host,
            "res_dir": self._res_dir
        })

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
        # 编历 NotificationType 枚举，生成消息类型选项
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
                            },
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
                                            'model': 'res_dir',
                                            'label': '素材路径',
                                            'placeholder': '本地素材路径，不传用默认'
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'days',
                                            'label': '报告天数',
                                            'placeholder': '向前获取数据的天数'
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'type',
                                            'label': '推送方式',
                                            'items': [
                                                {'title': 'Telegram', 'value': "tg"},
                                                {'title': '微信', 'value': "wx"}
                                            ]
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'mp_host',
                                            'label': 'MoviePilot域名',
                                            'placeholder': '推送方式非tg可用'
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
                                            'text': 'MoviePilot域名仅在微信推送方式时需要填写。末尾不带/'
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
            "onlyonce": False,
            "cron": "5 1 * * *",
            "res_dir": "",
            "days": 7,
            "mp_host": "",
            "type": "tg"
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
