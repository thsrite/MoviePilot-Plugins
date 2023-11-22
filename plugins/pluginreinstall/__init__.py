import re

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.string import StringUtils


class PluginReInstall(_PluginBase):
    # 插件名称
    plugin_name = "插件强制重装"
    # 插件描述
    plugin_desc = "卸载当前插件，强制重装。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/reinstall.png"
    # 主题色
    plugin_color = "#3c78d8"
    # 插件版本
    plugin_version = "1.2"
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
    _plugin_ids = []
    _plugin_url = []

    def init_plugin(self, config: dict = None):
        if config:
            self._plugin_ids = config.get("plugin_ids") or []
            if not self._plugin_ids:
                return
            self._plugin_url = config.get("plugin_url")

            # 校验插件仓库格式
            pattern = "https://raw.githubusercontent.com/(.*?)/(.*?)/main/"
            matches = re.findall(pattern, str(self._plugin_url))
            if not matches:
                logger.error(f"指定插件仓库地址 {self._plugin_url} 错误，将使用插件默认地址重装")
                self._plugin_url = ""

            self.update_config({
                "plugin_url": self._plugin_url
            })

            # 本地插件
            local_plugins = self.get_local_plugins()

            # 开始重载插件
            plugin_reload = False
            for plugin_id in list(local_plugins.keys()):
                local_plugin = local_plugins.get(plugin_id)
                if plugin_id in self._plugin_ids:
                    logger.info(f"开始重载插件 {local_plugin.get('plugin_name')} {local_plugin.get('plugin_version')}")

                    # 开始安装线上插件
                    state, msg = PluginHelper().install(pid=plugin_id,
                                                        repo_url=str(self._plugin_url) or local_plugin.get("repo_url"))
                    # 安装失败
                    if not state:
                        logger.error(
                            f"插件 {local_plugin.get('plugin_name')} 重装失败，当前版本 {local_plugin.get('plugin_version')}")
                        continue

                    logger.info(
                        f"插件 {local_plugin.get('plugin_name')} 重装成功，当前版本 {local_plugin.get('plugin_version')}")
                    plugin_reload = True

            # 重载插件管理器
            if plugin_reload:
                logger.info("开始插件重载")
                PluginManager().init_config()

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
        # 已安装插件
        local_plugins = self.get_local_plugins()
        # 编历 local_plugins，生成插件类型选项
        pluginOptions = []

        for plugin_id in list(local_plugins.keys()):
            local_plugin = local_plugins.get(plugin_id)
            pluginOptions.append({
                "title": f"{local_plugin.get('plugin_name')} {local_plugin.get('plugin_version')}",
                "value": local_plugin.get("id")
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
                                            'placeholder': 'https://raw.githubusercontent.com/%s/%s/main/'
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
                                            'text': '支持指定插件仓库地址（https://raw.githubusercontent.com/%s/%s/main/）'
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
                                            'text': '点击保存卡住请稍等一会，等其他线程执行完先。重载完有提示。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "plugin_ids": [],
            "plugin_url": ""
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
