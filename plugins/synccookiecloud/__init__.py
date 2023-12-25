import hashlib
import json
from datetime import datetime, timedelta
from urllib.parse import urljoin

import pytz
import requests
from Cryptodome import Random
from Cryptodome.Cipher import AES
import base64
from hashlib import md5
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
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/cookiecloud.png"
    # 插件版本
    plugin_version = "1.2"
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

        # 停止现有任务
        self.stop_service()

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

        if not settings.COOKIECLOUD_HOST or not settings.COOKIECLOUD_KEY or not settings.COOKIECLOUD_PASSWORD:
            logger.error('cookiecloud配置错误，请检查配置')
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
                    "domain": domain,
                    "sameSite": "unspecified",
                    "path": "/",
                    "name": ck.split("=")[0],
                    "value": ck.split("=")[1]
                })

            # 存储cookies
            cookies[domain] = site_cookies

        # 覆盖到cookiecloud
        if cookies:
            success = self.__update_cookie(cookies)

            logger.info(cookies)
            logger.info(f"同步站点cookie到CookieCloud {'成功' if success else '失败'}")

    def __update_cookie(self, cookie: Dict[str, Any]) -> bool:
        """
        Update cookie data to CookieCloud.

        :param cookie: cookie value to update, if this cookie does not contain 'cookie_data' key, it will be added into 'cookie_data'.
        :return: if update success, return True, else return False.
        """
        if 'cookie_data' not in cookie:
            cookie = {'cookie_data': cookie}
        raw_data = json.dumps(cookie)
        encrypted_data = self.__encrypt(raw_data.encode('utf-8'), self.__get_the_key().encode('utf-8')).decode('utf-8')
        cookie_cloud_request = requests.post(urljoin(settings.COOKIECLOUD_HOST, '/update'),
                                             data={'uuid': settings.COOKIECLOUD_KEY, 'encrypted': encrypted_data})
        if cookie_cloud_request.status_code == 200:
            if cookie_cloud_request.json()['action'] == 'done':
                return True
        return False

    def __encrypt(self, message, passphrase):
        salt = Random.new().read(8)
        key_iv = self.__bytes_to_key(passphrase, salt, 32 + 16)
        key = key_iv[:32]
        iv = key_iv[32:]
        aes = AES.new(key, AES.MODE_CBC, iv)
        return base64.b64encode(b"Salted__" + salt + aes.encrypt(self.__pad(message)))

    @staticmethod
    def __pad(data):
        BLOCK_SIZE = 16
        length = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
        return data + (chr(length) * length).encode()

    @staticmethod
    def __bytes_to_key(data, salt, output=48):
        # extended from https://gist.github.com/gsakkis/4546068
        assert len(salt) == 8, len(salt)
        data += salt
        key = md5(data).digest()
        final_key = key
        while len(final_key) < output:
            key = md5(key + data).digest()
            final_key += key
        return final_key[:output]

    def __get_the_key(self) -> str:
        """
        Get the key used to encrypt and decrypt data.

        :return: the key.
        """
        md5 = hashlib.md5()
        md5.update((settings.COOKIECLOUD_KEY + '-' + settings.COOKIECLOUD_PASSWORD).encode('utf-8'))
        return md5.hexdigest()[:16]

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron
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
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))