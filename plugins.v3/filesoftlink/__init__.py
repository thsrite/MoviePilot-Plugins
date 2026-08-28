from __future__ import annotations

import asyncio
import datetime
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.plugins import _PluginBase
from app.schemas.event import PluginActionEventData
from app.schemas.types import EventType, SystemConfigKey
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import AsyncRequestUtils
from app.sdk.utilities import SystemUtils


class FileMonitorHandler(FileSystemEventHandler):
    """把 watchdog 的文件事件转交给插件实例。"""

    def __init__(self, monpath: str, sync: "FileSoftLink", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event: Any) -> None:
        self.sync.event_handler(
            event=event,
            text="创建",
            mon_path=self._watch_path,
            event_path=event.src_path,
        )

    def on_moved(self, event: Any) -> None:
        self.sync.event_handler(
            event=event,
            text="移动",
            mon_path=self._watch_path,
            event_path=event.dest_path,
        )


class FileSoftLink(_PluginBase):
    """监控下载目录并将文件安全地软链接或复制到目标目录。"""

    plugin_name = "实时软连接"
    plugin_desc = "监控目录文件变化，媒体文件软连接，其他文件可选复制。"
    plugin_icon = "softlink.png"
    plugin_version = "3.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "filesoftlink_"
    plugin_order = 10
    auth_level = 1

    _DEFAULT_MEDIA_EXTENSIONS = (
        ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, "
        ".m4v, .flv, .m2ts, .strm,.tp, .f4v"
    )
    _MONITOR_MODES = {"compatibility", "fast", "nomonitor"}

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: Optional[BackgroundScheduler] = None
        self._observer: list[Any] = []
        self._enabled = False
        self._onlyonce = False
        self._copy_files = False
        self._cron = ""
        self._url = ""
        self._force = False
        self._size = 0.0
        self._sync_interval = 0.0
        self._mode = "compatibility"
        self._monitor_dirs = ""
        self._exclude_keywords = ""
        self._exclude_patterns: list[re.Pattern[str]] = []
        self._media_extensions = self._parse_extensions(self._DEFAULT_MEDIA_EXTENSIONS)
        self._dirconf: Dict[str, Optional[Path]] = {}
        self._categoryconf: Dict[str, Optional[list[str]]] = {}
        self._monitor_modes: Dict[str, str] = {}
        self._lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        """停止旧实例后读取配置，并重建监听器和一次性任务。"""
        self.stop_service()
        if self._observer or self._scheduler is not None:
            logger.error("旧实时软链接服务未完全停止，跳过重新初始化")
            return
        config = config or {}

        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._copy_files = bool(config.get("copy_files", True))
        self._force = bool(config.get("force", False))
        self._mode = self._normalize_mode(config.get("mode"))
        self._monitor_dirs = str(config.get("monitor_dirs") or "")
        self._exclude_keywords = str(config.get("exclude_keywords") or "")
        self._cron = str(config.get("cron") or "").strip()
        self._url = str(config.get("url") or "").strip()
        self._size = self._parse_nonnegative_float(config.get("size"), 0.0)
        self._sync_interval = self._parse_nonnegative_float(
            config.get("sync_interval"), 0.0
        )
        extension_value = str(config.get("rmt_mediaext") or "").strip()
        self._media_extensions = self._parse_extensions(
            extension_value or self._DEFAULT_MEDIA_EXTENSIONS
        )
        self._exclude_patterns = self._compile_exclude_patterns(self._exclude_keywords)
        self._dirconf = {}
        self._categoryconf = {}
        self._monitor_modes = {}
        self._load_monitor_dirs()

        if not (self._enabled or self._onlyonce):
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._enabled:
            for source_dir in self._dirconf:
                if self._monitor_modes.get(source_dir, self._mode) == "nomonitor":
                    logger.info(f"{source_dir} 实时软链接服务已关闭")
                    continue
                self._scheduler.add_job(
                    func=self.start_monitor,
                    trigger="date",
                    run_date=datetime.datetime.now(
                        tz=pytz.timezone(settings.TZ)
                    )
                    + datetime.timedelta(seconds=3),
                    name=f"实时软连接 {source_dir}",
                    kwargs={"source_dir": source_dir},
                )

        if self._onlyonce:
            logger.info("实时软连接服务启动，立即运行一次")
            self._scheduler.add_job(
                name="实时软连接",
                func=self.sync_all,
                trigger="date",
                run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                + datetime.timedelta(seconds=3),
            )
            self._onlyonce = False
            self.__update_config()

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def _normalize_mode(value: Any) -> str:
        mode = str(value or "compatibility").strip().lower()
        return mode if mode in FileSoftLink._MONITOR_MODES else "compatibility"

    @staticmethod
    def _parse_nonnegative_float(value: Any, default: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= 0 else default

    @staticmethod
    def _parse_extensions(value: str) -> set[str]:
        extensions = set()
        for extension in str(value or "").split(","):
            normalized = extension.strip().lower()
            if not normalized:
                continue
            if not normalized.startswith("."):
                normalized = f".{normalized}"
            extensions.add(normalized)
        return extensions

    @staticmethod
    def _compile_exclude_patterns(value: str) -> list[re.Pattern[str]]:
        patterns = []
        for keyword in str(value or "").splitlines():
            keyword = keyword.strip()
            if not keyword:
                continue
            try:
                patterns.append(re.compile(keyword))
            except re.error as error:
                logger.warning(f"排除关键词 {keyword!r} 无效，已忽略：{error}")
        return patterns

    @staticmethod
    def _resolve_absolute_path(value: Any) -> Path:
        raw_value = str(value or "").strip()
        if not raw_value:
            raise ValueError("路径不能为空")
        path = Path(raw_value).expanduser()
        if not path.is_absolute():
            raise ValueError("路径必须是绝对路径")
        return path.resolve(strict=False)

    @staticmethod
    def _split_monitor_path(value: str) -> tuple[str, Optional[str]]:
        """按平台拆分 source:target，避免 Windows 盘符被误当分隔符。"""
        if SystemUtils.is_windows() and re.match(r"^[A-Za-z]:[\\/]", value):
            separator = value.find(":", 2)
            if separator < 0:
                return value, None
            return value[:separator], value[separator + 1 :]
        source, separator, target = value.partition(":")
        return source, target if separator else None

    def _load_monitor_dirs(self) -> None:
        """解析监控配置；任何无法证明边界的目录配置都不启用。"""
        for raw_line in self._monitor_dirs.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            monitor_mode = self._mode
            if line.count("$") == 1:
                line, custom_mode = line.rsplit("$", 1)
                monitor_mode = self._normalize_mode(custom_mode)

            categories = None
            if line.count("#") == 1:
                line, category_value = line.rsplit("#", 1)
                categories = [item.strip() for item in category_value.split(",") if item.strip()]

            source_value, target_value = self._split_monitor_path(line)
            try:
                source_path = self._resolve_absolute_path(source_value)
                target_path = (
                    self._resolve_absolute_path(target_value)
                    if target_value is not None and target_value.strip()
                    else None
                )
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning(f"跳过无效监控目录配置 {raw_line!r}：{error}")
                continue

            if target_path and (
                target_path == source_path or target_path.is_relative_to(source_path)
            ):
                logger.warning(f"{target_path} 是监控目录 {source_path} 的子目录，无法监控")
                self.systemmessage.put(
                    f"{target_path} 是下载目录 {source_path} 的子目录，无法监控"
                )
                continue

            source_key = str(source_path)
            self._dirconf[source_key] = target_path
            self._categoryconf[source_key] = categories
            self._monitor_modes[source_key] = monitor_mode

    def _source_entry(self, source_value: Any) -> tuple[Optional[str], Optional[Path]]:
        """返回配置中最具体的源目录，调用方不得用字符串前缀猜测边界。"""
        try:
            candidate = self._resolve_absolute_path(source_value)
        except (OSError, RuntimeError, ValueError):
            return None, None

        matches: list[tuple[str, Path]] = []
        for source_key in self._dirconf:
            try:
                source_path = self._resolve_absolute_path(source_key)
                if candidate == source_path or candidate.is_relative_to(source_path):
                    matches.append((source_key, source_path))
            except (OSError, RuntimeError, ValueError):
                continue
        if not matches:
            return None, None
        return max(matches, key=lambda item: len(item[1].parts))

    def _safe_target_file(
        self, source_file: Path, source_path: Path, target_path: Path
    ) -> Optional[Path]:
        """把源文件映射到目标目录，并拒绝符号链接导致的越界路径。"""
        try:
            relative_path = source_file.relative_to(source_path)
            target_file = target_path / relative_path
            resolved_target = target_file.resolve(strict=False)
            if not resolved_target.is_relative_to(target_path):
                logger.warning(f"拒绝越界目标路径：{target_file}")
                return None
            return target_file
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(f"无法验证目标路径 {source_file}：{error}")
            return None

    def start_monitor(self, source_dir: str) -> None:
        """启动一个目录监听器；源目录不存在时保持停止状态。"""
        source_key, source_path = self._source_entry(source_dir)
        if not source_key or source_path is None or not source_path.is_dir():
            logger.warning(f"监控目录不存在或不是目录：{source_dir}")
            return

        observer: Any = None
        try:
            monitor_mode = self._monitor_modes.get(source_key, self._mode)
            observer = (
                PollingObserver(timeout=10)
                if monitor_mode == "compatibility"
                else Observer(timeout=10)
            )
            observer.schedule(
                FileMonitorHandler(source_key, self),
                path=str(source_path),
                recursive=True,
            )
            observer.daemon = True
            observer.start()
            self._observer.append(observer)
            logger.info(f"{source_path} 的实时软链接服务启动")
        except Exception as error:
            if observer is not None:
                try:
                    observer.stop()
                except Exception:
                    pass
            message = str(error)
            if "inotify" in message and "reached" in message:
                logger.warning(f"云盘监控服务启动出现异常：{message}")
            else:
                logger.error(f"{source_path} 启动云盘监控失败：{message}")
            self.systemmessage.put(f"{source_path} 启动云盘监控失败：{message}")

    def __update_config(self) -> None:
        """保存规范化后的插件配置，确保一次性开关不会重复执行。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "copy_files": self._copy_files,
                "mode": self._mode,
                "monitor_dirs": self._monitor_dirs,
                "exclude_keywords": self._exclude_keywords,
                "cron": self._cron,
                "url": self._url,
                "force": self._force,
                "size": self._size,
                "sync_interval": self._sync_interval,
                "rmt_mediaext": ", ".join(sorted(self._media_extensions)),
            }
        )

    @staticmethod
    def _action_payload(event: Optional[Event]) -> Optional[PluginActionEventData]:
        """读取已按宿主事件合同校验的插件动作快照。"""
        if event is None:
            return None
        snapshot = event.snapshot()
        if not snapshot.valid or not isinstance(snapshot.payload, PluginActionEventData):
            logger.warning("插件动作事件 payload 校验失败，已忽略")
            return None
        return snapshot.payload

    @staticmethod
    def _action_extra(payload: PluginActionEventData, key: str) -> Any:
        return (payload.model_extra or {}).get(key)

    def _notify_action(self, payload: PluginActionEventData, title: str) -> None:
        if payload.user:
            self.post_message(channel=payload.channel, title=title, userid=payload.user)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Optional[Event] = None) -> None:
        """处理远程全量同步动作。"""
        payload = self._action_payload(event)
        if event is not None:
            if payload is None or payload.action != "softlink_sync":
                return
            self._notify_action(payload, "开始同步监控目录 ...")
        self.sync_all()
        if payload is not None:
            self._notify_action(payload, "监控目录同步完成！")

    @eventmanager.register(EventType.PluginAction)
    def softlink_file(self, event: Optional[Event] = None) -> None:
        """处理其它插件请求的单文件软链接动作。"""
        payload = self._action_payload(event)
        if payload is None or payload.action != "softlink_file":
            return
        file_path = self._action_extra(payload, "file_path")
        if not file_path:
            logger.error("软链接单文件动作缺少 file_path")
            return
        source_key, _ = self._source_entry(file_path)
        if not source_key:
            logger.error(f"未找到文件 {file_path} 对应的监控目录")
            return
        self._handle_file(str(file_path), source_key)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Optional[Event] = None) -> None:
        """处理远程定向同步动作，路径和分类均限制在已配置源目录内。"""
        payload = self._action_payload(event)
        if payload is None or payload.action not in {"softlink_one", "softlink_all"}:
            return
        args = self._action_extra(payload, "arg_str") or self._action_extra(payload, "args")
        if not str(args or "").strip():
            logger.error("定向软链接动作缺少参数")
            return

        all_args = str(args).strip()
        args_parts = all_args.split(maxsplit=1)
        category = args_parts[0] if len(args_parts) == 2 else None
        path_or_name = args_parts[1] if len(args_parts) == 2 else all_args
        limit = int(path_or_name) if category and path_or_name.isdigit() else None

        if category and limit is not None:
            category_path = Path(category).expanduser()
            if category_path.is_dir():
                source_key, source_path = self._source_entry(category_path)
                if source_key and source_path:
                    self._handle_limit(category_path, limit, source_key, payload)
                    return

        if category:
            for source_key, categories in self._categoryconf.items():
                if not categories or category not in categories:
                    continue
                source_path = Path(source_key)
                category_path = source_path / category
                if limit is not None:
                    self._handle_limit(category_path, limit, source_key, payload)
                else:
                    base_path = category_path / path_or_name
                    target_paths = self._find_related_paths(base_path, source_path)
                    if not target_paths:
                        logger.error(f"未查找到 {category} {path_or_name} 对应的具体目录")
                        self._notify_action(
                            payload,
                            f"未查找到 {category} {path_or_name} 对应的具体目录",
                        )
                        return
                    for target_path in target_paths:
                        self._process_directory(target_path, source_key)
                        self._notify_action(payload, f"{target_path} 软连接完成！")
                    if payload.action == "softlink_one":
                        return
            self._notify_action(payload, f"{all_args} 未检索到，请检查输入是否正确！")
            return

        source_key, source_path = self._source_entry(path_or_name)
        if source_key and source_path:
            requested_path = Path(path_or_name).expanduser().resolve(strict=False)
            if not requested_path.exists():
                logger.info(f"同步路径 {path_or_name} 不存在")
                return
            if requested_path.is_file():
                self._handle_file(str(requested_path), source_key)
            elif requested_path.is_dir() and requested_path.is_relative_to(source_path):
                self._process_directory(requested_path, source_key)
            else:
                logger.warning(f"拒绝监控目录之外的定向路径：{path_or_name}")
                return
            self._notify_action(payload, f"{all_args} 软连接完成！")
            return

        for source_key, categories in self._categoryconf.items():
            if not categories or all_args not in categories:
                continue
            category_path = Path(source_key) / all_args
            self._process_directory(category_path, source_key)
            self._notify_action(payload, f"{all_args} 软连接完成！")
            return
        self._notify_action(payload, f"{all_args} 未检索到，请检查输入是否正确！")

    def _handle_limit(
        self,
        path: Path,
        limit: int,
        source_key: str,
        payload: PluginActionEventData,
    ) -> None:
        source_path = Path(source_key)
        try:
            path = path.resolve(strict=True)
            if not path.is_relative_to(source_path) or not path.is_dir():
                logger.warning(f"拒绝目录之外的定向路径：{path}")
                return
            sub_paths = [
                item
                for item in path.iterdir()
                if item.is_dir() and not item.is_symlink()
            ]
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(f"无法读取定向目录 {path}：{error}")
            return

        sub_paths.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for sub_path in sub_paths[: max(limit, 0)]:
            self._process_directory(sub_path, source_key)
            self._notify_action(payload, f"{sub_path} 软连接完成！")

    @staticmethod
    def _find_related_paths(base_path: Path, source_path: Path) -> list[Path]:
        try:
            base_path = base_path.resolve(strict=False)
            if not base_path.is_relative_to(source_path):
                return []
            if not base_path.parent.is_dir():
                return []
            related = [
                item
                for item in base_path.parent.iterdir()
                if item.name.startswith(base_path.name)
                and item.is_dir()
                and not item.is_symlink()
                and item.resolve(strict=False).is_relative_to(source_path)
            ]
            return sorted(related, key=lambda item: item.stat().st_mtime, reverse=True)
        except (OSError, RuntimeError, ValueError):
            return []

    def _process_directory(self, path: Path, source_key: str) -> None:
        source_path = Path(source_key)
        try:
            path = path.resolve(strict=True)
            if not path.is_relative_to(source_path) or not path.is_dir():
                logger.warning(f"拒绝目录之外的定向路径：{path}")
                return
            files = [
                item
                for item in path.rglob("*")
                if item.is_file() and not item.is_symlink()
            ]
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(f"无法遍历定向目录 {path}：{error}")
            return
        for file_path in files:
            self._handle_file(str(file_path), source_key)
            if self._sync_interval:
                time.sleep(self._sync_interval)

    def sync_all(self) -> bool:
        """遍历所有已配置源目录并处理其中的文件。"""
        success = True
        logger.info("开始全量同步监控目录 ...")
        for source_key in tuple(self._dirconf):
            source_path = Path(source_key)
            if not source_path.is_dir():
                logger.warning(f"监控目录不存在或不是目录：{source_path}")
                success = False
                continue
            self._process_directory(source_path, source_key)
        logger.info("全量同步监控目录完成！")
        return success

    def event_handler(self, event: Any, mon_path: str, text: str, event_path: str) -> None:
        """忽略目录事件，只处理已配置源目录内的普通文件。"""
        if event.is_directory:
            return
        logger.debug(f"文件{text}：{event_path}")
        self._handle_file(event_path=event_path, mon_path=mon_path)

    @staticmethod
    def _is_hidden_or_recycled(path: Path) -> bool:
        for part in path.parts:
            lowered = part.casefold()
            if lowered in {"@recycle", "#recycle", "@eadir"} or (
                part.startswith(".") and part not in {".", ".."}
            ):
                return True
        return False

    def _matches_exclusion(self, path: Path) -> bool:
        path_value = str(path)
        for pattern in self._exclude_patterns:
            if pattern.search(path_value):
                logger.info(f"{path} 命中过滤关键字 {pattern.pattern}，不处理")
                return True
        transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords) or []
        for keyword in transfer_exclude_words:
            if not keyword:
                continue
            try:
                if re.search(str(keyword), path_value, re.IGNORECASE):
                    logger.info(f"{path} 命中整理屏蔽词 {keyword}，不处理")
                    return True
            except re.error:
                logger.warning(f"整理屏蔽词 {keyword!r} 无效，已忽略")
        return False

    def _handle_file(self, event_path: str, mon_path: str) -> None:
        """在源目录边界内创建软链接或复制文件，所有失败均停止写入。"""
        source_key, source_path = self._source_entry(mon_path)
        if not source_key or source_path is None:
            logger.warning(f"未找到文件 {event_path} 对应的监控目录")
            return
        target_path = self._dirconf.get(source_key)
        if target_path is None:
            logger.info(f"{source_key} 没有配置转移目的目录，不处理")
            return

        try:
            raw_path = Path(event_path).expanduser()
            if raw_path.is_symlink() or not raw_path.is_file():
                return
            file_path = raw_path.resolve(strict=True)
            source_path = source_path.resolve(strict=True)
            if not file_path.is_relative_to(source_path):
                logger.warning(f"拒绝监控目录之外的文件：{event_path}")
                return
            if self._is_hidden_or_recycled(raw_path):
                logger.debug(f"{event_path} 是回收站或隐藏的文件")
                return
            if self._matches_exclusion(raw_path):
                return
            if self._size > 0 and file_path.stat().st_size < self._size * 1024**3:
                logger.info(f"{file_path} 文件大小小于监控文件大小，不处理")
                return
            target_file = self._safe_target_file(file_path, source_path, target_path)
            if target_file is None:
                return

            with self._lock:
                target_file = self._safe_target_file(file_path, source_path, target_path)
                if target_file is None:
                    return
                if target_file.exists() or target_file.is_symlink():
                    if target_file.is_dir() and not target_file.is_symlink():
                        logger.warning(f"目标路径是目录，拒绝覆盖：{target_file}")
                        return
                    if not self._force:
                        logger.info(f"目标文件 {target_file} 已存在")
                        return
                    target_file.unlink()

                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file = self._safe_target_file(file_path, source_path, target_path)
                if target_file is None:
                    return

                if file_path.suffix.lower() in self._media_extensions:
                    code, message = SystemUtils.softlink(file_path, target_file)
                    if code != 0:
                        logger.error(
                            f"创建媒体文件软连接失败 {file_path} -> {target_file}：{message}"
                        )
                        return
                    logger.info(f"创建媒体文件软连接 {file_path} 到 {target_file}")
                    if self._url:
                        self._notify_indexer(file_path)
                elif self._copy_files:
                    code, message = SystemUtils.copy(file_path, target_file)
                    if code != 0:
                        logger.error(f"复制文件失败 {file_path} -> {target_file}：{message}")
                        return
                    logger.info(f"复制其他文件 {file_path} 到 {target_file}")
        except (OSError, RuntimeError, ValueError) as error:
            logger.error(f"软连接发生错误：{event_path} - {error}")

    def _notify_indexer(self, file_path: Path) -> None:
        """从监听线程调用异步 SDK，避免使用同步网络客户端阻塞宿主事件循环。"""
        coroutine = self._notify_indexer_async(file_path)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(coroutine)
            except Exception as error:
                logger.warning(f"任务推送失败：{error}")
        else:
            asyncio.create_task(coroutine)

    async def _notify_indexer_async(self, file_path: Path) -> None:
        try:
            result = await AsyncRequestUtils(
                content_type="application/json"
            ).post_json(
                url=self._url,
                json={"path": str(file_path), "type": "add"},
            )
            if result is None:
                logger.warning(f"任务推送未收到有效响应：{self._url}")
        except Exception as error:
            logger.warning(f"任务推送失败：{error}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """声明远程同步命令。"""
        return [
            {
                "cmd": "/softlink_sync",
                "event": EventType.PluginAction,
                "desc": "文件软连接同步",
                "category": "",
                "data": {"action": "softlink_sync"},
            },
            {
                "cmd": "/soft",
                "event": EventType.PluginAction,
                "desc": "定向软连接处理",
                "category": "",
                "data": {"action": "softlink_one"},
            },
            {
                "cmd": "/softall",
                "event": EventType.PluginAction,
                "desc": "定向软连接处理",
                "category": "",
                "data": {"action": "softlink_all"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/softlink_sync",
                "endpoint": self.sync,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "实时软连接同步",
                "description": "实时软连接同步",
                "response_model": schemas.Response[None],
            }
        ]

    async def sync(self) -> schemas.Response[None]:
        """异步 API 入口，将阻塞的本地遍历移出宿主事件循环。"""
        success = await asyncio.to_thread(self.sync_all)
        return schemas.Response(
            success=success,
            message="监控目录同步完成" if success else "部分监控目录同步失败",
        )

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
            except (TypeError, ValueError) as error:
                logger.warning(f"定时全量同步周期配置错误：{error}")
                return []
            return [
                {
                    "id": "FileSoftLink",
                    "name": "实时软连接全量同步服务",
                    "trigger": trigger,
                    "func": self.sync_all,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页面和默认值，字段名保持旧版本配置兼容。"""
        fields = [
            ("VSwitch", "enabled", "启用插件"),
            ("VSwitch", "onlyonce", "立即运行一次"),
            ("VSwitch", "copy_files", "复制非媒体文件"),
            ("VSwitch", "force", "强制覆盖"),
            ("VSelect", "mode", "监控模式"),
            ("VCronField", "cron", "定时全量同步周期"),
            ("VTextField", "size", "监控文件大小（GB）"),
            ("VTextField", "sync_interval", "同步遍历文件间隔（s）"),
            ("VTextarea", "monitor_dirs", "监控目录"),
            ("VTextarea", "exclude_keywords", "排除关键词"),
            ("VTextarea", "rmt_mediaext", "视频格式"),
            ("VTextField", "url", "任务推送url"),
        ]
        content = []
        for component, model, label in fields:
            props: Dict[str, Any] = {"model": model, "label": label}
            if component == "VSelect":
                props["items"] = [
                    {"title": "兼容模式", "value": "compatibility"},
                    {"title": "性能模式", "value": "fast"},
                    {"title": "不监控", "value": "nomonitor"},
                ]
            elif component in {"VTextarea", "VCronField"}:
                props["rows"] = 5 if model == "monitor_dirs" else 2
            content.append(
                {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 4},
                    "content": [{"component": component, "props": props}],
                }
            )
        return [
            {
                "component": "VForm",
                "content": [{"component": "VRow", "content": content}],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "copy_files": True,
            "force": False,
            "mode": "compatibility",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "cron": "",
            "size": 0,
            "sync_interval": 0,
            "url": "",
            "rmt_mediaext": self._DEFAULT_MEDIA_EXTENSIONS,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self) -> None:
        """停止监听器和插件私有调度器，供热加载与卸载复用。"""
        remaining_observers = []
        for observer in self._observer:
            try:
                observer.stop()
                observer.join(timeout=10)
                if observer.is_alive():
                    logger.warning("实时软链接监听器未在超时内停止")
                    remaining_observers.append(observer)
            except Exception as error:
                logger.warning(f"停止实时软链接监听器失败：{error}")
                remaining_observers.append(observer)
        self._observer = remaining_observers

        scheduler = self._scheduler
        if scheduler is not None:
            try:
                scheduler.remove_all_jobs()
                if scheduler.running:
                    scheduler.shutdown(wait=False)
            except Exception as error:
                logger.warning(f"停止实时软链接调度器失败：{error}")
            else:
                self._scheduler = None
