import subprocess

from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.schemas.types import EventType


class CommandExecute(_PluginBase):
    # 插件名称
    plugin_name = "命令执行器"
    # 插件描述
    plugin_desc = "自定义容器命令执行。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/command.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "commandexecute_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _onlyonce = None
    _command = None

    def init_plugin(self, config: dict = None):
        if config:
            self._onlyonce = config.get("onlyonce")
            self._command = config.get("command")

            if self._onlyonce and self._command:
                # 执行SQL语句
                try:
                    for command in self._command.split("\n"):
                        logger.info(f"开始执行命令 {command}")
                        last_output, last_error = self.execute_command(command)
                        logger.info(last_output if last_output else last_error)
                except Exception as e:
                    logger.error(f"命令执行失败 {str(e)}")
                    return
                finally:
                    self._onlyonce = False
                    self.update_config({
                        "onlyonce": self._onlyonce,
                        "command": self._command
                    })

    @staticmethod
    def execute_command(command: str):
        """
        执行命令
        :param command: 命令
        """
        result = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        last_output = None
        last_error = None
        while True:
            error = result.stderr.readline().decode("utf-8")
            if error == '' and result.poll() is not None:
                break
            if error:
                logger.info(error.strip())
                last_error = error.strip()
        while True:
            output = result.stdout.readline().decode("utf-8")
            if output == '' and result.poll() is not None:
                break
            if output:
                logger.info(output.strip())
                last_output = output.strip()

        return last_output, last_error

    @eventmanager.register(EventType.PluginAction)
    def execute(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "command_execute":
                return
            args = event_data.get("args")
            if not args:
                return

            logger.info(f"收到命令，开始执行命令 ...{args}")
            last_output, last_error = self.execute_command(args)
            self.post_message(channel=event.event_data.get("channel"),
                              title="命令执行结果",
                              text=last_output if last_output else last_error,
                              userid=event.event_data.get("user"))

    def get_state(self) -> bool:
        return True

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cmd",
            "event": EventType.PluginAction,
            "desc": "自定义命令执行",
            "category": "",
            "data": {
                "action": "command_execute"
            }
        }]

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
                                            'model': 'onlyonce',
                                            'label': '执行命令'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'command',
                                            'rows': '2',
                                            'label': 'command命令',
                                            'placeholder': '一行一条'
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
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '执行日志将会输出到控制台，请谨慎操作。'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "onlyonce": False,
            "command": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
