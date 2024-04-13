import docker
import time
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class DockerManager(_PluginBase):
    # 插件名称
    plugin_name = "docker管理"
    # 插件描述
    plugin_desc = "管理宿主机docker，自定义容器定时任务。"
    # 插件图标
    plugin_icon = "Docker_F.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "dockermanager_"
    # 加载顺序
    plugin_order = 39
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _clear: bool = False
    _msgtype: str = None
    _time_confs = None
    _docker_client = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._clear = config.get("clear")
            self._time_confs = config.get("time_confs")

            # 清除历史
            if self._clear:
                self.del_data('history')
                self._clear = False
                self.__update_config()

            if (self._enabled or self._onlyonce) and self._time_confs:
                # 创建 Docker 客户端
                self._docker_client = docker.DockerClient(base_url='tcp://127.0.0.1:38379')
                # 周期运行
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 分别执行命令，输入结果
                for time_conf in self._time_confs.split("\n"):
                    if time_conf:
                        if str(time_conf).count("#") == 2:
                            name = str(time_conf).split("#")[0]
                            cron = str(time_conf).split("#")[1]
                            command = str(time_conf).split("#")[2]
                            if self._onlyonce:
                                # 立即运行一次
                                logger.info(f"容器 {name} 立即执行 {command}")
                                self._scheduler.add_job(self.__execute_command, 'date',
                                                        run_date=datetime.now(
                                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                                        name=f"{name} {command}",
                                                        args=[name, command])
                                # 关闭一次性开关
                                self._onlyonce = False

                                # 保存配置
                                self.__update_config()
                            else:
                                try:
                                    self._scheduler.add_job(func=self.__execute_command,
                                                            trigger=CronTrigger.from_crontab(str(cron)),
                                                            name=f"{name} {command}",
                                                            args=[name, command])
                                except Exception as err:
                                    logger.error(f"定时任务配置错误：{err}")
                                    # 推送实时消息
                                    self.systemmessage.put(f"执行周期配置错误：{err}")
                        else:
                            logger.error(f"{time_conf} 配置错误，跳过处理")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __execute_command(self, name, command):
        """
        执行命令
        """
        # 获取所有容器列表
        containers = self._docker_client.containers.list()

        # 遍历容器列表，找到对应名称的容器ID
        for container in containers:
            for env in container.attrs['Config']['Env']:
                if str(env.split("=")[0]) == "HOST_CONTAINERNAME":
                    if str(env.split("=")[1]) == str(name):
                        container_id = container.id
                        # 执行命令
                        log_text = f"容器：{name} ID：{container_id} {command}"
                        if str(command) == "restart":
                            state = self._docker_client.containers.get(container_id).restart()
                        elif str(command) == "start":
                            state = self._docker_client.containers.get(container_id).start()
                        elif str(command) == "stop":
                            state = self._docker_client.containers.get(container_id).stop()
                        elif str(command) == "pause":
                            state = self._docker_client.containers.get(container_id).pause()
                        else:
                            logger.error(f"不支持的命令：{command}")
                            break

                        if state:
                            log_text += " success"
                            logger.info(log_text)
                        else:
                            log_text += " fail"
                            logger.error(log_text)

                        # 读取历史记录
                        history = self.get_data('history') or []

                        history.append({
                            "name": name,
                            "command": command,
                            "result": 'success' if state else 'fail',
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                        })
                        # 保存历史
                        self.save_data(key="history", value=history)

                        if self._notify and self._msgtype:
                            # 发送通知
                            mtype = NotificationType.Manual
                            if self._msgtype:
                                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual

                            container_icon = container.attrs['Config']['Labels']['net.unraid.docker.icon']
                            self.post_message(title="docker管理",
                                              mtype=mtype,
                                              text=log_text,
                                              image=container_icon if container_icon and str(container_icon).startswith(
                                                  "http") else None)
                        break

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "msgtype": self._msgtype,
            "time_confs": self._time_confs,
            "clear": self._clear
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
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
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
                                    'md': 2
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
                                    'md': 2
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
                                            'model': 'clear',
                                            'label': '清除历史记录',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 2
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
                                            'model': 'time_confs',
                                            'label': '执行命令',
                                            'rows': 2,
                                            'placeholder': '容器名#cron表达式#restart/start/stop'
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
                                            'text': '容器名#cron表达式#restart/start/stop'
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
            "notify": False,
            "onlyonce": False,
            "clear": False,
            "time_confs": "",
            "msgtype": ""
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': history.get("name")
                    },
                    {
                        'component': 'td',
                        'text': history.get("command")
                    },
                    {
                        'component': 'td',
                        'text': history.get("result")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '执行时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '容器名称'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '命令'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '执行结果'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

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
