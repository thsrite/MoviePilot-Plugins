from datetime import datetime, timedelta

import pytz
from PyCookieCloud import PyCookieCloud

from app.core.config import settings
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class SyncCookieCloud(_PluginBase):
    # 插件名称
    plugin_name = "同步CookieCloud"
    # 插件描述
    plugin_desc = "同步MoviePilot站点Cookie到CookieCloud。"
    # 插件图标
    plugin_icon = "Cookiecloud_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "synccookiecloud_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    siteoper = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.siteoper = SiteOper()
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"同步CookieCloud服务启动，立即运行一次")
                    self._scheduler.add_job(self.__sync_to_cookiecloud, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="同步CookieCloud")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__sync_to_cookiecloud,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="同步CookieCloud")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __sync_to_cookiecloud(self):
        """
        同步站点cookie到cookiecloud
        """
        # 获取所有站点
        sites = self.siteoper.list_order_by_pri()
        if not sites:
            return

        cookie_cloud = PyCookieCloud(settings.COOKIECLOUD_HOST, settings.COOKIECLOUD_KEY, settings.COOKIECLOUD_PASSWORD)
        the_key = cookie_cloud.get_the_key()
        if not the_key:
            logger.error('链接cookiecloud异常，请检查配置')
            return

        cookies = {}
        for site in sites:
            domain = site.domain
            cookie = site.cookie

            if not cookie:
                logger.error(f"站点{domain}无cookie，跳过处理")
                continue

            # 解析cookie
            site_cookies = []
            for ck in cookie.split(";"):
                site_cookies.append({
                    "domain": "audiences.me",
                    "hostOnly": False,
                    "httpOnly": False,
                    "path": "/",
                    "sameSite": "unspecified",
                    "secure": False,
                    "session": True,
                    "storeld": "0",
                    "name": ck.split("=")[0],
                    "value": ck.split("=")[1]
                })

            # 存储cookies
            cookies[domain] = site_cookies

        # 覆盖到cookiecloud
        if cookies:
            success = cookie_cloud.update_cookie(cookies)
            logger.info(f"同步站点cookie到CookieCloud {'成功' if success else '失败'}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron
        })

    def get_state(self) -> bool:
        return False

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
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
