import re
from datetime import datetime, timedelta

import pytz
from clouddrive import CloudDriveClient, Client

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas import NotificationType
from app.schemas.types import EventType

class Cd2Assistant(_PluginBase):
    # 插件名称
    plugin_name = "CloudDrive2助手"
    # 插件描述
    plugin_desc = "监控上传任务，检测是否有异常，发送通知。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    # 插件版本
    plugin_version = "1.1"
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
    _cd2_restart: bool = False
    _cron = None
    _notify = False
    _msgtype = None
    _keyword = None
    _cd2_url = None
    _cd2_username = None
    _cd2_password = None
    _cd2_client = None
    _client = None

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._onlyonce = config.get("onlyonce")
            self._cd2_restart = config.get("cd2_restart")
            self._cron = config.get("cron")
            self._keyword = config.get("keyword")
            self._cd2_url = config.get("cd2_url")
            self._cd2_username = config.get("cd2_username")
            self._cd2_password = config.get("cd2_password")

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce or self._cd2_restart:
            if not self._cd2_url or not self._cd2_username or not self._cd2_password:
                logger.error("CloudDrive2助手配置错误，请检查配置")
                return

            self._cd2_client = CloudDriveClient(self._cd2_url, self._cd2_username, self._cd2_password)
            if not self._cd2_client:
                logger.error("CloudDrive2助手连接失败，请检查配置")
                return

            self._client = Client(self._cd2_url, self._cd2_username, self._cd2_password)
            if not self._client:
                logger.error("CloudDrive2助手连接失败，请检查配置")
                return

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

            # 立即运行一次
            if self._cd2_restart:
                logger.info(f"CloudDrive2重启任务，立即运行一次")
                self._scheduler.add_job(self.restart_cd2(), 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="CloudDrive2重启任务")
                # 关闭一次性开关
                self._cd2_restart = False

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
            "cd2_restart": self._cd2_restart,
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
        logger.info("开始检查CloudDrive2上传任务")
        # 获取上传任务列表
        upload_tasklist = self._cd2_client.upload_tasklist.list(page=0, page_size=10, filter="")
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

    @eventmanager.register(EventType.PluginAction)
    def restart_cd2(self, event: Event = None):
        """
        重启CloudDrive2
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cd2_restart":
                return

        logger.info("CloudDrive2重启成功")
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="CloudDrive2重启成功！", userid=event.event_data.get("user"))

        self._client.RestartService()


    @eventmanager.register(EventType.PluginAction)
    def cd2_info(self, event: Event = None):
        """
        获取CloudDrive2信息
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cd2_info":
                return

        # 运行信息
        system_info = self._client.GetRunningInfo()
        if system_info:
            pattern = re.compile(r'(\w+): ([\d.]+)')
            matches = pattern.findall(str(system_info))
            # 将匹配到的结果转换为字典
            system_info = {key: float(value) for key, value in matches}

        # 上传任务数量
        upload_count = self._client.GetUploadFileCount()
        # 下载任务数量
        download_count = self._client.GetDownloadFileCount()

        system_info_dict = {
            "cpuUsage": f"{system_info.get('cpuUsage'):.2f}%" if system_info.get(
                "cpuUsage") else "0.00%" if system_info else None,
            "memUsageKB": f"{system_info.get('memUsageKB') / 1024:.2f}MB" if system_info.get(
                "memUsageKB") else "0MB" if system_info else None,
            "uptime": self.convert_seconds(system_info.get('uptime')) if system_info.get(
                "uptime") else "0秒" if system_info else None,
            "fhTableCount": system_info.get('fhTableCount') if system_info.get(
                "fhTableCount") else 0 if system_info else None,
            "dirCacheCount": int(system_info.get('dirCacheCount')) if system_info.get(
                "dirCacheCount") else 0 if system_info else None,
            "tempFileCount": system_info.get('tempFileCount') if system_info.get(
                "tempFileCount") else 0 if system_info else None,
            "upload_count": str(upload_count).replace("fileCount: ", "") or 0 if upload_count and "fileCount" in str(
                upload_count) else 0,
            "download_count": str(download_count).replace("fileCount: ",
                                                          "") or 0 if download_count and "fileCount" in str(
                download_count) else 0,
        }

        logger.info(f"获取CloudDrive2系统信息：\n{system_info_dict}")

        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="CloudDrive2系统信息",
                              userid=event.event_data.get("user"),
                              text=f"CPU占用：{system_info_dict.get('cpuUsage')}\n"
                                   f"内存占用：{system_info_dict.get('memUsageKB')}\n"
                                   f"运行时间：{system_info_dict.get('uptime')}\n"
                                   f"打开文件数量：{system_info_dict.get('fhTableCount')}\n"
                                   f"目录缓存数量：{system_info_dict.get('dirCacheCount')}\n"
                                   f"临时文件数量：{system_info_dict.get('tempFileCount')}\n"
                                   f"上传任务数量：{system_info_dict.get('upload_count')}\n"
                                   f"下载任务数量：{system_info_dict.get('download_count')}\n")

        return system_info_dict

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

    @staticmethod
    def convert_seconds(seconds):
        days, seconds = divmod(seconds, 86400)  # 86400秒 = 1天
        hours, seconds = divmod(seconds, 3600)  # 3600秒 = 1小时
        minutes, seconds = divmod(seconds, 60)  # 60秒 = 1分钟
        parts = []
        if days > 0:
            parts.append(f"{int(days)}天")
        if hours > 0:
            parts.append(f"{int(hours)}小时")
        if minutes > 0:
            parts.append(f"{int(minutes)}分钟")
        if seconds > 0 or not parts:  # 添加秒数或只有秒数时
            parts.append(f"{seconds:.0f}秒")

        return ''.join(parts)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/cd2_restart",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2重启",
                "category": "",
                "data": {
                    "action": "cd2_restart"
                }
            },
            {
                "cmd": "/cd2_info",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2系统信息",
                "category": "",
                "data": {
                    "action": "cd2_info"
                }
            }
        ]

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
                                            'label': '开启通知',
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
                                            'model': 'cd2_restart',
                                            'label': 'cd2重启一次',
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
            "cd2_restart": False,
            "cron": "*/10 * * * *",
            "keyword": "账号异常",
            "cd2_url": "",
            "cd2_username": "",
            "cd2_password": "",
            "msgtype": "Manual"
        }

    def get_page(self) -> List[dict]:
        cd2_info = self.cd2_info()
        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': 'CPU占用'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('cpuUsage')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '内存占用'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('memUsageKB')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '运行时间'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('uptime')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '打开文件数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('fhTableCount')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '缓存目录数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('dirCacheCount')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '临时文件数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('tempFileCount')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '下载任务数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('download_count')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 3,
                            'sm': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'tonal',
                                },
                                'content': [
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'd-flex align-center',
                                        },
                                        'content': [
                                            {
                                                'component': 'div',
                                                'content': [
                                                    {
                                                        'component': 'span',
                                                        'props': {
                                                            'class': 'text-caption'
                                                        },
                                                        'text': '上传任务数'
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'span',
                                                                'props': {
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_info.get('upload_count')
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }]

    def get_dashboard(self) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """
        获取插件仪表盘页面，需要返回：1、仪表板col配置字典；2、全局配置（自动刷新等）；3、仪表板页面元素配置json（含数据）
        1、col配置参考：
        {
            "cols": 12, "md": 6
        }
        2、全局配置参考：
        {
            "refresh": 10 // 自动刷新时间，单位秒
        }
        3、页面配置使用Vuetify组件拼装，参考：https://vuetifyjs.com/
        """
        # 列配置
        cols = {
            "cols": 12,
            "md": 8
        }
        # 全局配置
        attrs = {
            "refresh": 10
        }
        if not self._client:
            logger.warn(f"请求CloudDrive2服务失败")
            elements = [
                {
                    'component': 'div',
                    'text': '无法连接CloudDrive2',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]
        else:
            """
            Active connections: 62 
            server accepts handled requests
             468843 468843 1368256 
            Reading: 0 Writing: 1 Waiting: 61 
            """
            cd2_info = self.cd2_info()
            elements = [
                {
                    'component': 'VRow',
                    'content': [
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': 'CPU占用'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('cpuUsage')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '内存占用'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('memUsageKB')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '运行时间'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('uptime')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '打开文件数'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('fhTableCount')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '缓存目录数'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('dirCacheCount')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '临时文件数'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('tempFileCount')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '下载任务数'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('download_count')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            'component': 'VCol',
                            'props': {
                                'cols': 6,
                                'md': 3
                            },
                            'content': [
                                {
                                    'component': 'VCard',
                                    'props': {
                                        'variant': 'tonal',
                                    },
                                    'content': [
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'd-flex align-center',
                                            },
                                            'content': [
                                                {
                                                    'component': 'div',
                                                    'content': [
                                                        {
                                                            'component': 'span',
                                                            'props': {
                                                                'class': 'text-caption'
                                                            },
                                                            'text': '上传任务数'
                                                        },
                                                        {
                                                            'component': 'div',
                                                            'props': {
                                                                'class': 'd-flex align-center flex-wrap'
                                                            },
                                                            'content': [
                                                                {
                                                                    'component': 'span',
                                                                    'props': {
                                                                        'class': 'text-h6'
                                                                    },
                                                                    'text': cd2_info.get('upload_count')
                                                                }
                                                            ]
                                                        }
                                                    ]
                                                }
                                            ]
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                }]

        return cols, attrs, elements

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
