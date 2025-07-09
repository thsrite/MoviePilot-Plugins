from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.schemas import NotificationType
from app.scheduler import Scheduler
from app.schemas.types import EventType
from app.core.event import eventmanager, Event

router = APIRouter()


class PluginAutoUpdate(_PluginBase):
    # 插件名称
    plugin_name = "插件更新管理"
    # 插件描述
    plugin_desc = "监测已安装插件，推送更新提醒，可配置自动更新。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/pluginupdate.png"
    # 插件版本
    plugin_version = "2.0.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "pluginautoupdate_"
    # 加载顺序
    plugin_order = 97
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _update = False
    _notify = False
    _msgtype = None
    _update_ids = []
    _exclude_ids = []

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _plugin_version = {}

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._update = config.get("update")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._update_ids = config.get("update_ids")
            self._exclude_ids = config.get("exclude_ids")

        if self._enabled:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    self._scheduler.add_job(func=self.plugin_update,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="插件自动更新")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce:
                logger.info(f"插件自动更新服务启动，立即运行一次")
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config({
                    "onlyonce": self._onlyonce,
                    "cron": self._cron,
                    "enabled": self._enabled,
                    "update": self._update,
                    "notify": self._notify,
                    "msgtype": self._msgtype,
                    "update_ids": self._update_ids,
                    "exclude_ids": self._exclude_ids,
                })

                self._scheduler.add_job(func=self.plugin_update, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=1),
                                        name="插件自动更新")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @eventmanager.register(EventType.PluginAction)
    def plugin_update(self, event: Event = None):
        """
        插件自动更新
        """
        if not self._enabled:
            logger.error("插件未开启")
            return

        update_forced: bool = False
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "plugin_update":
                return
            logger.info("收到命令，开始插件更新 ...")
            update_forced = True
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始插件更新 ...",
                              userid=event.event_data.get("user"))

        logger.info("插件更新任务开始")
        # 已安装插件
        install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []

        # 在线插件
        online_plugins = PluginManager().get_online_plugins()
        if not online_plugins:
            logger.error("未获取到在线插件，停止运行")
            return

        # 使用字典来存储每个插件的最大版本号
        max_versions = {}
        for plugin in online_plugins:
            if plugin.id not in max_versions or plugin.plugin_version > max_versions[plugin.id]:
                max_versions[plugin.id] = plugin.plugin_version
        # 根据最大版本号来筛选数据
        online_plugins = [plugin for plugin in online_plugins if
                          plugin.plugin_version == max_versions[plugin.id]]

        # 已安装插件版本
        self.__get_install_plugin_version()

        # 系统运行的服务
        schedulers = Scheduler().list()
        running_scheduler = []
        for scheduler in schedulers:
            if scheduler.status == "正在运行":
                running_scheduler.append(scheduler.id)

        title = None
        # 支持更新的插件自动更新
        for plugin in online_plugins:
            # 只处理已安装的插件
            if str(plugin.id) in install_plugins:
                # 有更新 或者 本地未安装的
                if plugin.has_update or not plugin.installed:
                    # 已安装插件版本
                    install_plugin_version = self._plugin_version.get(str(plugin.id))
                    if not install_plugin_version or str(install_plugin_version) == "None":
                        continue

                    version_text = f"更新版本：v{install_plugin_version} -> v{plugin.plugin_version}"

                    # 自动更新
                    if self._update or update_forced:
                        # 判断是否是排除插件
                        if self._exclude_ids and str(plugin.id) in self._exclude_ids:
                            logger.info(f"插件 {plugin.plugin_name} 已被排除自动更新，跳过")
                            continue
                        # 判断是否是已选择插件
                        if self._update_ids and str(plugin.id) not in self._update_ids:
                            logger.info(f"插件 {plugin.plugin_name} 不在自动更新列表中，跳过")
                            continue
                        # 判断当前要升级的插件是否正在运行，正则运行则暂不更新
                        if plugin.id in running_scheduler:
                            msg = f"插件 {plugin.plugin_name} 正在运行，跳过自动升级，最新版本 v{plugin.plugin_version}"
                            logger.info(msg)
                            title = msg
                            continue
                        else:
                            # 下载安装
                            state, msg = PluginHelper().install(pid=plugin.id,
                                                                repo_url=plugin.repo_url)
                            # 安装失败
                            if not state:
                                title = f"插件 {plugin.plugin_name} 更新失败"
                                logger.error(f"{title} {version_text}")
                            else:
                                title = f"插件 {plugin.plugin_name} 更新成功"
                                logger.info(f"{title} {version_text}")

                                self.__send_notify(title=title, plugin=plugin, version_text=version_text)

                                # 加载插件到内存
                                PluginManager().reload_plugin(plugin.id)
                                # 注册插件服务
                                Scheduler().update_plugin_job(plugin.id)
                                # 注册插件API
                                self.register_plugin_api(plugin.id)
                    else:
                        title = f"插件 {plugin.plugin_name} 有更新啦"
                        logger.info(f"{title} {version_text}")
                        self.__send_notify(title=title, plugin=plugin, version_text=version_text)

        # 重载插件管理器
        if not title:
            logger.info("所有插件已是最新版本")
            if event:
                event_data = event.event_data
                if not event_data or event_data.get("action") != "plugin_update":
                    return
                self.post_message(channel=event.event_data.get("channel"),
                                  title="所有插件已是最新版本",
                                  userid=event.event_data.get("user"))

        else:
            if '正在运行，跳过自动升级' in title:
                if event:
                    event_data = event.event_data
                    if not event_data or event_data.get("action") != "plugin_update":
                        return
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=title,
                                      userid=event.event_data.get("user"))

    def __send_notify(self, title: str, plugin, version_text: str):
        # 发送通知
        if self._notify:
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            plugin_icon = plugin.plugin_icon
            if not str(plugin_icon).startswith("http"):
                plugin_icon = f"https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/{plugin_icon}"
            if plugin.history:
                for verison in plugin.history.keys():
                    if str(verison).replace("v", "") == str(plugin.plugin_version).replace("v", ""):
                        version_text += f"\n更新记录：{plugin.history[verison]}"
            self.post_message(title=title,
                              mtype=mtype,
                              text=version_text,
                              image=plugin_icon)

    def __get_install_plugin_version(self):
        """
        获取已安装插件版本
        """
        # 本地插件
        local_plugins = PluginManager().get_local_plugins()
        for plugin in local_plugins:
            self._plugin_version[plugin.id] = plugin.plugin_version

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/plugin_update",
            "event": EventType.PluginAction,
            "desc": "插件更新",
            "category": "",
            "data": {
                "action": "plugin_update"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    @staticmethod
    def register_plugin_api(plugin_id: str = None):
        """
        注册插件API（先删除后新增）
        """
        apis: List[Dict[str, Any]] = []
        for api in PluginManager().get_plugin_apis():
            if plugin_id in api.get("path"):
                apis.append(api)

        for api in apis:
            for r in router.routes:
                if r.path == api.get("path"):
                    router.routes.remove(r)
                    break
            router.add_api_route(**api)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })

        # 已安装插件
        local_plugins = PluginManager().get_local_plugins()
        # 编历 local_plugins，生成插件类型选项
        pluginOptions = []

        for plugin in local_plugins:
            if not plugin.installed:
                continue
            pluginOptions.append({
                "title": f"{plugin.plugin_name} v{plugin.plugin_version}",
                "value": plugin.id
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
                                    'md': 3
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
                                    'md': 3
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'update',
                                            'label': '自动更新',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                    'md': 3
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
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'update_ids',
                                            'label': '更新插件',
                                            'items': pluginOptions
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'exclude_ids',
                                            'label': '排除插件',
                                            'items': pluginOptions
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
                                            'text': '已安装的插件自动更新最新版本。'
                                                    '如未开启自动更新则发送更新通知。'
                                                    '如更新插件正在运行，则本次跳过更新。'
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
                                            'text': '所有已安装插件均会检查更新，发送通知。'
                                                    '更新插件/排除插件仅针对于自动更新场景。'
                                                    '如未选择更新插件，则默认为自动更新所有。'
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
            "update": False,
            "notify": False,
            "cron": "",
            "msgtype": "",
            "update_ids": [],
            "exclude_ids": [],
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
