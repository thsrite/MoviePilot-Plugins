import os
from typing import List, Tuple, Dict, Any
from app.log import logger
from app.plugins import _PluginBase


class SoftLinkRedirect(_PluginBase):
    # 插件名称
    plugin_name = "软连接重定向"
    # 插件描述
    plugin_desc = "重定向软连接指向。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/softlinkredirect.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "softlinkredirect_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _onlyonce = False
    _soft_path = None
    _origin_path = None
    _redirect_path = None

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._onlyonce = config.get("onlyonce")
            self._soft_path = config.get("soft_path")
            self._origin_path = config.get("origin_path")
            self._redirect_path = config.get("redirect_path")

            if self._onlyonce and self._soft_path and self._origin_path and self._redirect_path:
                logger.info(f"{self._soft_path} 软连接重定向开始 {self._origin_path} - {self._redirect_path}")
                self.update_symlink(self._origin_path, self._redirect_path, self._soft_path)
                logger.info(f"{self._soft_path} 软连接重定向完成")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": self._onlyonce,
                    "soft_path": self._soft_path,
                    "origin_path": self._origin_path,
                    "redirect_path": self._redirect_path
                })

    @staticmethod
    def update_symlink(target_from, target_to, directory):
        for root, dirs, files in os.walk(directory):
            for name in dirs + files:
                file_path = os.path.join(root, name)
                if os.path.islink(file_path):
                    current_target = os.readlink(file_path)
                    if str(current_target).startswith(target_from):
                        new_target = current_target.replace(target_from, target_to)
                        os.remove(file_path)
                        os.symlink(new_target, file_path)
                        print(f"Updated symlink: {file_path} -> {new_target}")

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        return []

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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'soft_path',
                                            'label': '软连接路径',
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
                                            'model': 'origin_path',
                                            'label': '原来源文件路径',
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
                                            'model': 'redirect_path',
                                            'label': '重定向源文件路径',
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
                                            'text': '软连接指向由A路径改为B路径'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "onlyonce": False,
            "soft_path": "",
            "origin_path": "",
            "redirect_path": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def get_state(self):
        return False

    def stop_service(self):
        """
        退出插件
        """
        pass
