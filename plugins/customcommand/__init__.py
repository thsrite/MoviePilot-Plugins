import random
import subprocess
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


class CustomCommand(_PluginBase):
    # 插件名称
    plugin_name = "自定义命令"
    # 插件描述
    plugin_desc = "自定义执行周期执行命令并推送结果。"
    # 插件图标
    plugin_icon = "Ntfy_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "customcommand_"
    # 加载顺序
    plugin_order = 39
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _msgtype: bool = False
    _time_confs = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._time_confs = config.get("time_confs")

            if (self._enabled or self._onlyonce) and self._time_confs:
                # 周期运行
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 分别执行命令，输入结果
                for time_conf in self._time_confs.split("\n"):
                    if time_conf:
                        if str(time_conf).count("#") == 2 or str(time_conf).count("#") == 3:
                            name = str(time_conf).split("#")[0]
                            cron = str(time_conf).split("#")[1]
                            command = str(time_conf).split("#")[2]
                            random_delay = None
                            if str(time_conf).count("#") == 3:
                                random_delay = str(time_conf).split("#")[3]

                            if self._onlyonce:
                                # 立即运行一次
                                logger.info(f"{name}服务启动，立即运行一次")
                                self._scheduler.add_job(self.__execute_command, 'date',
                                                        run_date=datetime.now(
                                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                                        name=name,
                                                        args=[name, command])
                                # 关闭一次性开关
                                self._onlyonce = False

                                # 保存配置
                                self.__update_config()
                            else:
                                try:
                                    self._scheduler.add_job(func=self.__execute_command,
                                                            trigger=CronTrigger.from_crontab(str(cron)),
                                                            name=name + (
                                                                f"随机延时{random_delay}秒" if random_delay else ""),
                                                            args=[name, command, random_delay])
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

    def __execute_command(self, name, command, random_delay=None):
        """
        执行命令
        """
        if random_delay:
            random_delay = random.randint(int(str(random_delay).split("-")[0]), int(str(random_delay).split("-")[1]))
            logger.info(f"随机延时 {random_delay} 秒")
            time.sleep(random_delay)

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

            output = result.stdout.readline().decode("utf-8")
            if output == '' and result.poll() is not None:
                break
            if output:
                logger.info(output.strip())
                last_output = output.strip()

        logger.info(
            f"执行命令：{command} {'成功' if result.returncode == 0 else '失败'} 返回值：{last_output if last_output else last_error}")

        if self._notify and self._msgtype:
            # 发送通知
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual

            self.post_message(title=name,
                              mtype=mtype,
                              text=last_output if last_output else last_error)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "msgtype": self._msgtype,
            "time_confs": self._time_confs
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
                                            'placeholder': '命令名#0 9 * * *#python main.py\n'
                                                           '命令名#0 9 * * *#python main.py#1-600'
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
                                            'text': '命令名#cron表达式#命令'
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
                                            'text': '命令名#cron表达式#命令#随机延时（单位秒）'
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
            "time_confs": "",
            "msgtype": ""
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
            logger.error("退出插件失败：%s" % str(e))
