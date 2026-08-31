"""提供云盘文件扫描、STRM 生成和处理索引维护能力。"""

from __future__ import annotations

import json
import os
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


@dataclass(frozen=True)
class MonitorConfig:
    """一条源目录到输出目录的稳定路径映射。"""

    source_dir: Path
    target_dir: Path
    library_dir: Optional[Path] = None
    cloud_type: Optional[str] = None
    cloud_path: Optional[Path] = None
    cloud_url: Optional[str] = None


# 配置字段与已发布键一一对应，路径映射和执行开关不能合并为隐式状态。
# pylint: disable=too-many-instance-attributes
class CloudStrm(_PluginBase):
    """扫描云盘挂载目录并在媒体库目标目录生成 STRM 文件。"""

    plugin_name = "云盘Strm生成"
    plugin_desc = "定时扫描云盘文件，生成Strm文件。"
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/"
        "MoviePilot-Plugins/main/icons/create.png"
    )
    plugin_version = "5.0.1"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cloudstrm_"
    plugin_order = 26
    auth_level = 1

    _enabled = False
    _cron = ""
    _rebuild_cron = ""
    _monitor_confs = ""
    _onlyonce = False
    _copy_files = False
    _rebuild = False
    _https = False
    _monitors: list[MonitorConfig] = []
    _cloud_files: set[str] = set()
    _cloud_files_json: Path

    def __init__(self) -> None:
        """初始化实例级状态，避免热重载复用可变缓存或并发锁。"""
        super().__init__()
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()
        self._run_once = False
        self._monitors = []
        self._cloud_files = set()
        self._cloud_files_json = Path()

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并把周期任务交给宿主服务投影。"""
        config = config or {}
        enabled = bool(config.get("enabled"))
        cron = str(config.get("cron") or "").strip()
        rebuild_cron = str(config.get("rebuild_cron") or "").strip()
        monitor_confs = str(config.get("monitor_confs") or "")
        onlyonce = bool(config.get("onlyonce"))
        copy_files = bool(config.get("copy_files"))
        rebuild = bool(config.get("rebuild"))
        https = bool(config.get("https"))
        monitors = self.__parse_monitor_confs(monitor_confs)
        cloud_files_json = self.get_data_path() / "cloud_files.json"

        # 配置切换必须等待在途文件写入结束，保证一次扫描只观察一套映射和开关。
        with self._run_lock:
            self._enabled = enabled
            self._cron = cron
            self._rebuild_cron = rebuild_cron
            self._monitor_confs = monitor_confs
            self._onlyonce = onlyonce
            self._run_once = onlyonce
            self._copy_files = copy_files
            self._rebuild = rebuild
            self._https = https
            self._monitors = monitors
            self._cloud_files = set()
            self._cloud_files_json = cloud_files_json

        if (enabled or onlyonce) and not monitors:
            logger.warning("未获取到可用目录监控配置，请检查")

    @staticmethod
    def __normalise_path(value: str | Path) -> Path:
        """把配置和事件路径规范化为绝对路径，后续映射只按组件比较。"""
        return Path(os.path.abspath(os.path.expanduser(str(value).strip())))

    @classmethod
    def __parse_monitor_confs(  # pylint: disable=too-many-locals
        cls, monitor_confs: str
    ) -> list[MonitorConfig]:
        """解析旧配置的本地媒体库、CD2 和 Alist 三种映射格式。"""
        monitors: list[MonitorConfig] = []
        for raw_line in monitor_confs.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [part.strip() for part in line.split("#")]
            try:
                if len(parts) == 3:
                    source_dir, target_dir, library_dir = parts
                    if not source_dir or not target_dir or not library_dir:
                        raise ValueError("本地映射缺少路径")
                    monitor = MonitorConfig(
                        source_dir=cls.__normalise_path(source_dir),
                        target_dir=cls.__normalise_path(target_dir),
                        library_dir=cls.__normalise_path(library_dir),
                    )
                elif len(parts) == 5:
                    source_dir, target_dir, cloud_type, cloud_path, cloud_url = parts
                    cloud_type = cloud_type.casefold()
                    if cloud_type not in {"cd2", "alist"}:
                        raise ValueError(f"不支持的云盘类型 {cloud_type}")
                    if not source_dir or not target_dir or not cloud_path or not cloud_url:
                        raise ValueError("云盘映射缺少必要配置")
                    monitor = MonitorConfig(
                        source_dir=cls.__normalise_path(source_dir),
                        target_dir=cls.__normalise_path(target_dir),
                        cloud_type=cloud_type,
                        cloud_path=cls.__normalise_path(cloud_path),
                        cloud_url=cloud_url,
                    )
                else:
                    raise ValueError("格式应包含 2 或 4 个 # 分隔符")
            except ValueError as error:
                logger.error(f"{line} 格式错误：{error}")
                continue

            source_root = monitor.source_dir.resolve(strict=False)
            target_root = monitor.target_dir.resolve(strict=False)
            if target_root == source_root or target_root.is_relative_to(source_root):
                logger.error(f"{monitor.target_dir} 是监控目录 {monitor.source_dir} 的子目录，无法监控")
                continue
            monitors.append(monitor)
        return monitors

    @staticmethod
    def __relative_path(root: Path, path: Path) -> Optional[Path]:
        """按路径组件获取相对路径，拒绝同名前缀但非子目录的路径。"""
        try:
            return path.relative_to(root)
        except ValueError:
            return None

    @classmethod
    def __safe_target_path(cls, target_root: Path, relative_path: Path) -> Optional[Path]:
        """确保目标父目录解析后仍位于目标根内，阻止符号链接逃逸。"""
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return None
        target_path = target_root.joinpath(relative_path)
        try:
            resolved_root = target_root.resolve(strict=False)
            resolved_parent = target_path.parent.resolve(strict=False)
            if not resolved_parent.is_relative_to(resolved_root):
                return None
        except OSError:
            return None
        return target_path

    @staticmethod
    def __skip_component(name: str) -> bool:
        """识别隐藏、回收站和不应进入媒体库的附属目录。"""
        return name.startswith(".") or name.casefold() in {
            "@recycle",
            "#recycle",
            "@eadir",
            "extrafanart",
        }

    @classmethod
    def __skip_path(cls, path: Path, source_root: Path) -> bool:
        """按源根的相对组件过滤隐藏文件和受保护目录。"""
        relative_path = cls.__relative_path(source_root, path)
        return relative_path is None or any(
            cls.__skip_component(part) for part in relative_path.parts
        )

    def __iter_files(self, monitor: MonitorConfig) -> Iterator[Path]:
        """以确定性顺序遍历源目录，并在遍历层面剪枝受保护目录。"""
        source_root = monitor.source_dir
        if not source_root.is_dir():
            logger.error(f"监控目录不存在或不是目录：{source_root}")
            return

        for root, dirs, files in os.walk(str(source_root), topdown=True, followlinks=False):
            root_path = Path(root)
            dirs[:] = sorted(
                name
                for name in dirs
                if not self.__skip_component(name)
                and not (root_path / name).is_symlink()
            )
            for name in sorted(files):
                path = root_path / name
                if self.__skip_path(path, source_root):
                    logger.info(f"{path} 是回收站或隐藏的文件，跳过处理")
                    continue
                if path.is_symlink() or not path.is_file():
                    logger.info(f"{path} 不是普通文件，跳过处理")
                    continue
                if not self._copy_files and path.suffix.casefold() not in self.__media_extensions():
                    continue
                yield path

    @staticmethod
    def __media_extensions() -> set[str]:
        """读取宿主公开媒体扩展名并统一大小写。"""
        return {str(extension).casefold() for extension in settings.RMT_MEDIAEXT}

    def __find_monitor(self, source_file: Path) -> Optional[MonitorConfig]:
        """选择最长匹配的监控根，避免相邻或嵌套路径误配。"""
        matches = [
            monitor
            for monitor in self._monitors
            if self.__relative_path(monitor.source_dir, source_file) is not None
        ]
        return max(matches, key=lambda monitor: len(monitor.source_dir.parts), default=None)

    @staticmethod
    def __service_url(scheme: str, cloud_url: str) -> Optional[Tuple[str, str]]:
        """规范化云盘服务地址，并返回基础 URL 与主机组件。"""
        raw_url = cloud_url.strip()
        parsed = urllib.parse.urlsplit(
            raw_url if "://" in raw_url else f"{scheme}://{raw_url}"
        )
        if not parsed.netloc:
            return None
        base_url = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return base_url, parsed.netloc

    def __build_strm_content(  # pylint: disable=too-many-return-statements
        self,
        monitor: MonitorConfig,
        source_file: Path,
        dest_file: Path,
    ) -> Optional[str]:
        """根据映射模式生成媒体服务器可读取的本地路径或云盘 URL。"""
        relative_path = self.__relative_path(monitor.source_dir, source_file)
        if relative_path is None:
            return None

        scheme = "https" if self._https else "http"
        if monitor.cloud_type:
            if monitor.cloud_path is None or monitor.cloud_url is None:
                return None
            cloud_relative = self.__relative_path(monitor.cloud_path, source_file)
            if cloud_relative is None:
                logger.error(f"文件 {source_file} 不在云盘挂载根 {monitor.cloud_path} 内")
                return None
            service = self.__service_url(scheme, monitor.cloud_url)
            if service is None:
                logger.error(f"云盘服务地址无效：{monitor.cloud_url}")
                return None
            base_url, host = service
            cloud_path = "/" + cloud_relative.as_posix().lstrip("/")
            encoded_path = urllib.parse.quote(cloud_path, safe="")
            if monitor.cloud_type == "cd2":
                return f"{base_url}/static/{scheme}/{host}/False/{encoded_path}"
            if monitor.cloud_type == "alist":
                return f"{base_url}/d/{encoded_path}"
            return None

        if monitor.library_dir is None:
            return None
        del dest_file
        return (monitor.library_dir / relative_path).as_posix()

    @staticmethod
    def __atomic_create_text(path: Path, content: str) -> bool:
        """以同目录临时文件和排他硬链接完成原子且不覆盖的文本创建。"""
        if path.exists() or path.is_symlink():
            return True
        fd: Optional[int] = None
        temporary_name: Optional[str] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
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
                return True
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
    def __copy_without_overwrite(source: Path, target: Path) -> bool:
        """复制非媒体文件并以排他链接提交，避免竞态覆盖既有目标。"""
        if target.exists() or target.is_symlink():
            return True
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
                return True
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

    def __process_file(  # pylint: disable=too-many-return-statements
        self, monitor: MonitorConfig, source_file: Path
    ) -> bool:
        """处理单个普通源文件，只有目标已存在或成功生成后才算完成。"""
        if (
            not source_file.exists()
            or source_file.is_symlink()
            or not source_file.is_file()
            or self.__skip_path(source_file, monitor.source_dir)
        ):
            return False
        relative_path = self.__relative_path(monitor.source_dir, source_file)
        if relative_path is None:
            return False
        dest_file = self.__safe_target_path(monitor.target_dir, relative_path)
        if dest_file is None:
            logger.error(f"目标路径越界，跳过文件：{source_file}")
            return False
        if dest_file.exists() or dest_file.is_symlink():
            logger.info(f"目标文件 {dest_file} 已存在，跳过处理")
            return True

        if source_file.suffix.casefold() in self.__media_extensions():
            strm_path = dest_file.with_suffix(".strm")
            content = self.__build_strm_content(monitor, source_file, dest_file)
            if content is None:
                logger.error(f"无法生成 {source_file} 的 STRM 内容")
                return False
            if self.__atomic_create_text(strm_path, content):
                logger.info(f"创建strm文件 {strm_path}")
                return True
            return False

        if not self._copy_files:
            return False
        return self.__copy_without_overwrite(source_file, dest_file)

    def __load_index(self) -> set[str]:
        """加载旧索引并规范化路径；损坏索引按空索引处理以便重建。"""
        if not self._cloud_files_json.exists():
            return set()
        try:
            content = json.loads(self._cloud_files_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"读取云盘文件索引失败，将重新扫描：{error}")
            return set()
        if not isinstance(content, list):
            logger.warning("云盘文件索引格式无效，将重新扫描")
            return set()
        return {
            str(self.__normalise_path(item))
            for item in content
            if isinstance(item, str) and item.strip()
        }

    def __write_index(self, files: set[str]) -> bool:
        """在数据目录内原子替换索引，避免进程中断留下半份 JSON。"""
        fd: Optional[int] = None
        temporary_name: Optional[str] = None
        try:
            self._cloud_files_json.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{self._cloud_files_json.name}.",
                dir=self._cloud_files_json.parent,
            )
            file_obj = os.fdopen(fd, "w", encoding="utf-8")
            fd = None
            with file_obj:
                json.dump(sorted(files), file_obj, ensure_ascii=False)
                file_obj.flush()
                os.fsync(file_obj.fileno())
            os.replace(temporary_name, self._cloud_files_json)
            return True
        except OSError as error:
            logger.error(f"写入云盘文件索引失败：{error}")
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

    def __run_scan(  # pylint: disable=too-many-branches
        self,
        force_rebuild: bool = False,
        event_data: Optional[dict] = None,
        *,
        lock_held: bool = False,
        once_consumed: bool = False,
    ) -> bool:
        """以 single-flight 门禁执行增量或完整扫描，并原子提交索引。"""
        acquired = False
        # 非阻塞获取定义重叠扫描的跳过语义，不能改用阻塞上下文。
        # pylint: disable-next=consider-using-with
        if not lock_held and not self._run_lock.acquire(blocking=False):
            logger.warning("云盘strm生成任务正在运行，跳过重复触发")
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
                    title="开始云盘strm生成 ...",
                    userid=event_data.get("user"),
                )
            rebuild = force_rebuild or self._rebuild or not self._cloud_files_json.exists()
            previous = set() if rebuild else self.__load_index()
            processed = set() if rebuild else previous
            success = True
            changed = False
            if not self._monitors:
                success = False
            for monitor in self._monitors:
                if not monitor.source_dir.is_dir():
                    success = False
                    continue
                for source_file in self.__iter_files(monitor):
                    source_key = str(source_file)
                    if not rebuild and source_key in processed:
                        continue
                    logger.info(f"扫描到新文件 {source_file}，正在开始处理")
                    if self.__process_file(monitor, source_file):
                        if source_key not in processed:
                            processed.add(source_key)
                            changed = True
                    else:
                        success = False

            if rebuild or changed:
                # 完整扫描全部失败时保留旧索引，避免失败结果覆盖可恢复的处理状态。
                if success or processed:
                    if not self.__write_index(processed):
                        success = False
                    else:
                        self._cloud_files = processed
            else:
                self._cloud_files = processed

            if success and self._rebuild:
                self._rebuild = False
                self.__update_config()
            logger.info("云盘strm生成任务完成" if success else "云盘strm生成任务结束，存在失败文件")
            if event_data:
                self.post_message(
                    channel=event_data.get("channel"),
                    title="云盘strm生成任务完成！" if success else "云盘strm生成任务存在失败文件",
                    userid=event_data.get("user"),
                )
            return success
        finally:
            if acquired:
                self._run_lock.release()

    def __consume_once(self) -> bool:
        """配置持久化成功后才消费一次性扫描意图。"""
        with self._once_lock:
            if not self._run_once or not self._monitors:
                return False
            try:
                persisted = self.__update_config(onlyonce=False)
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error(f"保存一次性扫描状态失败：{error}")
                return False
            if persisted is False:
                logger.error("保存一次性扫描状态失败：宿主拒绝更新配置")
                return False
            self._onlyonce = False
            self._run_once = False
            return True

    def __run_once_scan(self) -> bool:
        """等待扫描执行槽，在可靠消费一次性意图后完成扫描。"""
        with self._run_lock:
            if not self.__consume_once():
                return False
            return self.__run_scan(lock_held=True, once_consumed=True)

    @eventmanager.register(EventType.PluginAction)
    def cloudstrm_file(  # pylint: disable=too-many-return-statements
        self, event: Event = None
    ) -> bool:
        """响应单文件联动事件，并复用与全量扫描相同的路径安全边界。"""
        if not event:
            return False
        event_data = event.event_data or {}
        if event_data.get("action") != "cloudstrm_file":
            return False
        file_path = event_data.get("file_path")
        if not file_path:
            return False
        source_file = self.__normalise_path(file_path)
        # 单文件联动与全量扫描共享非阻塞 single-flight 门禁。
        # pylint: disable-next=consider-using-with
        if not self._run_lock.acquire(blocking=False):
            logger.warning("云盘strm生成任务正在运行，跳过重复触发")
            return False
        try:
            if not (self._enabled or self._run_once):
                return False
            monitor = self.__find_monitor(source_file)
            if monitor is None:
                logger.error(f"未找到文件 {source_file} 对应的监控目录")
                return False
            if self.__skip_path(source_file, monitor.source_dir):
                return False
            if (
                not self._copy_files
                and source_file.suffix.casefold() not in self.__media_extensions()
            ):
                return False
            if not self.__process_file(monitor, source_file):
                return False
            files = self.__load_index()
            files.add(str(source_file))
            self._cloud_files = files
            return self.__write_index(files)
        finally:
            self._run_lock.release()

    @eventmanager.register(EventType.PluginAction)
    def scan(self, event: Event = None) -> bool:
        """响应远程命令或宿主服务的全量/增量扫描。"""
        event_data = None
        if event:
            event_data = event.event_data or {}
            if event_data.get("action") != "cloud_strm":
                return False
        if not (self._enabled or self._run_once):
            logger.error("插件未开启")
            return False
        return self.__run_scan(event_data=event_data)

    def rebuild_index(self) -> bool:
        """由宿主服务触发一次完整索引重建，不删除任何已有输出。"""
        if not self._enabled:
            return False
        return self.__run_scan(force_rebuild=True)

    def __update_config(self, *, onlyonce: Optional[bool] = None) -> Optional[bool]:
        """保存一次性和重建开关被消费后的配置状态。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce if onlyonce is None else onlyonce,
                "rebuild": self._rebuild,
                "copy_files": self._copy_files,
                "https": self._https,
                "cron": self._cron,
                "rebuild_cron": self._rebuild_cron,
                "monitor_confs": self._monitor_confs,
            }
        )

    def get_state(self) -> bool:
        """一次性任务待执行时保持插件可被宿主服务投影。"""
        return bool(self._enabled or self._run_once)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """声明远程全量扫描命令。"""
        return [
            {
                "cmd": "/cloud_strm",
                "event": EventType.PluginAction,
                "desc": "云盘strm文件生成",
                "category": "",
                "data": {"action": "cloud_strm"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """声明宿主统一调度的周期、重建和一次性服务。"""
        services: list[dict] = []
        timezone = pytz.timezone(str(settings.TZ))
        if self._run_once:
            run_date = datetime.now(tz=timezone) + timedelta(seconds=3)
            services.append(
                {
                    "id": "CloudStrmOnce",
                    "name": "云盘strm全量执行",
                    "trigger": DateTrigger(run_date=run_date),
                    "func": self.__run_once_scan,
                    "kwargs": {},
                }
            )
        if not self._enabled:
            return services
        if self._cron:
            try:
                services.append(
                    {
                        "id": "CloudStrm",
                        "name": "云盘strm文件生成服务",
                        "trigger": CronTrigger.from_crontab(
                            self._cron, timezone=timezone
                        ),
                        "func": self.scan,
                        "kwargs": {},
                    }
                )
            except (TypeError, ValueError) as error:
                logger.error(f"执行周期配置错误：{error}")
        if self._rebuild_cron:
            try:
                services.append(
                    {
                        "id": "CloudStrmRebuild",
                        "name": "云盘strm重建索引服务",
                        "trigger": CronTrigger.from_crontab(
                            self._rebuild_cron, timezone=timezone
                        ),
                        "func": self.rebuild_index,
                        "kwargs": {},
                    }
                )
            except (TypeError, ValueError) as error:
                logger.error(f"重建索引周期配置错误：{error}")
        return services

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册 HTTP API。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面及与旧版本一致的配置字段。"""
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
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "全量运行一次"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "rebuild", "label": "重建索引"},
                                    }
                                ],
                            },
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
                                            "model": "rebuild_cron",
                                            "label": "重建索引周期",
                                            "placeholder": "0 1 * * *",
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
                            "placeholder": "监控目录#目的目录#媒体服务器内源文件路径",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "copy_files", "label": "复制非媒体文件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "https", "label": "启用https"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "目录监控格式：1.监控目录#目的目录#媒体服务器内源文件路径；"
                                "2.监控目录#目的目录#cd2#cd2挂载本地跟路径#cd2服务地址；"
                                "3.监控目录#目的目录#alist#alist挂载本地跟路径#alist服务地址。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "rebuild_cron": "",
            "onlyonce": False,
            "rebuild": False,
            "copy_files": False,
            "https": False,
            "monitor_confs": "",
        }

    def get_page(self) -> List[dict]:
        """当前插件不注册详情页面。"""
        return []

    def stop_service(self) -> None:
        """等待在途文件写入结束，并阻止已注销的宿主回调继续执行。"""
        with self._run_lock:
            self._enabled = False
            self._onlyonce = False
            self._run_once = False
