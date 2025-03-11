import os
import re
import urllib.parse
from pathlib import Path
from typing import List, Tuple, Dict, Any

from app.log import logger
from app.plugins import _PluginBase


class StrmRedirect(_PluginBase):
    # 插件名称
    plugin_name = "Strm重定向"
    # 插件描述
    plugin_desc = "重写Strm文件内容。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/softlinkredirect.png"
    # 插件版本
    plugin_version = "1.2.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmredirect_"
    # 加载顺序
    plugin_order = 27
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _onlyonce = False
    _unquote = False
    _strm_path = None
    _origin_path = None
    _redirect_path = None

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._onlyonce = config.get("onlyonce")
            self._unquote = config.get("unquote")
            self._strm_path = config.get("strm_path")
            self._origin_path = config.get("origin_path")
            self._redirect_path = config.get("redirect_path")

            if self._onlyonce and self._strm_path and ((self._origin_path and self._redirect_path) or self._unquote):
                logger.info(f"{self._strm_path} Strm重定向开始 {self._origin_path} - {self._redirect_path}")
                self.update_strm(self._origin_path, self._redirect_path, self._strm_path)
                logger.info(f"{self._strm_path} Strm重定向完成")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": self._onlyonce,
                    "unquote": self._unquote,
                    "strm_path": self._strm_path,
                    "origin_path": self._origin_path,
                    "redirect_path": self._redirect_path
                })

    def update_strm(self, target_from, target_to, directory):
        for root, dirs, files in os.walk(directory):
            for name in dirs + files:
                file_path = os.path.join(root, name)
                if Path(str(file_path)).is_dir():
                    continue
                if Path(str(file_path)).is_file():
                    if Path(str(file_path)).suffix.lower() != ".strm":
                        continue
                    with open(str(file_path), 'r', encoding='utf-8') as file:
                        strm_content = file.read()
                    if not strm_content:
                        continue
                    # unencoded = self.find_unencoded_parts(strm_content)
                    # 解码url
                    unercoded_strm_content = urllib.parse.unquote(strm_content)
                    if self._unquote:
                        with open(str(file_path), 'w', encoding='utf-8') as file:
                            file.write(unercoded_strm_content)
                            logger.info(f"Unquote Strm: {strm_content} -> {unercoded_strm_content} success")
                    if target_from and target_to:
                        if str(unercoded_strm_content).startswith(target_from):
                            strm_content = unercoded_strm_content.replace(target_from, target_to)
                            # no_encoded = unencoded[0]
                            # encoded = strm_content.replace(no_encoded, "")
                            # encoded = urllib.parse.quote(encoded)
                            # strm_content = no_encoded + encoded

                            # 如果不是url，不进行编码
                            if not str(strm_content).startswith("http"):
                                strm_content = urllib.parse.unquote(strm_content)
                            with open(str(file_path), 'w', encoding='utf-8') as file:
                                file.write(strm_content)
                            logger.info(
                                f"Updated Strm: {unercoded_strm_content} -> {strm_content} success")

    @staticmethod
    def find_unencoded_parts(input_string: str):
        # 匹配URL编码的部分
        url_encoded_pattern = re.compile(r'%[0-9A-Fa-f]{2}')

        # 用于存储未编码的部分
        unencoded_parts = []

        # 找到所有的URL编码部分
        last_index = 0
        for match in url_encoded_pattern.finditer(input_string):
            # 提取未编码的部分
            start_index = match.start()
            if start_index > last_index:
                unencoded_parts.append(input_string[last_index:start_index])
            last_index = match.end()

        # 提取最后一部分，可能未被编码
        if last_index < len(input_string):
            unencoded_parts.append(input_string[last_index:])

        return unencoded_parts

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
                                            'model': 'unquote',
                                            'label': '解码URL',
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
                                            'model': 'strm_path',
                                            'label': 'strm路径',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'origin_path',
                                            'label': '源路径',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'redirect_path',
                                            'label': '新路径',
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
                                            'text': '源路径->新路径，将会替换所有.strm文件中的源路径为新路径。'
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
                                            'text': '如想解码Strm中的url路径，仅需勾选解码URL和填写strm路径即可。'
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
            "unquote": False,
            "strm_path": "",
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
