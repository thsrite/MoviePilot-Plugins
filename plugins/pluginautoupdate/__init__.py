from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas.types import SystemConfigKey


class PluginAutoUpdate(_PluginBase):
    # 插件名称
    plugin_name = "插件自动更新"
    # 插件描述
    plugin_desc = "监测已安装插件，自动更新最新版本。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/pluginupdate.png"
    # 主题色
    plugin_color = "#95eb95"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "pluginautoupdate_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _run_cnt = 0

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")

        if self._enabled:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    self._scheduler.add_job(func=self.__plugin_update,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="插件自动更新")
                    self._run_cnt += 1
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce or self._run_cnt == 0:
                self._run_cnt += 1

                logger.info(f"插件自动更新服务启动，立即运行一次")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "cron": self._cron,
                    "enabled": self._enabled,
                })

                self._scheduler.add_job(func=self.__plugin_update, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=1),
                                        name="插件自动更新")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __plugin_update(self):
        """
        插件自动更新
        """
        # 已安装插件
        install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
        # 在线插件
        online_plugins = PluginManager().get_online_plugins()
        if not online_plugins:
            logger.error("未获取到在线插件，停止运行")
            return

        plugin_reload = False
        # 支持更新的插件自动更新
        for plugin in online_plugins:
            # 只处理已安装的插件
            if str(plugin.get("id")) in install_plugins:
                # 有更新 或者 本地未安装的
                if plugin.get("has_update") or not plugin.get("installed"):
                    # 下载安装
                    state, msg = PluginHelper().install(pid=plugin.get("id"),
                                                        repo_url=plugin.get("repo_url"))
                    # 安装失败
                    if not state:
                        logger.error(
                            f"插件 {plugin.get('plugin_name')} 更新失败，最新版本 {plugin.get('plugin_version')}")
                        continue

                    logger.info(f"插件 {plugin.get('plugin_name')} 更新成功，最新版本 {plugin.get('plugin_version')}")
                    plugin_reload = True

        # 重载插件管理器
        if plugin_reload:
            logger.info("开始插件重载")
            PluginManager().init_config()
        else:
            logger.info("所有插件已是最新版本")

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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '监测周期',
                                            'placeholder': '5位cron表达式'
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
                                            'text': '已安装的三方插件重装容器自动安装。'
                                                    '已安装的插件自动更新最新版本。'
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
            "cron": ""
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
            pass
            # logger.error("退出插件失败：%s" % str(e))
