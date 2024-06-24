import random
import re
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
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/code.png"
    # 插件版本
    plugin_version = "1.7"
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
    _clear: bool = False
    _msgtype: str = None
    _time_confs = None
    _history_days = None
    _notify_keywords = None
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
            self._history_days = config.get("history_days") or 30
            self._notify_keywords = config.get("notify_keywords")
            self._time_confs = config.get("time_confs")

            # 清除历史
            if self._clear:
                self.del_data('history')
                self._clear = False
                self.__update_config()

            if (self._enabled or self._onlyonce) and self._time_confs:
                # 周期运行
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 分别执行命令，输入结果
                for time_conf in self._time_confs.split("\n"):
                    if time_conf:
                        if str(time_conf).startswith("#"):
                            logger.info(f"已被注释，跳过 {time_conf}")
                            continue
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

                if self._onlyonce:
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()
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
        while True:
            output = result.stdout.readline().decode("utf-8")
            if output == '' and result.poll() is not None:
                break
            if output:
                logger.info(output.strip())
                last_output = output.strip()

        logger.info(
            f"执行命令：{command} {'成功' if result.returncode == 0 else '失败'} 返回值：{last_output if last_output else last_error}")

        # 读取历史记录
        history = self.get_data('history') or []

        history.append({
            "name": name,
            "command": command,
            "result": last_output if last_output else last_error,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        })

        thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
        history = [record for record in history if
                   datetime.strptime(record["time"],
                                     '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
        # 保存历史
        self.save_data(key="history", value=history)

        if self._notify and self._msgtype:
            if self._notify_keywords and not re.search(self._notify_keywords,
                                                       last_output if last_output else last_error):
                logger.info(f"通知关键词 {self._notify_keywords} 不匹配，跳过通知")
                return

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
            "time_confs": self._time_confs,
            "history_days": self._history_days,
            "notify_keywords": self._notify_keywords,
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清除历史记录',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
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
                                            'model': 'notify_keywords',
                                            'label': '通知关键词',
                                            'placeholder': '支持正则表达式，未配置时所有通知均推送'
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
            "clear": False,
            "time_confs": "",
            "history_days": 30,
            "notify_keywords": "",
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

        # 按照签到时间倒序
        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        # 签到消息
        sign_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': history.get("name")
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
                                                'text': '命令名称'
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
                                        'content': sign_msgs
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
