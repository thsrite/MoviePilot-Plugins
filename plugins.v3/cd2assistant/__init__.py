from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from clouddrive import Client, CloudDriveClient
from clouddrive.proto import CloudDrive_pb2
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel, ConfigDict

from app import schemas
from app.plugins import _PluginBase
from app.schemas.event import PluginActionEventData
from app.schemas.types import EventType, MessageType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger


class CloudDriveInfo(BaseModel):
    """CloudDrive2 状态页和 HomePage API 的稳定返回模型。"""

    model_config = ConfigDict(extra="ignore")

    cpuUsage: Optional[str] = None
    memUsageKB: Optional[str] = None
    uptime: Optional[str] = None
    fhTableCount: Optional[int] = None
    dirCacheCount: Optional[int] = None
    tempFileCount: Optional[int] = None
    upload_count: int = 0
    download_count: int = 0
    download_speed: str = "0KB/s"
    upload_speed: str = "0KB/s"
    cloud_space: Optional[str] = None


class Cd2Assistant(_PluginBase):
    """监控 CloudDrive2 上传任务和云盘登录状态。"""

    plugin_name = "CloudDrive2助手"
    plugin_desc = "监控上传任务，检测是否有异常，发送通知。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/clouddrive.png"
    plugin_version = "3.0.1"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cd2assistant_"
    plugin_order = 5
    auth_level = 2

    def __init__(self) -> None:
        """初始化实例状态，避免不同插件实例共享 CloudDrive 客户端。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cd2_restart = False
        self._cron: Optional[str] = None
        self._notify = False
        self._msgtype: Optional[str] = None
        self._keyword: Optional[str] = None
        self._black_dir = ""
        self._cloud_path = ""
        self._cd2_confs: Optional[str] = None
        self._cd2_clients: Dict[str, CloudDriveClient] = {}
        self._clients: Dict[str, Client] = {}
        self._cd2_url: Dict[str, str] = {}
        self._info: Dict[str, Dict[str, Any]] = {}
        self._scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并建立客户端；周期任务由宿主调度器统一管理。"""
        self.stop_service()
        if self._scheduler is not None or self._cd2_clients or self._clients:
            logger.error("旧 CloudDrive2 资源未完全停止，跳过重新初始化")
            return
        self._info = {}

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._msgtype = config.get("msgtype")
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cd2_restart = bool(config.get("cd2_restart", False))
        self._cron = config.get("cron")
        self._keyword = config.get("keyword")
        self._cd2_confs = config.get("cd2_confs")
        self._black_dir = config.get("black_dir") or ""
        self._cloud_path = config.get("cloud_path") or ""
        self.__sync_old_config()

        if not (self._enabled or self._onlyonce or self._cd2_restart):
            return
        if not self._cd2_confs:
            logger.error("CloudDrive2助手配置错误，请检查配置")
            return

        self.__create_clients()
        if not self._cd2_clients:
            return

        if self._onlyonce or self._cd2_restart:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            logger.info("CloudDrive2助手定时任务，立即运行一次")
            self._scheduler.add_job(
                self.__run_check,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2助手定时任务",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cd2_restart:
            logger.info("CloudDrive2重启任务，立即运行一次")
            self._scheduler.add_job(
                self.__run_restart,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="CloudDrive2重启任务",
            )
            self._cd2_restart = False
            self.__update_config()

        if self._scheduler and self._scheduler.get_jobs():
            self._scheduler.start()

    def __create_clients(self) -> None:
        """按一行一个配置建立 CloudDrive2 客户端，隔离单条坏配置。"""
        for line in (self._cd2_confs or "").splitlines():
            parts = line.split("#", 3)
            if len(parts) != 4 or not all(parts[:3]):
                logger.error("CloudDrive2助手配置格式错误，已跳过：%s", line.split("#", 1)[0])
                continue

            name, url, username, password = parts
            try:
                cd2_client = CloudDriveClient(url, username, password)
                client = Client(url, username, password)
            except Exception as error:
                logger.error("CloudDrive2助手连接失败，请检查配置 %s：%s", name, error)
                continue

            old_cd2_client = self._cd2_clients.get(name)
            old_client = self._clients.get(name)
            if old_cd2_client:
                old_cd2_client.close()
            if old_client:
                old_client.close()
            self._cd2_clients[name] = cd2_client
            self._clients[name] = client
            self._cd2_url[name] = url

    def __sync_old_config(self) -> None:
        """把历史单实例配置转换为当前多实例配置。"""
        if self._cd2_confs:
            return
        old_config = self.get_config() or {}
        if not all(old_config.get(key) for key in ("cd2_url", "cd2_username", "cd2_password")):
            return
        self._cd2_confs = (
            f"默认配置1#{old_config['cd2_url']}#"
            f"{old_config['cd2_username']}#{old_config['cd2_password']}"
        )
        self.__update_config()

    def __update_config(self) -> None:
        """持久化会被一次性任务消费的配置状态。"""
        self.update_config(
            {
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
            }
        )

    @staticmethod
    async def __await_rpc(method, *args, **kwargs):
        """等待 CloudDrive2 的异步 RPC，同时兼容确定性的测试替身。"""
        result = method(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    async def __listdir(fs: Any, path: str = "") -> list[str]:
        """把 CloudDrive2 同步文件系统查询移出宿主事件循环。"""
        if not fs:
            return []
        result = await asyncio.to_thread(fs.listdir, path)
        return list(result or [])

    async def check(self) -> None:
        """检查所有配置实例的云盘状态和上传任务。"""
        for name, cd2_client in tuple(self._cd2_clients.items()):
            try:
                await self.__check_cookie(name, cd2_client)
                await self.__check_task(name, cd2_client)
            except Exception as error:
                logger.error("检查 CloudDrive2 实例 %s 失败：%s", name, error)

    async def __check_cookie(self, name: str, cd2_client: CloudDriveClient) -> None:
        """检查云盘根目录是否可读，以识别登录失效和访问频控。"""
        logger.info("开始检查 %s cookie", name)
        fs = cd2_client.fs
        if not fs:
            logger.error("CloudDrive2连接失败，请检查配置")
            return

        blacklist = {item.strip() for item in self._black_dir.split(",") if item.strip()}
        for path in await self.__listdir(fs):
            if not path or path in blacklist:
                continue
            error_msg = None
            try:
                if not await self.__listdir(fs, path):
                    logger.warning("云盘 %s 为空", path)
                    error_msg = f"云盘 {path} cookie过期"
            except Exception as error:
                logger.error("云盘 %s cookie过期：%s", path, error)
                error_msg = (
                    f"云盘 {path} 访问频率过高，请稍后再试"
                    if "429" in str(error)
                    else f"云盘 {path} cookie过期"
                )
            if self._notify and error_msg:
                self.__send_notify(error_msg)

    async def __check_task(self, name: str, cd2_client: CloudDriveClient) -> None:
        """检查上传任务并按关键词通知异常任务。"""
        logger.info("开始检查 %s 上传任务", name)
        task_list = await self.__await_rpc(
            cd2_client.upload_tasklist.list,
            page=0,
            page_size=10,
            filter="",
            async_=True,
        )
        if not task_list:
            logger.info("没有发现上传任务")
            return

        for task in task_list:
            status = self.__field(task, "status")
            error_message = self.__field(task, "errorMessage", "") or ""
            try:
                matched = bool(self._keyword and re.search(self._keyword, str(error_message)))
            except re.error as error:
                logger.error("CloudDrive2检测关键字配置错误：%s", error)
                return
            if status == "FatalError" and matched:
                logger.info("发现异常上传任务：%s", error_message)
                if self._notify:
                    self.__send_notify(str(error_message))
                    break

    @staticmethod
    def __field(value: Any, name: str, default: Any = None) -> Any:
        """读取 CloudDrive2 protobuf 或字典响应中的字段。"""
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def __action_payload(event: Event | None) -> Optional[PluginActionEventData]:
        """读取插件动作的 typed 快照，不直接依赖可变原始 payload。"""
        if event is None:
            return None
        snapshot = event.snapshot()
        if not snapshot.valid or not isinstance(snapshot.payload, PluginActionEventData):
            logger.warning("CloudDrive2助手忽略无效插件动作事件：%s", snapshot.errors)
            return None
        return snapshot.payload

    @eventmanager.register(EventType.PluginAction)
    async def restart_cd2(self, event: Event | None = None) -> None:
        """重启指定或全部 CloudDrive2 实例。"""
        payload = self.__action_payload(event)
        if event is not None:
            if payload is None or payload.action != "cd2_restart":
                return
            args = str(getattr(payload, "arg_str", "") or "").strip()
        else:
            args = ""

        found = False
        for name, client in tuple(self._clients.items()):
            if args and args.casefold() != str(name).casefold():
                continue
            found = True
            try:
                await self.__await_rpc(client.RestartService, async_=True)
            except Exception as error:
                logger.error("%s CloudDrive2重启失败：%s", name, error)
                if payload is not None:
                    self.post_message(
                        channel=payload.channel,
                        title=f"{name} CloudDrive2重启失败！",
                        userid=payload.user,
                        text=str(error),
                    )
                continue
            logger.info("%s CloudDrive2重启成功", name)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title=f"{name} CloudDrive2重启成功！",
                    userid=payload.user,
                )

        if payload is not None and args and not found:
            self.post_message(
                channel=payload.channel,
                title=f"未找到 {args} 配置！",
                userid=payload.user,
            )

    @eventmanager.register(EventType.PluginAction)
    async def add_offline_files(self, event: Event | None = None) -> None:
        """执行 CloudDrive2 离线下载命令。"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cloud_download":
            return
        raw_args = getattr(payload, "arg_str", "")
        if not raw_args:
            logger.error("缺少参数：%s", raw_args)
            return

        args = str(raw_args).replace(" ", "\n")
        cloud_path = self._cloud_path.strip()
        first_line = args.split("\n", 1)[0]
        if first_line.startswith("/"):
            cloud_path = first_line
            args = args[len(first_line):].lstrip("\n")
        if not cloud_path:
            logger.error("请先设置云盘路径")
            if payload.user:
                self.post_message(channel=payload.channel, title="请先设置云盘路径！", userid=payload.user)
            return

        client = next((item for item in self._clients.values() if item), None)
        if not client:
            logger.error("CloudDrive2助手没有可用连接")
            if payload.user:
                self.post_message(channel=payload.channel, title="CloudDrive2连接失败！", userid=payload.user)
            return

        logger.info("获取到离线云盘路径：%s", cloud_path)
        logger.info("开始离线下载：%s", args)
        try:
            result = await self.__await_rpc(
                client.AddOfflineFiles,
                CloudDrive_pb2.AddOfflineFileRequest(urls=args, toFolder=cloud_path),
                async_=True,
            )
        except Exception as error:
            logger.error("离线下载失败：%s", error)
            if payload.user:
                self.post_message(
                    channel=payload.channel,
                    title="离线下载失败！",
                    userid=payload.user,
                    text=f"错误信息：{error}",
                )
            return

        if self.__field(result, "success", False):
            logger.info("离线下载成功")
            if payload.user:
                self.post_message(channel=payload.channel, title=f"{cloud_path} 离线下载成功！", userid=payload.user)
            return

        error_message = self.__field(result, "errorMessage")
        logger.error("离线下载失败：%s", error_message)
        if payload.user:
            self.post_message(
                channel=payload.channel,
                title="离线下载失败！",
                userid=payload.user,
                text=f"错误信息：{error_message}",
            )

    @eventmanager.register(EventType.PluginAction)
    async def cd2_info(self, event: Event | None = None) -> None:
        """通过插件动作消息返回 CloudDrive2 运行状态。"""
        payload = self.__action_payload(event)
        if event is None or payload is None or payload.action != "cd2_info":
            return
        args = str(getattr(payload, "arg_str", "") or "").strip()
        found = False
        for name, client in tuple(self._clients.items()):
            if args and args.casefold() != str(name).casefold():
                continue
            found = True
            cd2_client = self._cd2_clients.get(name)
            try:
                info = await self.__get_cd2_info(event=event, client=client, cd2_client=cd2_client)
                self._info[name] = info
            except Exception as error:
                logger.error("获取 %s CloudDrive2 信息失败：%s", name, error)

        if args and not found:
            self.post_message(channel=payload.channel, title=f"未找到 {args} 配置！", userid=payload.user)

    @staticmethod
    def __message_to_dict(value: Any) -> dict[str, Any]:
        """把 protobuf、字典或文本响应归一为可计算字典。"""
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return MessageToDict(value)
        except (TypeError, ValueError):
            matches = re.findall(r"(\w+): ([\d.]+)", str(value))
            return {key: float(number) for key, number in matches}

    @staticmethod
    def __number(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def __get_cloud_space(self, cd2_client: CloudDriveClient | None) -> Optional[str]:
        """异步读取各云盘空间，且将目录枚举放进线程边界。"""
        if not cd2_client or not cd2_client.fs:
            logger.error("CloudDrive2连接失败，请检查配置")
            return None

        blacklist = {item.strip() for item in self._black_dir.split(",") if item.strip()}
        space_info_text = "\n"
        for path in await self.__listdir(cd2_client.fs):
            if not path or path in blacklist:
                continue
            try:
                result = await self.__await_rpc(
                    cd2_client.GetSpaceInfo,
                    CloudDrive_pb2.FileRequest(path=path),
                    async_=True,
                )
                values = self.__message_to_dict(result)
                total = self.__convert_bytes(self.__number(values.get("totalSpace")))
                used = self.__convert_bytes(self.__number(values.get("usedSpace")))
                space_info_text += f"{path}：{used}/{total}\n"
            except Exception as error:
                logger.error("获取云盘 %s 空间信息失败：%s", path, error)
        return space_info_text

    async def __get_cd2_info(
        self,
        event: Event | None = None,
        client: Client | None = None,
        cd2_client: CloudDriveClient | None = None,
    ) -> dict[str, Any]:
        """读取 CloudDrive2 系统状态并保持旧接口字段名。"""
        if not client or not cd2_client:
            return {}
        system_info = self.__message_to_dict(
            await self.__await_rpc(client.GetRunningInfo, async_=True)
        )
        task_count = self.__message_to_dict(
            await self.__await_rpc(client.GetAllTasksCount, async_=True)
        )
        download_files = self.__message_to_dict(
            await self.__await_rpc(client.GetDownloadFileList, async_=True)
        )
        upload_files = self.__message_to_dict(
            await self.__await_rpc(
                client.GetUploadFileList,
                CloudDrive_pb2.GetUploadFileListRequest(getAll=True),
                async_=True,
            )
        )
        cloud_space = await self.__get_cloud_space(cd2_client)

        cpu_usage = self.__number(system_info.get("cpuUsage"))
        memory_kb = self.__number(system_info.get("memUsageKB"))
        uptime = self.__number(system_info.get("uptime"))
        download_speed = self.__number(download_files.get("globalBytesPerSecond"))
        upload_speed = self.__number(upload_files.get("globalBytesPerSecond"))
        info = {
            "cpuUsage": f"{cpu_usage:.2f}%" if system_info else None,
            "memUsageKB": f"{memory_kb / 1024:.2f}MB" if system_info else None,
            "uptime": self.convert_seconds(uptime) if system_info else None,
            "fhTableCount": int(self.__number(system_info.get("fhTableCount"))) if system_info else None,
            "dirCacheCount": int(self.__number(system_info.get("dirCacheCount"))) if system_info else None,
            "tempFileCount": int(self.__number(system_info.get("tempFileCount"))) if system_info else None,
            "upload_count": int(self.__number(task_count.get("uploadCount"))),
            "download_count": int(self.__number(task_count.get("downloadCount"))),
            "download_speed": f"{download_speed / 1024 / 1024:.2f}MB/s" if download_speed else "0KB/s",
            "upload_speed": f"{upload_speed / 1024 / 1024:.2f}MB/s" if upload_speed else "0KB/s",
            "cloud_space": cloud_space,
        }
        logger.info("获取CloudDrive2系统信息：\n%s", info)

        if event is not None:
            payload = self.__action_payload(event)
            if payload is not None:
                self.post_message(
                    channel=payload.channel,
                    title="CloudDrive2系统信息",
                    userid=payload.user,
                    text=(
                        f"CPU占用：{info['cpuUsage']}\n"
                        f"内存占用：{info['memUsageKB']}\n"
                        f"运行时间：{info['uptime']}\n"
                        f"打开文件数量：{info['fhTableCount']}\n"
                        f"目录缓存数量：{info['dirCacheCount']}\n"
                        f"临时文件数量：{info['tempFileCount']}\n"
                        f"上传任务数量：{info['upload_count']}\n"
                        f"下载任务数量：{info['download_count']}\n"
                        f"下载速度：{info['download_speed']}\n"
                        f"上传速度：{info['upload_speed']}\n"
                        f"存储空间：{info['cloud_space']}\n"
                    ),
                )
        return info

    async def homepage(self, name: str | None = None) -> CloudDriveInfo:
        """返回指定 CloudDrive2 实例的 HomePage 状态。"""
        for cd2_name, client in self._clients.items():
            if name and str(cd2_name) != name:
                continue
            info = await self.__get_cd2_info(client=client, cd2_client=self._cd2_clients.get(cd2_name))
            self._info[cd2_name] = info
            return CloudDriveInfo.model_validate(info)
        return CloudDriveInfo()

    async def __refresh_info(self) -> None:
        """刷新详情页和仪表板共用的 CloudDrive2 状态快照。"""
        for name, client in tuple(self._clients.items()):
            try:
                self._info[name] = await self.__get_cd2_info(
                    client=client,
                    cd2_client=self._cd2_clients.get(name),
                )
            except Exception as error:
                logger.error("获取 %s CloudDrive2 信息失败：%s", name, error)

    def __refresh_info_sync(self) -> None:
        """从宿主同步页面 ABI 运行异步状态采集。"""
        try:
            asyncio.run(self.__refresh_info())
        except Exception as error:
            logger.error("刷新 CloudDrive2 页面状态失败：%s", error)

    @staticmethod
    def __convert_bytes(size_in_bytes: float) -> str:
        """把字节数转换为适合状态页展示的单位。"""
        units = ("B", "KB", "MB", "GB", "TB", "PB")
        unit_index = 0
        while size_in_bytes >= 1024 and unit_index < len(units) - 1:
            size_in_bytes /= 1024
            unit_index += 1
        return f"{size_in_bytes:.2f} {units[unit_index]}"

    @staticmethod
    def convert_seconds(seconds: float) -> str:
        """把运行秒数转换为中文时长。"""
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        parts = []
        if days > 0:
            parts.append(f"{int(days)}天")
        if hours > 0:
            parts.append(f"{int(hours)}小时")
        if minutes > 0:
            parts.append(f"{int(minutes)}分钟")
        if seconds > 0 or not parts:
            parts.append(f"{seconds:.0f}秒")
        return "".join(parts)

    def __send_notify(self, message: str) -> None:
        """发送检测通知，并将无效消息类型回退到手动处理。"""
        try:
            message_type = MessageType[str(self._msgtype)] if self._msgtype else MessageType.Manual
        except KeyError:
            message_type = MessageType.Manual
        self.post_message(title="CloudDrive2助手通知", mtype=message_type, text=message)

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册 CloudDrive2 重启、状态和离线下载命令。"""
        return [
            {
                "cmd": "/cd2_restart",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2重启",
                "category": "",
                "data": {"action": "cd2_restart"},
            },
            {
                "cmd": "/cd2_info",
                "event": EventType.PluginAction,
                "desc": "CloudDrive2系统信息",
                "category": "",
                "data": {"action": "cd2_info"},
            },
            {
                "cmd": "/cd",
                "event": EventType.PluginAction,
                "desc": "云下载",
                "category": "",
                "data": {"action": "cloud_download"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册由宿主 API Key 鉴权的 HomePage 状态接口。"""
        return [
            {
                "path": "/homepage",
                "endpoint": self.homepage,
                "methods": ["GET"],
                "auth": "apikey",
                "response_model": CloudDriveInfo,
                "summary": "HomePage",
                "description": "HomePage自定义api",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """把周期检测注册到宿主调度器。"""
        if not self._enabled or not self._cron or not self._cd2_clients:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
        except Exception as error:
            logger.error("定时任务配置错误：%s", error)
            return []
        return [
            {
                "id": "Cd2Assistant",
                "name": "CloudDrive2助手检测服务",
                "trigger": trigger,
                "func": self.check,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单及默认配置。"""
        message_options = [
            {"title": item.value, "value": item.name}
            for item in MessageType
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VSwitch", "enabled", "启用插件", 3),
                            self.__field_control("VSwitch", "notify", "开启通知", 3),
                            self.__field_control("VSwitch", "cd2_restart", "cd2重启一次", 3),
                            self.__field_control("VSwitch", "onlyonce", "立即运行一次", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "cd2_confs",
                                            "label": "cd2配置",
                                            "rows": 2,
                                            "placeholder": "cd2配置1#http://127.0.0.1:19798#admin#123456（一行一个配置）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VCronField", "cron", "检测周期", 4, "5位cron表达式"),
                            self.__field_control("VTextField", "keyword", "检测关键字", 4),
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "chips": True,
                                            "model": "msgtype",
                                            "label": "消息类型",
                                            "items": message_options,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self.__field_control("VTextField", "black_dir", "cd2黑名单目录", 4, "cd2上添加的本地目录(多个目录用英文逗号分隔)"),
                            self.__field_control("VTextField", "cloud_path", "云下载路径", 4),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "周期检测CloudDrive2上传任务，检测是否命中检测关键词，发送通知。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "周期检测CloudDrive2云盘CK是否过期，发送通知（挂载的本地路径可添加黑名单）。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {"type": "success", "variant": "tonal"},
                                        "content": [
                                            {"component": "span", "text": "HomePage配置教程请参考："},
                                            {
                                                "component": "a",
                                                "props": {
                                                    "href": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md",
                                                    "target": "_blank",
                                                },
                                                "text": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/Cd2Assistant.md",
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
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

    @staticmethod
    def __field_control(component: str, model: str, label: str, md: int, placeholder: str | None = None) -> dict:
        """构造配置表单中的单字段列。"""
        props = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}],
        }

    @staticmethod
    def __metric_card(label: str, value: Any) -> dict[str, Any]:
        """构造插件详情页和仪表板共用的状态卡片。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3, "sm": 6},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "d-flex align-center"},
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {"component": "span", "props": {"class": "text-caption"}, "text": label},
                                        {"component": "span", "props": {"class": "text-h6"}, "text": value},
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def __render_instance(self, name: str, info: dict[str, Any]) -> dict[str, Any]:
        """渲染单个 CloudDrive2 实例及其状态字段。"""
        values = [
            ("CPU占用", info.get("cpuUsage")),
            ("内存占用", info.get("memUsageKB")),
            ("运行时间", info.get("uptime")),
            ("打开文件数", info.get("fhTableCount")),
            ("缓存目录数", info.get("dirCacheCount")),
            ("临时文件数", info.get("tempFileCount")),
            ("下载任务数", info.get("download_count", 0)),
            ("上传任务数", info.get("upload_count", 0)),
            ("下载速率", info.get("download_speed", "0KB/s")),
            ("上传速率", info.get("upload_speed", "0KB/s")),
            ("存储空间", info.get("cloud_space")),
        ]
        cards = [
            {
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{"component": "VCard", "props": {"variant": "tonal"}, "content": [{"component": "VCardText", "text": f"{name} ({self._cd2_url.get(name, '')})"}]}],
            }
        ]
        cards.extend(self.__metric_card(label, value) for label, value in values)
        return {"component": "VRow", "content": cards}

    def get_page(self) -> List[dict]:
        """刷新状态后返回插件详情页。"""
        self.__refresh_info_sync()
        return [self.__render_instance(name, self._info.get(name, {})) for name in self._clients]

    def get_dashboard(self, key: str | None = None, **kwargs) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[dict]]]:
        """刷新状态后返回仪表板布局。"""
        if not self._clients:
            return {
                "cols": 12,
                "md": 12,
            }, {"refresh": 10, "border": False}, [{"component": "div", "text": "无法连接CloudDrive2"}]
        self.__refresh_info_sync()
        elements = [self.__render_instance(name, self._info.get(name, {})) for name in self._clients]
        return {"cols": 12, "md": 12}, {"refresh": 10, "border": False}, elements

    def stop_service(self) -> None:
        """停止一次性调度器并释放 CloudDrive2 连接。"""
        scheduler = self._scheduler
        if scheduler:
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception as error:
                logger.error("退出插件失败：%s", error)
            else:
                self._scheduler = None

        for clients in (self._cd2_clients, self._clients):
            for name, client in tuple(clients.items()):
                try:
                    client.close()
                except Exception as error:
                    logger.debug("关闭 CloudDrive2 连接失败：%s", error)
                    continue
                clients.pop(name, None)
        for name in tuple(self._cd2_url):
            if name not in self._cd2_clients and name not in self._clients:
                self._cd2_url.pop(name, None)

    def __run_check(self) -> None:
        """为同步 APScheduler 一次性任务桥接异步检测。"""
        try:
            asyncio.run(self.check())
        except Exception as error:
            logger.error("CloudDrive2助手一次性检测失败：%s", error)

    def __run_restart(self) -> None:
        """为同步 APScheduler 一次性任务桥接异步重启。"""
        try:
            asyncio.run(self.restart_cd2())
        except Exception as error:
            logger.error("CloudDrive2助手一次性重启失败：%s", error)
