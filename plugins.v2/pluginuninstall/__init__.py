import shutil
from pathlib import Path

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey


class PluginUnInstall(_PluginBase):
    # 插件名称
    plugin_name = "插件彻底卸载"
    # 插件描述
    plugin_desc = "删除数据库中已安装插件记录、清理插件文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/uninstall.png"
    # 插件版本
    plugin_version = "2.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "pluginuninstall_"
    # 加载顺序
    plugin_order = 98
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _plugin_ids = []
    _clear_config = False
    _clear_data = False

    def init_plugin(self, config: dict = None):
        if config:
            self._plugin_ids = config.get("plugin_ids") or []
            self._clear_config = config.get("clear_config")
            self._clear_data = config.get("clear_data")
            if not self._plugin_ids:
                return

            # 已安装插件
            install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []

            new_install_plugins = []
            for install_plugin in install_plugins:
                if install_plugin in self._plugin_ids:
                    # 移除插件服务
                    Scheduler().remove_plugin_job(install_plugin)
                    # 移除插件
                    PluginManager().remove_plugin(install_plugin)
                    # 删除插件文件
                    plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / install_plugin.lower()
                    if plugin_dir.exists():
                        shutil.rmtree(plugin_dir, ignore_errors=True)
                    if self._clear_config:
                        # 删除配置
                        PluginManager().delete_plugin_config(install_plugin)
                    if self._clear_data:
                        # 删除插件所有数据
                        PluginManager().delete_plugin_data(install_plugin)
                    logger.info(f"插件 {install_plugin} 已卸载")
                else:
                    new_install_plugins.append(install_plugin)

            # 保存已安装插件
            SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, new_install_plugins)

            self.update_config({
                "plugin_ids": [],
                "clear_config": self._clear_config,
                "clear_data": self._clear_data
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
        # 直接调用修复后的 get_local_plugins 获取选项
        pluginOptions = self.get_local_plugins()
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
                                            'model': 'clear_config',
                                            'label': '清除配置(配置信息)',
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
                                            'model': 'clear_data',
                                            'label': '清除数据(运行数据)',
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
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'plugin_ids',
                                            'label': '卸载插件',
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
                                            'text': '删除数据库中已安装插件记录、清理插件文件。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "plugin_ids": [],
            "clear_config": False,
            "clear_data": False
        }

    @staticmethod
    def get_local_plugins():
        """
        获取本地插件
        (修改为只获取已安装插件，避免 compare_version 和市场查询)
        """
        plugin_manager = PluginManager()
        # 获取本地所有插件实例
        local_plugin_instances = plugin_manager.get_local_plugins() or []

        # 过滤出已安装的插件
        installed_plugins = [p for p in local_plugin_instances if getattr(p, 'installed', False)]

        # 根据插件顺序排序 (可选)
        sorted_plugins = sorted(installed_plugins, key=lambda p: getattr(p, 'plugin_order', 1000))

        # 构建 VSelect 需要的选项列表
        plugin_options = []
        for plugin in sorted_plugins:
             # 确保 getattr 有默认值
             plugin_name = getattr(plugin, 'plugin_name', getattr(plugin, 'id', '未知插件'))
             plugin_version = getattr(plugin, 'plugin_version', 'N/A')
             plugin_id = getattr(plugin, 'id', None)
             if plugin_id:
                 plugin_options.append({
                     "title": f"{plugin_name} v{plugin_version}",
                     "value": plugin_id
                 })

        return plugin_options

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass