import subprocess
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any

from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class BbDown(_PluginBase):
    # 插件名称
    plugin_name = "bbdown"
    # 插件描述
    plugin_desc = "交互下载B站视频，调用BBDown。"
    # 插件图标
    plugin_icon = "Bilibili_E.png"
    # 插件版本
    plugin_version = "1.0.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "bbdown_"
    # 加载顺序
    plugin_order = 66
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enable = False
    _bbdown_path = False
    _save_path = None
    _origin_path = None
    _redirect_path = None

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enable = config.get("enable")
            self._bbdown_path = config.get("bbdown_path")
            self._save_path = config.get("save_path")

    @eventmanager.register(EventType.PluginAction)
    def bbdown_action(self, event: Event = None):
        """
        获取CloudDrive2信息
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "bbdown":
                return

            args = event_data.get("arg_str")
            if not args:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"参数错误！/bbdown <url> [command] [options]",
                                  userid=event.event_data.get("user"))
                return

            bbdown_path = Path(self._bbdown_path) / "bbdown"
            ffmpeg_path = Path(self._bbdown_path) / "ffmpeg"
            if not bbdown_path.exists() or not ffmpeg_path.exists():
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"BBDown路径下BBDwon或ffmpeg不存在！请检查配置！。",
                                  userid=event.event_data.get("user"))
                return

            # 赋予执行权限
            self.__execute_command(f'chmod +x {bbdown_path} {ffmpeg_path}')
            logger.info(f"赋予执行权限：{bbdown_path} {ffmpeg_path}")

            # 执行命令
            command = f"cd {self._bbdown_path} && ./bbdown {args} {f'--work-dir {self._save_path}' if self._save_path else ''}"
            logger.info(f"执行命令：{command}")

            self.post_message(channel=event.event_data.get("channel"),
                              title=f"BBDown命令提交成功，请耐心等候！",
                              text=f"保存路径：{self._save_path}" if self._save_path else None,
                              userid=event.event_data.get("user"))

            # output = self.__execute_command(command=command)
            # 创建命令执行对象
            executor = CommandExecutor()
            executor.set_input_callback(self.bbdown_input)

            # 执行命令
            output = executor.execute_command(command)

            logger.info(f"命令输出：{output}")

            self.post_message(channel=event.event_data.get("channel"),
                              title=f"执行命令成功！",
                              text=f"{output[-1]}",
                              userid=event.event_data.get("user"))

    # 外部方法提供输入
    @eventmanager.register(EventType.PluginAction)
    def bbdown_input(self, event: Event = None):
        time.sleep(5)  # 模拟等待其他操作
        return "输入的数据"

    def __execute_command(self, command: str):
        """
        执行命令
        :param command: 命令
        """
        result = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ouptut = []
        while True:
            error = result.stderr.readline().decode("utf-8")
            if error == '' and result.poll() is not None:
                break
            if error:
                logger.info(error.strip())
                ouptut.append(error.strip())
        while True:
            output = result.stdout.readline().decode("utf-8")
            if output == '' and result.poll() is not None:
                break
            if output:
                logger.info(output.strip())
                ouptut.append(output.strip())

        return ouptut

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/bbdown",
                "event": EventType.PluginAction,
                "desc": "BBDown下载",
                "category": "",
                "data": {
                    "action": "bbdown"
                }
            }
        ]

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
                                            'model': 'enable',
                                            'label': '开启插件',
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
                                            'model': 'bbdown_path',
                                            'label': 'BBDown路径',
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
                                            'model': 'save_path',
                                            'label': '保存路径（请确保有访问权限）',
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
                                'component': 'VAlert',
                                'props': {
                                    'type': 'success',
                                    'variant': 'tonal'
                                },
                                'content': [
                                    {
                                        'component': 'a',
                                        'props': {
                                            'href': 'https://github.com/nilaoda/BBDown/blob/master/README.md',
                                            'target': '_blank'
                                        },
                                        'text': '交互命令：/bbdown <url> [command] [options]。BBDown路径：存放BBDown、ffmpeg、data等文件。'
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enable": False,
            "bbdown_path": "",
            "save_path": "",
        }

    def get_page(self) -> List[dict]:
        pass

    def get_state(self):
        return self._enable

    def stop_service(self):
        """
        退出插件
        """
        pass


class CommandExecutor:
    def __init__(self):
        self._input_callback = None
        self._output = []

    def set_input_callback(self, callback):
        """ 设置外部输入回调函数 """
        self._input_callback = callback

    def execute_command(self, command: str):
        """
        执行命令并等待输入
        :param command: 命令
        """
        result = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        output = []

        while True:
            # 读取标准输出
            output_line = result.stdout.readline().decode('utf-8')
            error_line = result.stderr.readline().decode('utf-8')

            # 如果命令结束且没有输出，则退出
            if output_line == '' and error_line == '' and result.poll() is not None:
                break

            if output_line:
                output.append(output_line.strip())
                logger.info(output_line.strip())  # 输出到控制台

            if error_line:
                output.append(error_line.strip())
                logger.info(error_line.strip())  # 输出到控制台

            # 如果有需要输入的提示，调用回调函数来获取输入
            if '请选择' in output_line:  # 假设命令要求输入时，输出中包含‘请输入’字符串
                if self._input_callback:
                    input_data = self._input_callback()  # 调用回调函数获取输入
                    result.stdin.write(input_data.encode('utf-8'))
                    result.stdin.flush()

        return output