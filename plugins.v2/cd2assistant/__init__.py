import re
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
try:
    from clouddrive import CloudDriveClient, Client
    from clouddrive.proto import CloudDrive_pb2
except ImportError:
    from sys import executable
    from subprocess import run

    run([executable, "-m", "pip", "install", "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/refs/heads/main/data/clouddrive-0.0.12.7.1.tar.gz"], check=True)

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
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
    plugin_version = "2.0.5"
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
    _black_dir = None
    _cloud_path = None
    _cd2_confs = None
    _cd2_clients = {}
    _clients = {}
    _cd2_url = {}

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self._cd2_clients = {}
        self._clients = {}
        self._cd2_url = {}
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._msgtype = config.get("msgtype")
            self._onlyonce = config.get("onlyonce")
            self._cd2_restart = config.get("cd2_restart")
            self._cron = config.get("cron")
            self._keyword = config.get("keyword")
            self._cd2_confs = config.get("cd2_confs")
            self._black_dir = config.get("black_dir") or ""
            self._cloud_path = config.get("cloud_path") or ""

            # 兼容旧版本配置
            self.__sync_old_config()

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce or self._cd2_restart:
            if not self._cd2_confs:
                logger.error("CloudDrive2助手配置错误，请检查配置")
                return

            for cd2_conf in self._cd2_confs.split("\n"):
                _cd2_client = CloudDriveClient(str(cd2_conf).split("#")[1], str(cd2_conf).split("#")[2],
                                               str(cd2_conf).split("#")[3])
                _cd2_name = str(cd2_conf).split("#")[0]
                if not _cd2_client:
                    logger.error(f"CloudDrive2助手连接失败，请检查配置：{_cd2_name}")
                    continue
                _client = Client(str(cd2_conf).split("#")[1], str(cd2_conf).split("#")[2],
                                 str(cd2_conf).split("#")[3])
                if not _client:
                    logger.error("CloudDrive2助手连接失败，请检查配置")
                    continue
                self._cd2_clients[_cd2_name] = _cd2_client
                self._clients[_cd2_name] = _client
                self._cd2_url[_cd2_name] = str(cd2_conf).split("#")[1]

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

    def __sync_old_config(self):
        """
        兼容旧版本配置
        """
        config = self.get_config()
        if not config or not config.get("cd2_url") or not config.get("cd2_username") or not config.get("cd2_password"):
            return

        self._cd2_confs = f"默认配置1#{config.get('cd2_url')}#{config.get('cd2_username')}#{config.get('cd2_password')}"
        self.__update_config()

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cd2_restart": self._cd2_restart,
            "cron": self._cron,
            "msgtype": self._msgtype,
            "keyword": self._keyword,
            "notify": self._notify,
            "cd2_confs": self._cd2_confs,
            "black_dir": self._black_dir,
            "cloud_path": self._cloud_path,
        })

    def check(self):
        """
        检查
        """
        for cd2_name in self._cd2_clients.keys():
            _cd2_client = self._cd2_clients.get(cd2_name)
            self.__check_cookie(cd2_name, _cd2_client)
            self.__check_task(cd2_name, _cd2_client)

    def __check_cookie(self, cd2_name, cd2_client):
        """
        检查cookie是否过期
        """
        logger.info(f"开始检查 {cd2_name} cookie")
        if not cd2_client:
            logger.error("CloudDrive2助手连接失败，请检查配置")
            return
        fs = cd2_client.fs
        if not fs:
            logger.error("CloudDrive2连接失败，请检查配置")
            return

        for f in fs.listdir():
            error_msg = None
            if f and f not in self._black_dir.split(","):
                try:
                    cloud_file = fs.listdir(f)
                    if not cloud_file or len(cloud_file) == 0:
                        logger.warning(f"云盘 {f} 为空")
                        error_msg = f"云盘 {f} cookie过期"
                except Exception as err:
                    logger.error(f"云盘 {f} cookie过期：{err}")
                    if "429" in str(err):
                        error_msg = f"云盘 {f} 访问频率过高，请稍后再试"
                    else:
                        error_msg = f"云盘 {f} cookie过期"

            # 发送通知
            if self._notify and error_msg:
                self.__send_notify(error_msg)

    def __check_task(self, cd2_name, cd2_client):
        """
        检查上传任务
        """
        logger.info(f"开始检查 {cd2_name} 上传任务")
        # 获取上传任务列表
        upload_tasklist = cd2_client.upload_tasklist.list(page=0, page_size=10, filter="")
        if not upload_tasklist:
            logger.info("没有发现上传任务")
            return

        for task in upload_tasklist:
            if task.get("status") == "FatalError" and self._keyword and re.search(self._keyword,
                                                                                  task.get("errorMessage")):
                logger.info(f"发现异常上传任务：{task.get('errorMessage')}")
                # 发送通知
                if self._notify:
                    self.__send_notify(task.get("errorMessage"))
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
            args = event_data.get("arg_str")
            found = False
            for cd2_name, client in self._clients.items():
                if args and str(args).lower() != str(cd2_name):
                    continue
                found = True
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"{cd2_name} CloudDrive2重启成功！", userid=event.event_data.get("user"))
                client.RestartService()

            if args and not found:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"未找到 {args} 配置！", userid=event.event_data.get("user"))
                return
        else:
            for cd2_name in self._clients.keys():
                _client = self._clients.get(cd2_name)
                logger.info(f"{cd2_name} CloudDrive2重启成功")
                _client.RestartService()

    def __get_cloud_space(self, cd2_client):
        """
        获取云盘空间
        """
        fs = cd2_client.fs
        if not fs:
            logger.error("CloudDrive2连接失败，请检查配置")
            return

        _space_info = "\n"
        for f in fs.listdir():
            try:
                if f and f not in self._black_dir.split(","):
                    space_info = cd2_client.GetSpaceInfo(CloudDrive_pb2.FileRequest(path=f))
                    space_info = self.__str_to_dict(space_info)
                    total = self.__convert_bytes(space_info.get("totalSpace"))
                    used = self.__convert_bytes(space_info.get("usedSpace"))
                    free = self.__convert_bytes(space_info.get("freeSpace"))
                    _space_info += f"{f}：{used}/{total}\n"
            except Exception:
                logger.error(f"获取云盘 {f} 空间信息失败")

        return _space_info

    @eventmanager.register(EventType.PluginAction)
    def add_offline_files(self, event: Event = None):
        """
        离线下载
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloud_download":
                return
            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            # 判断有无指定路径
            args = args.replace(" ", "\n")
            _cloud_path = self._cloud_path.strip()
            if args.split("\n")[0].startswith("/"):
                _cloud_path = str(args.split("\n")[0])
                args = args.replace(f"{_cloud_path}\n", "")
            if not _cloud_path:
                logger.error("请先设置云盘路径")
                if event.event_data.get("user"):
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"请先设置云盘路径！",
                                      userid=event.event_data.get("user"))
                return

            logger.info(f"获取到离线云盘路径：{_cloud_path}")
            logger.info(f"开始离线下载：{args}")

            client = None
            for cd2_name, client in self._clients.items():
                if client:
                    break

            result = client.AddOfflineFiles(
                CloudDrive_pb2.AddOfflineFileRequest(urls=args, toFolder=_cloud_path))
            if result and result.success:
                logger.info(f"离线下载成功")
                if event.event_data.get("user"):
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"{_cloud_path} 离线下载成功！",
                                      userid=event.event_data.get("user"))
            else:
                errorMessage = None
                if result and result.errorMessage:
                    errorMessage = result.errorMessage
                logger.error(f"离线下载失败：{errorMessage}")
                if event.event_data.get("user"):
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"离线下载失败！",
                                      userid=event.event_data.get("user"),
                                      text=f"错误信息：{errorMessage}")

    @eventmanager.register(EventType.PluginAction)
    def cd2_info(self, event: Event = None):
        """
        获取CloudDrive2信息
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cd2_info":
                return

            args = event_data.get("arg_str")
            found = False
            for cd2_name, client in self._clients.items():
                if args and str(args).lower() != str(cd2_name):
                    continue
                found = True
                cd2_client = self._cd2_clients[cd2_name]
                self.__get_cd2_info(event=event, client=client, cd2_client=cd2_client)

            if args and not found:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"未找到 {args} 配置！", userid=event.event_data.get("user"))
                return

    def __get_cd2_info(self, event: Event = None, client: Client = None, cd2_client: CloudDriveClient = None):
        """
        获取CloudDrive2信息
        """
        # 运行信息
        system_info = client.GetRunningInfo()
        system_info = self.__str_to_dict(system_info) if system_info else {}

        # 任务数量
        task_count = client.GetAllTasksCount()
        task_count = self.__str_to_dict(task_count) if task_count else {}

        # 速度
        downloadFileList = client.GetDownloadFileList()
        downloadFileList = self.__str_to_dict(downloadFileList) if downloadFileList else {}
        uploadFileList = client.GetUploadFileList(CloudDrive_pb2.GetUploadFileListRequest(getAll=True))
        uploadFileList = self.__str_to_dict(uploadFileList) if uploadFileList else {}

        # 云盘空间
        cloud_space = self.__get_cloud_space(cd2_client)

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
            "upload_count": task_count.get("uploadCount") if task_count.get("uploadCount") else 0,
            "download_count": task_count.get("downloadCount") if task_count.get("downloadCount") else 0,
            "download_speed": f"{downloadFileList.get('globalBytesPerSecond') / 1024 / 1024:.2f}MB/s" if downloadFileList.get(
                "globalBytesPerSecond") else "0KB/s" if downloadFileList else "0KB/s",
            "upload_speed": f"{uploadFileList.get('globalBytesPerSecond') / 1024 / 1024:.2f}MB/s" if uploadFileList.get(
                "globalBytesPerSecond") else "0KB/s" if uploadFileList else "0KB/s",
            "cloud_space": cloud_space
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
                                   f"下载任务数量：{system_info_dict.get('download_count')}\n"
                                   f"下载速度：{system_info_dict.get('download_speed')}\n"
                                   f"上传速度：{system_info_dict.get('upload_speed')}\n"
                                   f"存储空间：{system_info_dict.get('cloud_space')}\n")

        return system_info_dict

    def homepage(self, apikey: str, name: str = None) -> Any:
        """
        homepage自定义api
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")

        client = None
        cd2_client = None
        for cd2_name, client in self._clients.items():
            if name and str(cd2_name) != name:
                continue
            cd2_client = self._cd2_clients[cd2_name]
            if client and cd2_client:
                break

        return self.__get_cd2_info(client=client, cd2_client=cd2_client)

    @staticmethod
    def __convert_bytes(size_in_bytes):
        """ Convert bytes to the most appropriate unit (PB, TB, GB, etc.) """
        units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
        unit_index = 0

        while size_in_bytes >= 1024 and unit_index < len(units) - 1:
            size_in_bytes /= 1024
            unit_index += 1

        return f"{size_in_bytes:.2f} {units[unit_index]}"

    @staticmethod
    def __str_to_dict(str_data):
        """
        字符串转字典
        """
        pattern = re.compile(r'(\w+): ([\d.]+)')
        matches = pattern.findall(str(str_data))
        # 将匹配到的结果转换为字典
        return {key: float(value) for key, value in matches}

    def __send_notify(self, msg):
        """
        发送通知
        """
        mtype = NotificationType.Manual
        if self._msgtype:
            mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
        self.post_message(title="CloudDrive2助手通知",
                          mtype=mtype,
                          text=msg)

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
            },
            {
                "cmd": "/cd",
                "event": EventType.PluginAction,
                "desc": "云下载",
                "category": "",
                "data": {
                    "action": "cloud_download"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/homepage",
            "endpoint": self.homepage,
            "methods": ["GET"],
            "summary": "HomePage",
            "description": "HomePage自定义api",
        }]

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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cd2_confs',
                                            'label': 'cd2配置',
                                            'rows': 2,
                                            'placeholder': 'cd2配置1#http://127.0.0.1:19798#admin#123456（一行一个配置）'
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
                                        'component': 'VCronField',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'black_dir',
                                            'label': 'cd2黑名单目录',
                                            'placeholder': 'cd2上添加的本地目录(多个目录用英文逗号分隔)'
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
                                            'model': 'cloud_path',
                                            'label': '云下载路径'
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
                                            'text': '周期检测CloudDrive2上传任务，检测是否命中检测关键词，发送通知。'
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
                                            'text': '周期检测CloudDrive2云盘CK是否过期，发送通知（挂载的本地路径可添加黑名单）。'
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': 'HomePage配置教程请参考：'
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md',
                                                    'target': '_blank'
                                                },
                                                'text': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md'
                                            }
                                        ]
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
                                            'text': '如安装完启用插件后，HomePage提示404，重启MoviePilot即可。'
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
            "cd2_confs": "",
            "msgtype": "Manual",
            "black_dir": "",
            "cloud_path": "",
        }

    def get_page(self) -> List[dict]:
        page_form = []
        for cd2_name, client in self._clients.items():
            cd2_client = self._cd2_clients[cd2_name]
            cd2_url = self._cd2_url[cd2_name]
            cd2_info = self.__get_cd2_info(client=client, cd2_client=cd2_client)
            page_form.append({
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4,
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
                                                            'class': 'text-h6'
                                                        },
                                                        'text': cd2_name
                                                    },
                                                    {
                                                        'component': 'div',
                                                        'props': {
                                                            'class': 'd-flex align-center flex-wrap'
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'a',
                                                                'props': {
                                                                    'class': 'text-caption',
                                                                    'href': cd2_url,
                                                                    'target': '_blank',
                                                                },
                                                                'text': cd2_url
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                            'md': 4,
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
                    },
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 4,
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
                                                        'text': '实时速率'
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
                                                                'text': f"↑ {cd2_info.get('download_speed')}  ↓ {cd2_info.get('upload_speed')}"
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
                            'md': 4,
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
                                                        'text': '存储空间'
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
                                                                'text': cd2_info.get('cloud_space')
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
            }, )
        return page_form

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
            "md": 12
        }
        # 全局配置
        attrs = {
            "refresh": 10, "border": False
        }
        if not self._clients:
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
            elements = []
            for cd2_name, client in self._clients.items():
                cd2_client = self._cd2_clients[cd2_name]
                cd2_url = self._cd2_url[cd2_name]
                cd2_info = self.__get_cd2_info(client=client, cd2_client=cd2_client)

                elements.append(
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
                                                                    'class': 'text-h6'
                                                                },
                                                                'text': cd2_name
                                                            },
                                                            {
                                                                'component': 'div',
                                                                'props': {
                                                                    'class': 'd-flex align-center flex-wrap'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'a',
                                                                        'props': {
                                                                            'class': 'text-caption',
                                                                            'href': cd2_url,
                                                                            'target': '_blank',
                                                                        },
                                                                        'text': cd2_url
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
                                                                    'class': 'text-h6'
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
                                                                'text': '存储空间'
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
                                                                        'text': cd2_info.get('cloud_space')
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
                                                                'text': '下载速率'
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
                                                                        'text': cd2_info.get('download_speed')
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
                                                                'text': '上传速率'
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
                                                                        'text': cd2_info.get('upload_speed')
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

                        ]
                    })

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
