"""提供增量目录归档和云盘 STRM 生成能力。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger


DEFAULT_MEDIA_EXTENSIONS = (
    ".mp4,.mkv,.ts,.iso,.rmvb,.avi,.mov,.mpeg,.mpg,.wmv,.3gp,.asf,.m4v,"
    ".flv,.m2ts,.strm,.tp,.f4v"
)


@dataclass(frozen=True)
class IncrementMonitor:
    """一条增量目录、源目录和媒体库输出目录的稳定映射。"""

    increment_dir: Path
    source_dir: Path
    target_dir: Path
    library_dir: Optional[Path] = None
    cloud_type: Optional[str] = None
    cloud_path: Optional[Path] = None
    cloud_url: Optional[str] = None


# 配置字段分别控制复制、源删除和输出协议，不能合并为隐式模式。
# pylint: disable=too-many-instance-attributes
class CloudStrmIncrement(_PluginBase):
    """把增量目录安全归档到源树，并为归档文件生成媒体库输出。"""

    plugin_name = "云盘Strm生成（增量版）"
    plugin_desc = "扫描增量目录并生成Strm文件。"
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/"
        "MoviePilot-Plugins/main/icons/create.png"
    )
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cloudstrm_"
    plugin_order = 26
    auth_level = 1

    def __init__(self) -> None:
        """初始化实例级运行状态，避免热重载共享锁和可变配置。"""
        super().__init__()
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()
        self._enabled = False
        self._cron = ""
        self._onlyonce = False
        self._run_once = False
        self._copy_files = False
        self._https = False
        self._del_source = True
        self._monitor_confs = ""
        self._no_del_dirs = ""
        self._preserved_dir_names: set[str] = set()
        self._rmt_mediaext = DEFAULT_MEDIA_EXTENSIONS
        self._monitors: list[IncrementMonitor] = []

    def init_plugin(self, config: dict = None) -> None:
        """解析完整候选配置，并在在途任务收敛后一次性切换。"""
        config = config or {}
        enabled = bool(config.get("enabled"))
        cron = str(config.get("cron") or "").strip()
        onlyonce = bool(config.get("onlyonce"))
        copy_files = bool(config.get("copy_files"))
        https = bool(config.get("https"))
        del_source = bool(config.get("del_source", True))
        monitor_confs = str(config.get("monitor_confs") or "")
        no_del_dirs = str(config.get("no_del_dirs") or "")
        rmt_mediaext = str(config.get("rmt_mediaext") or DEFAULT_MEDIA_EXTENSIONS)
        monitors = self.__parse_monitor_confs(monitor_confs)
        preserved_names = {
            item.strip().casefold()
            for item in re.split(r"[,，\n]+", no_del_dirs)
            if item.strip()
        }

        with self._run_lock:
            self._enabled = enabled
            self._cron = cron
            self._onlyonce = onlyonce
            self._run_once = onlyonce
            self._copy_files = copy_files
            self._https = https
            self._del_source = del_source
            self._monitor_confs = monitor_confs
            self._no_del_dirs = no_del_dirs
            self._preserved_dir_names = preserved_names
            self._rmt_mediaext = rmt_mediaext
            self._monitors = monitors

        if (enabled or onlyonce) and not monitors:
            logger.warning("未获取到可用增量目录配置，请检查")

    @staticmethod
    def __normalise_path(value: str | Path) -> Path:
        """把配置和事件路径规范化为绝对路径。"""
        return Path(os.path.abspath(os.path.expanduser(str(value).strip())))

    @staticmethod
    def __paths_overlap(first: Path, second: Path) -> bool:
        """判断两个目录是否相同或存在任一方向的包含关系。"""
        first_root = first.resolve(strict=False)
        second_root = second.resolve(strict=False)
        return (
            first_root == second_root
            or first_root.is_relative_to(second_root)
            or second_root.is_relative_to(first_root)
        )

    @classmethod
    def __parse_monitor_confs(  # pylint: disable=too-many-locals
        cls, monitor_confs: str
    ) -> list[IncrementMonitor]:
        """解析本地媒体库、CD2 和 Alist 三种旧配置格式。"""
        monitors: list[IncrementMonitor] = []
        for raw_line in monitor_confs.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("#")]
            try:
                if len(parts) == 4:
                    increment_dir, source_dir, target_dir, library_dir = parts
                    if not all(parts):
                        raise ValueError("本地映射缺少路径")
                    monitor = IncrementMonitor(
                        increment_dir=cls.__normalise_path(increment_dir),
                        source_dir=cls.__normalise_path(source_dir),
                        target_dir=cls.__normalise_path(target_dir),
                        library_dir=cls.__normalise_path(library_dir),
                    )
                elif len(parts) == 6:
                    increment_dir, source_dir, target_dir, cloud_type, cloud_path, cloud_url = parts
                    cloud_type = cloud_type.casefold()
                    if cloud_type not in {"cd2", "alist"}:
                        raise ValueError(f"不支持的云盘类型 {cloud_type}")
                    if not all((increment_dir, source_dir, target_dir, cloud_path, cloud_url)):
                        raise ValueError("云盘映射缺少必要配置")
                    monitor = IncrementMonitor(
                        increment_dir=cls.__normalise_path(increment_dir),
                        source_dir=cls.__normalise_path(source_dir),
                        target_dir=cls.__normalise_path(target_dir),
                        cloud_type=cloud_type,
                        cloud_path=cls.__normalise_path(cloud_path),
                        cloud_url=cloud_url,
                    )
                else:
                    raise ValueError("格式应包含 3 或 5 个 # 分隔符")

                roots = (monitor.increment_dir, monitor.source_dir, monitor.target_dir)
                if any(
                    cls.__paths_overlap(roots[left], roots[right])
                    for left, right in ((0, 1), (0, 2), (1, 2))
                ):
                    raise ValueError("增量、源和目的目录不能相互包含")
            except (OSError, ValueError) as error:
                logger.error(f"{line} 格式错误：{error}")
                continue
            monitors.append(monitor)
        return monitors

    @staticmethod
    def __relative_path(root: Path, path: Path) -> Optional[Path]:
        """按路径组件获取相对路径，拒绝同名前缀路径。"""
        try:
            return path.relative_to(root)
        except ValueError:
            return None

    @classmethod
    def __safe_target_path(cls, root: Path, relative_path: Path) -> Optional[Path]:
        """确保目标父目录解析后仍位于指定根目录内。"""
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return None
        target = root / relative_path
        try:
            resolved_root = root.resolve(strict=False)
            if not target.parent.resolve(strict=False).is_relative_to(resolved_root):
                return None
        except OSError:
            return None
        return target

    @staticmethod
    def __skip_component(name: str) -> bool:
        """识别隐藏、回收站和媒体附属目录。"""
        return name.startswith(".") or name.casefold() in {
            "@recycle",
            "#recycle",
            "@eadir",
            "extrafanart",
        }

    @classmethod
    def __skip_path(cls, path: Path, root: Path) -> bool:
        """按增量根的相对组件过滤受保护路径。"""
        relative = cls.__relative_path(root, path)
        return relative is None or any(cls.__skip_component(part) for part in relative.parts)

    def __media_extensions(self) -> set[str]:
        """解析用户配置的媒体扩展名并统一大小写和前导点。"""
        extensions = set()
        for item in self._rmt_mediaext.split(","):
            extension = item.strip().casefold()
            if not extension:
                continue
            extensions.add(extension if extension.startswith(".") else f".{extension}")
        return extensions

    @classmethod
    def __has_symlink_component(cls, root: Path, path: Path) -> bool:
        """拒绝根目录以下任一软链接组件，避免词法子路径解析到根外。"""
        relative = cls.__relative_path(root, path)
        if relative is None:
            return True
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return True
        return False

    @staticmethod
    def __same_file_contents(first: Path, second: Path) -> bool:
        """逐块比较两个普通文件，确认失败事务可安全从归档副本恢复。"""
        try:
            if (
                first.is_symlink()
                or second.is_symlink()
                or not first.is_file()
                or not second.is_file()
                or first.stat().st_size != second.stat().st_size
            ):
                return False
            with first.open("rb") as first_file, second.open("rb") as second_file:
                while True:
                    first_chunk = first_file.read(1024 * 1024)
                    second_chunk = second_file.read(1024 * 1024)
                    if first_chunk != second_chunk:
                        return False
                    if not first_chunk:
                        return True
        except OSError:
            return False

    def __iter_increment_files(self, monitor: IncrementMonitor) -> Iterator[Path]:
        """以确定性顺序遍历增量目录并剪枝隐藏目录和软链接。"""
        if not monitor.increment_dir.is_dir():
            logger.error(f"增量目录不存在或不是目录：{monitor.increment_dir}")
            return
        media_extensions = self.__media_extensions()
        for root, dirs, files in os.walk(
            str(monitor.increment_dir), topdown=True, followlinks=False
        ):
            root_path = Path(root)
            dirs[:] = sorted(
                name
                for name in dirs
                if not self.__skip_component(name) and not (root_path / name).is_symlink()
            )
            for name in sorted(files):
                path = root_path / name
                if (
                    self.__skip_path(path, monitor.increment_dir)
                    or path.is_symlink()
                    or not path.is_file()
                ):
                    continue
                if not self._copy_files and path.suffix.casefold() not in media_extensions:
                    continue
                yield path

    @staticmethod
    def __service_url(scheme: str, cloud_url: str) -> Optional[Tuple[str, str]]:
        """规范化云盘服务地址，并返回基础 URL 与主机组件。"""
        raw_url = cloud_url.strip()
        parsed = urllib.parse.urlsplit(
            raw_url if "://" in raw_url else f"{scheme}://{raw_url}"
        )
        if not parsed.netloc:
            return None
        return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}", parsed.netloc

    def __build_strm_content(  # pylint: disable=too-many-return-statements
        self, monitor: IncrementMonitor, source_file: Path
    ) -> Optional[str]:
        """根据稳定映射生成媒体服务器路径或云盘 URL。"""
        relative = self.__relative_path(monitor.source_dir, source_file)
        if relative is None:
            return None
        if not monitor.cloud_type:
            if monitor.library_dir is None:
                return None
            return (monitor.library_dir / relative).as_posix()

        if monitor.cloud_path is None or monitor.cloud_url is None:
            return None
        cloud_relative = self.__relative_path(monitor.cloud_path, source_file)
        if cloud_relative is None:
            logger.error(f"文件 {source_file} 不在云盘挂载根 {monitor.cloud_path} 内")
            return None
        scheme = "https" if self._https else "http"
        service = self.__service_url(scheme, monitor.cloud_url)
        if service is None:
            return None
        base_url, host = service
        encoded_path = urllib.parse.quote(
            "/" + cloud_relative.as_posix().lstrip("/"), safe=""
        )
        if monitor.cloud_type == "cd2":
            return f"{base_url}/static/{scheme}/{host}/False/{encoded_path}"
        if monitor.cloud_type == "alist":
            return f"{base_url}/d/{encoded_path}"
        return None

    @staticmethod
    def __atomic_create_text(path: Path, content: str) -> bool:
        """以同目录临时文件排他提交文本，不覆盖既有目标。"""
        if path.is_symlink():
            logger.error(f"目标是软链接，拒绝写入：{path}")
            return False
        if path.exists():
            return path.is_file()
        fd: Optional[int] = None
        temporary_name: Optional[str] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            file_obj = os.fdopen(fd, "w", encoding="utf-8")
            fd = None
            with file_obj:
                file_obj.write(content)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            os.chmod(temporary_name, 0o644)
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                return path.is_file() and not path.is_symlink()
            return True
        except OSError as error:
            logger.error(f"创建文件失败 {path}：{error}")
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    @staticmethod
    def __copy_without_overwrite(  # pylint: disable=too-many-return-statements
        source: Path, target: Path, *, existing_ok: bool
    ) -> bool:
        """使用同目录临时文件排他提交副本，避免检查后覆盖竞态。"""
        if target.is_symlink():
            logger.error(f"目标是软链接，拒绝复制：{target}")
            return False
        if target.exists():
            return existing_ok and target.is_file()
        fd: Optional[int] = None
        temporary_name: Optional[str] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", dir=target.parent
            )
            os.close(fd)
            fd = None
            shutil.copy2(source, temporary_name)
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                return existing_ok and target.is_file() and not target.is_symlink()
            return True
        except OSError as error:
            logger.error(f"复制文件失败 {source} -> {target}：{error}")
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass

    def __process_source_file(  # pylint: disable=too-many-return-statements
        self, monitor: IncrementMonitor, source_file: Path
    ) -> bool:
        """为已归档源文件生成 STRM 或复制附属文件。"""
        try:
            resolved_root = monitor.source_dir.resolve(strict=False)
            resolved_file = source_file.resolve(strict=True)
        except OSError:
            return False
        if (
            not source_file.exists()
            or source_file.is_symlink()
            or not source_file.is_file()
        ):
            return False
        if (
            self.__skip_path(source_file, monitor.source_dir)
            or self.__has_symlink_component(monitor.source_dir, source_file)
            or not resolved_file.is_relative_to(resolved_root)
        ):
            return False
        relative = self.__relative_path(monitor.source_dir, source_file)
        if relative is None:
            return False
        target_file = self.__safe_target_path(monitor.target_dir, relative)
        if target_file is None:
            logger.error(f"目标路径越界，跳过文件：{source_file}")
            return False
        if source_file.suffix.casefold() in self.__media_extensions():
            content = self.__build_strm_content(monitor, source_file)
            if content is None:
                return False
            return self.__atomic_create_text(target_file.with_suffix(".strm"), content)
        if not self._copy_files:
            return False
        return self.__copy_without_overwrite(source_file, target_file, existing_ok=True)

    def __cleanup_increment_parents(
        self, monitor: IncrementMonitor, start_dir: Path
    ) -> None:
        """只清理增量根内的空目录，并在保留目录或异常处停止。"""
        root = monitor.increment_dir.resolve(strict=False)
        current = start_dir
        while current != root:
            try:
                resolved = current.resolve(strict=False)
                if not resolved.is_relative_to(root):
                    return
                if current.name.casefold() in self._preserved_dir_names:
                    return
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def __process_increment_file(  # pylint: disable=too-many-return-statements
        self, monitor: IncrementMonitor, increment_file: Path
    ) -> bool:
        """归档单个增量文件；输出成功后才允许删除增量源。"""
        relative = self.__relative_path(monitor.increment_dir, increment_file)
        if relative is None:
            return False
        source_file = self.__safe_target_path(monitor.source_dir, relative)
        if source_file is None:
            logger.error(f"源路径越界，跳过文件：{increment_file}")
            return False

        source_exists = source_file.exists() or source_file.is_symlink()
        if source_exists:
            if not self.__same_file_contents(increment_file, source_file):
                logger.error(f"源文件 {source_file} 已存在且内容不同，拒绝覆盖")
                return False
            logger.warning(f"检测到可恢复的归档副本 {source_file}，继续重试输出")
        if not self.__copy_without_overwrite(
            increment_file, source_file, existing_ok=source_exists
        ):
            return False
        if not self.__process_source_file(monitor, source_file):
            return False
        if not self._del_source:
            return True
        try:
            increment_file.unlink()
        except OSError as error:
            logger.error(f"删除已归档增量文件失败 {increment_file}：{error}")
            return False
        self.__cleanup_increment_parents(monitor, increment_file.parent)
        return True

    def __run_scan(
        self,
        event_data: Optional[dict] = None,
        *,
        lock_held: bool = False,
        once_consumed: bool = False,
    ) -> bool:
        """以 single-flight 门禁执行一次完整增量扫描。"""
        acquired = False
        # 非阻塞获取定义重复调度的跳过语义。
        # pylint: disable-next=consider-using-with
        if not lock_held and not self._run_lock.acquire(blocking=False):
            logger.warning("云盘增量生成任务正在运行，跳过重复触发")
            return False
        if not lock_held:
            acquired = True
        try:
            if not once_consumed and not (self._enabled or self._run_once):
                logger.error("插件未开启")
                return False
            if event_data:
                self.post_message(
                    channel=event_data.get("channel"),
                    title="开始云盘增量strm生成 ...",
                    userid=event_data.get("user"),
                )
            success = bool(self._monitors)
            for monitor in self._monitors:
                if not monitor.increment_dir.is_dir():
                    success = False
                    continue
                for increment_file in self.__iter_increment_files(monitor):
                    if not self.__process_increment_file(monitor, increment_file):
                        success = False
            if event_data:
                self.post_message(
                    channel=event_data.get("channel"),
                    title=(
                        "云盘增量strm生成任务完成！"
                        if success
                        else "云盘增量strm生成任务存在失败文件"
                    ),
                    userid=event_data.get("user"),
                )
            return success
        finally:
            if acquired:
                self._run_lock.release()

    def __update_config(self, *, onlyonce: Optional[bool] = None) -> Optional[bool]:
        """持久化稳定配置，并允许可靠消费一次性开关。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "cron": self._cron,
                "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
                "copy_files": self._copy_files,
                "https": self._https,
                "del_source": self._del_source,
                "monitor_confs": self._monitor_confs,
                "no_del_dirs": self._no_del_dirs,
                "rmt_mediaext": self._rmt_mediaext,
            }
        )

    def __consume_once(self) -> bool:
        """配置持久化成功后才消费一次性任务意图。"""
        with self._once_lock:
            if not self._run_once or not self._monitors:
                return False
            try:
                persisted = self.__update_config(onlyonce=False)
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error(f"保存一次性扫描状态失败：{error}")
                return False
            if persisted is False:
                return False
            self._onlyonce = False
            self._run_once = False
            return True

    def __run_once_scan(self) -> bool:
        """等待执行槽，可靠消费一次性意图后完成扫描。"""
        with self._run_lock:
            if not self.__consume_once():
                return False
            return self.__run_scan(lock_held=True, once_consumed=True)

    @eventmanager.register(EventType.PluginAction)
    def cloudstrm_file(self, event: Event = None) -> bool:
        """处理实时监控插件传入的已归档源文件。"""
        if not event:
            return False
        event_data = event.event_data or {}
        if event_data.get("action") != "cloudstrm_file" or not event_data.get("file_path"):
            return False
        source_file = self.__normalise_path(event_data["file_path"])
        # pylint: disable-next=consider-using-with
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            if not (self._enabled or self._run_once):
                return False
            matches = [
                monitor
                for monitor in self._monitors
                if self.__relative_path(monitor.source_dir, source_file) is not None
            ]
            monitor = max(
                matches, key=lambda item: len(item.source_dir.parts), default=None
            )
            if monitor is None or self.__skip_path(source_file, monitor.source_dir):
                return False
            return self.__process_source_file(monitor, source_file)
        finally:
            self._run_lock.release()

    @eventmanager.register(EventType.PluginAction)
    def scan(self, event: Event = None) -> bool:
        """响应远程命令或宿主服务的增量扫描。"""
        event_data = None
        if event:
            event_data = event.event_data or {}
            if event_data.get("action") != "cloud_strm_increment":
                return False
        return self.__run_scan(event_data=event_data)

    def get_state(self) -> bool:
        """一次性任务待执行时保持宿主服务投影可见。"""
        return bool(self._enabled or self._run_once)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """声明远程增量扫描命令。"""
        return [
            {
                "cmd": "/cloud_strm_increment",
                "event": EventType.PluginAction,
                "desc": "云盘strm文件生成(增量版)",
                "category": "",
                "data": {"action": "cloud_strm_increment"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """声明宿主统一调度的 cron 和一次性服务。"""
        services: list[dict] = []
        timezone = pytz.timezone(str(settings.TZ))
        if self._run_once:
            services.append(
                {
                    "id": "CloudStrmIncrementOnce",
                    "name": "云盘增量strm文件生成一次性服务",
                    "trigger": DateTrigger(
                        run_date=datetime.now(tz=timezone) + timedelta(seconds=3),
                        timezone=timezone,
                    ),
                    "func": self.__run_once_scan,
                    "kwargs": {},
                }
            )
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron, timezone=timezone)
            except (TypeError, ValueError) as error:
                logger.error(f"定时任务配置错误：{error}")
            else:
                services.append(
                    {
                        "id": "CloudStrmIncrement",
                        "name": "云盘增量strm文件生成服务",
                        "trigger": trigger,
                        "func": self.scan,
                        "kwargs": {},
                    }
                )
        return services

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册 HTTP API。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回增量扫描、源删除和目录映射配置表单。"""
        switches = [
            ("enabled", "启用插件"),
            ("onlyonce", "立即运行一次"),
            ("copy_files", "复制非媒体文件"),
            ("del_source", "删除源文件"),
            ("https", "启用https"),
        ]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": model, "label": label},
                                    }
                                ],
                            }
                            for model, label in switches
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "生成周期",
                                            "placeholder": "0 0 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "no_del_dirs",
                                            "label": "保留路径",
                                            "placeholder": "series,movies,downloads,others",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "monitor_confs",
                            "label": "监控目录",
                            "rows": 5,
                            "placeholder": (
                                "增量目录#监控目录#目的目录#媒体服务器内源文件路径"
                            ),
                        },
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "rmt_mediaext",
                            "label": "视频格式",
                            "rows": 2,
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "onlyonce": False,
            "copy_files": False,
            "del_source": True,
            "https": False,
            "monitor_confs": "",
            "no_del_dirs": "",
            "rmt_mediaext": DEFAULT_MEDIA_EXTENSIONS,
        }

    def get_page(self) -> List[dict]:
        """当前插件不注册详情页面。"""
        return []

    def stop_service(self) -> None:
        """等待在途文件操作结束，并阻止过期宿主回调继续执行。"""
        with self._run_lock:
            self._enabled = False
            self._onlyonce = False
            self._run_once = False
