import re
from datetime import datetime, timedelta

import pytz
from clouddrive import CloudDriveClient, Client

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas import NotificationType, MediaType


class Cd2Assistant(_PluginBase):
    # 插件名称
    plugin_name = "CloudDrive2助手"
    # 插件描述
    plugin_desc = "监控上传任务，检测是否有异常，发送通知。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cd2assistant_"
    # 加载顺序
    plugin_order = 5
    # 可使用的用户级别
    auth_level = 2

    # 任务执行间隔
    _enabled = False
    _onlyonce: bool = False
    _cron = None
    _notify = False
    _msgtype = None
    _keyword = None
    _cd2_url = None
    _cd2_username = None
    _cd2_password = None

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._keyword = config.get("keyword")
            self._cd2_url = config.get("cd2_url")
            self._cd2_username = config.get("cd2_username")
            self._cd2_password = config.get("cd2_password")

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 周期运行
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._cron:
                try:
                    self._scheduler.add_job(func=self.check,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="CloudDrive2助手定时任务")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 立即运行一次
            if self._onlyonce:
                logger.info(f"CloudDrive2助手定时任务，立即运行一次")
                self._scheduler.add_job(self.check, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="CloudDrive2助手定时任务")
                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "msgtype": self._msgtype,
            "keyword": self._keyword,
            "notify": self._notify,
            "cd2_url": self._cd2_url,
            "cd2_username": self._cd2_username,
            "cd2_password": self._cd2_password,
        })

    def check(self):
        """
        检查上传任务
        """
        if not self._cd2_url or not self._cd2_username or not self._cd2_password:
            logger.error("CloudDrive2助手配置错误，请检查配置")
            return

        _client = CloudDriveClient(self._cd2_url, self._cd2_username, self._cd2_password)
        if not _client:
            logger.error("CloudDrive2助手连接失败，请检查配置")
            return

        logger.info("开始检查CloudDrive2上传任务")
        # 获取上传任务列表
        upload_tasklist = _client.upload_tasklist.list(page=0, page_size=10, filter="")
        if not upload_tasklist:
            logger.info("没有发现上传任务")
            return

        for task in upload_tasklist:
            if task.get("status") == "FatalError" and self._keyword and re.search(self._keyword,
                                                                                  task.get("errorMessage")):
                logger.info(f"发现异常上传任务：{task.get('errorMessage')}")
                # 发送通知
                if self._notify:
                    self.__send_notify(task)
                    break

    def restart_cd2(self):
        """
        重启CloudDrive2
        """
        client = Client(self._cd2_url, self._cd2_username, self._cd2_password)
        if not client:
            logger.error("CloudDrive2助手连接失败，请检查配置")
            return

        logger.info("开始重启CloudDrive2")
        client.RestartService(async_=True)
        logger.info("CloudDrive2重启成功")

    def __send_notify(self, task):
        """
        发送通知
        """
        mtype = NotificationType.Manual
        if self._msgtype:
            mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
        self.post_message(title="CloudDrive2助手通知",
                          mtype=mtype,
                          text=task.get("errorMessage"))

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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                            'model': 'cd2_url',
                                            'label': 'cd2地址',
                                            'placeholder': 'http://127.0.0.1:19798'
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
                                            'model': 'cd2_username',
                                            'label': 'cd2用户名'
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
                                            'model': 'cd2_password',
                                            'label': 'cd2密码'
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
                                            'model': 'cron',
                                            'label': '检测周期',
                                            'placeholder': '5位cron表达式'
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
                                            'model': 'keyword',
                                            'label': '检测关键字'
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
                                            'text': '周期检测CloudDrive2上传任务，检测是否命中检测关键词，发送通知。'
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
            "cron": "*/10 * * * *",
            "keyword": "账号异常",
            "cd2_url": "",
            "cd2_username": "",
            "cd2_password": "",
            "msgtype": "Manual"
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
