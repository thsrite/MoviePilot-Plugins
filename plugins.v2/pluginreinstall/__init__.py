import re

from fastapi import APIRouter

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas.types import SystemConfigKey, EventType
from app.utils.string import StringUtils
from app.scheduler import Scheduler

router = APIRouter()


class PluginReInstall(_PluginBase):
    # 插件名称
    plugin_name = "插件重装重载"
    # 插件描述
    plugin_desc = "强制重载、强制重装已安装插件。"
    # 插件图标
    plugin_icon = "refresh.png"
    # 插件版本
    plugin_version = "2.0.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "pluginreinstall_"
    # 加载顺序
    plugin_order = 98
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _reload = False
    _plugin_ids = []
    _plugin_url = []
    _base_url = "https://raw.githubusercontent.com/%s/%s/main/"

    def init_plugin(self, config: dict = None):
        if config:
            self._reload = config.get("reload")
            self._enabled = config.get("enabled")
            self._plugin_ids = config.get("plugin_ids") or []
            if not self._plugin_ids:
                return
            self._plugin_url = config.get("plugin_url")

            # 仅重载插件
            if self._reload:
                for plugin_id in self._plugin_ids:
                    self.__reload_plugin(plugin_id)
                    logger.info(f"插件 {plugin_id} 热重载成功")
                self.__update_conifg()
            else:
                # 校验插件仓库格式
                plugin_url = None
                if self._plugin_url:
                    pattern = "https://github.com/(.*?)/(.*?)/"
                    matches = re.findall(pattern, str(self._plugin_url))
                    if not matches:
                        logger.warn(f"指定插件仓库地址 {self._plugin_url} 错误，将使用插件默认地址重装")
                        self._plugin_url = ""

                    user, repo = PluginHelper().get_repo_info(self._plugin_url)
                    plugin_url = self._base_url % (user, repo)

                self.__update_conifg()

                # 本地插件
                local_plugins = PluginManager().get_local_plugins()

                # 开始重载插件
                for plugin in local_plugins:
                    if plugin.id in self._plugin_ids:
                        logger.info(
                            f"开始重载插件 {plugin.plugin_name} v{plugin.plugin_version}")

                        # 开始安装线上插件
                        state, msg = PluginHelper().install(pid=plugin.id,
                                                            repo_url=plugin_url or plugin.repo_url)
                        # 安装失败
                        if not state:
                            logger.error(
                                f"插件 {plugin.plugin_name} 重装失败，当前版本 v{plugin.plugin_version}")
                            continue

                        logger.info(
                            f"插件 {plugin.plugin_name} 重装成功，当前版本 v{plugin.plugin_version}")

                        self.__reload_plugin(plugin.id)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or (
                    event_data.get("action") != "plugin_reinstall" and event_data.get("action") != "plugin_reload"):
                return
            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            # 本地插件
            local_plugins = PluginManager().get_local_plugins()

            # 开始重载插件
            found = False
            for plugin in local_plugins:
                if str(args) == plugin.plugin_name:
                    found = True
                    if event_data.get("action") == "plugin_reinstall":
                        logger.info(
                            f"开始重装插件 {plugin.plugin_name} v{plugin.plugin_version}")

                        # 校验插件仓库格式
                        plugin_url = None
                        if self._plugin_url:
                            pattern = "https://github.com/(.*?)/(.*?)/"
                            matches = re.findall(pattern, str(self._plugin_url))
                            if not matches:
                                logger.warn(f"指定插件仓库地址 {self._plugin_url} 错误，将使用插件默认地址重装")
                                self._plugin_url = ""

                            user, repo = PluginHelper().get_repo_info(self._plugin_url)
                            plugin_url = self._base_url % (user, repo)

                        # 开始安装线上插件
                        state, msg = PluginHelper().install(pid=plugin.id,
                                                            repo_url=plugin_url or plugin.repo_url)
                        # 安装失败
                        if not state:
                            log_msg = f"插件 {plugin.plugin_name} 重装失败，当前版本 v{plugin.plugin_version}"
                            logger.error(log_msg)
                        else:
                            log_msg = f"插件 {plugin.plugin_name} 重装成功，当前版本 v{plugin.plugin_version}"
                            logger.info(log_msg)

                        self.__reload_plugin(plugin.id)

                        self.post_message(channel=event.event_data.get("channel"),
                                          title=log_msg,
                                          userid=event.event_data.get("user"))
                    else:
                        self.__reload_plugin(plugin.id)
                        logger.info(f"插件 {args} {plugin.id} 热重载成功")
                        self.post_message(channel=event.event_data.get("channel"),
                                          title=f"插件 {args} {plugin.id} 热重载成功",
                                          userid=event.event_data.get("user"))
                    break

            if not found:
                logger.error(f"未找到插件：{args}")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"未找到插件：{args}",
                                  userid=event.event_data.get("user"))

    def __update_conifg(self):
        self.update_config({
            "enabled": self._enabled,
            "reload": self._reload,
            "plugin_url": self._plugin_url,
        })

    def __reload_plugin(self, plugin_id):
        """
        重载插件
        """
        # 加载插件到内存
        PluginManager().reload_plugin(plugin_id)
        # 注册插件服务
        Scheduler().update_plugin_job(plugin_id)
        # 注册插件API
        self.register_plugin_api(plugin_id)

    @staticmethod
    def register_plugin_api(plugin_id: str = None):
        """
        注册插件API（先删除后新增）
        """
        for api in PluginManager().get_plugin_apis(plugin_id):
            for r in router.routes:
                if r.path == api.get("path"):
                    router.routes.remove(r)
                    break
            router.add_api_route(**api)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/reload",
                "event": EventType.PluginAction,
                "desc": "插件重载",
                "category": "",
                "data": {
                    "action": "plugin_reload"
                }
            },
            {
                "cmd": "/reinstall",
                "event": EventType.PluginAction,
                "desc": "插件重装",
                "category": "",
                "data": {
                    "action": "plugin_reinstall"
                }
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                            'label': '开启插件',
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
                                            'model': 'reload',
                                            'label': '仅重载',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'plugin_ids',
                                            'label': '重装插件',
                                            'items': pluginOptions
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 8
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'plugin_url',
                                            'label': '仓库地址',
                                            'placeholder': 'https://github.com/%s/%s/'
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
                                            'text': '选择已安装的本地插件，强制安装插件市场最新版本。'
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
                                            'text': '支持指定插件仓库地址（https://github.com/%s/%s/）'
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
                                            'text': '仅重载：不会获取最新代码，而是基于本地代码重新加载插件。'
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
                                            'text': '/reinstall 插件名称（强制安装插件），/reload 插件名称（热重载插件）。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "reload": False,
            "plugin_ids": [],
            "plugin_url": "",
        }

    @staticmethod
    def get_local_plugins():
        """
        获取本地插件
        """
        # 已安装插件
        install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []

        local_plugins = {}
        # 线上插件列表
        markets = settings.PLUGIN_MARKET.split(",")
        for market in markets:
            online_plugins = PluginHelper().get_plugins(market) or {}
            for pid, plugin in online_plugins.items():
                if pid in install_plugins:
                    local_plugin = local_plugins.get(pid)
                    if local_plugin:
                        if StringUtils.compare_version(local_plugin.get("plugin_version"), plugin.get("version")) < 0:
                            local_plugins[pid] = {
                                "id": pid,
                                "plugin_name": plugin.get("name"),
                                "repo_url": market,
                                "plugin_version": plugin.get("version")
                            }
                    else:
                        local_plugins[pid] = {
                            "id": pid,
                            "plugin_name": plugin.get("name"),
                            "repo_url": market,
                            "plugin_version": plugin.get("version")
                        }

        return local_plugins

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
