from __future__ import annotations

import datetime
import random
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.utilities import SystemUtils


class FileCopy(_PluginBase):
    """按目录映射复制指定扩展名文件，并由宿主统一管理定时任务。"""

    # 插件市场元数据和宿主加载约束。
    plugin_name = "文件复制"
    plugin_desc = "自定义文件类型从源目录复制到目的目录。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/copy_files.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "filecopy_"
    plugin_order = 30
    auth_level = 1

    _DEFAULT_EXTENSIONS = ".nfo, .jpg"

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cron = ""
        self._delay = "20,1-10"
        self._monitor_dirs = ""
        self._rmt_mediaext = self._DEFAULT_EXTENSIONS
        self._dirconf: Dict[str, Path] = {}
        self._run_lock = threading.Lock()
        self._startup_pending = False

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建目录映射；定时任务由宿主调度器注册。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = str(config.get("cron") or "").strip()
        self._delay = str(config.get("delay") or "20,1-10").strip()
        self._monitor_dirs = str(config.get("monitor_dirs") or "")
        self._rmt_mediaext = str(
            config.get("rmt_mediaext") or self._DEFAULT_EXTENSIONS
        )
        self._dirconf = self._parse_monitor_dirs(self._monitor_dirs)
        self._startup_pending = (
            self._enabled and bool(self._dirconf) and not self._onlyonce
        )

    @staticmethod
    def _split_monitor_dir(value: str) -> Tuple[str, str]:
        """按平台拆分 source:target，避免 Windows 盘符吞掉分隔符。"""
        if SystemUtils.is_windows() and len(value) >= 2 and value[1] == ":":
            separator = value.find(":", 2)
            if separator < 0:
                return value, ""
            return value[:separator], value[separator + 1 :]
        source, separator, target = value.partition(":")
        return source, target if separator else ""

    @staticmethod
    def _resolve_path(value: str) -> Path:
        """规范化目录路径，确保相对路径比较不受当前工作目录影响。"""
        return Path(value.strip()).expanduser().resolve(strict=False)

    def _parse_monitor_dirs(self, value: str) -> Dict[str, Path]:
        """解析目录映射；缺少源目录或目标目录的配置按无效处理。"""
        mappings: Dict[str, Path] = {}
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            source_value, target_value = self._split_monitor_dir(line)
            if not source_value.strip() or not target_value.strip():
                logger.warning(f"跳过缺少源目录或目标目录的配置：{raw_line!r}")
                continue
            try:
                source_path = self._resolve_path(source_value)
                target_path = self._resolve_path(target_value)
            except (OSError, RuntimeError, ValueError) as error:
                logger.warning(f"跳过无效目录配置 {raw_line!r}：{error}")
                continue
            if target_path == source_path or target_path.is_relative_to(source_path):
                logger.warning(f"跳过源目录内部的目标目录配置：{raw_line!r}")
                continue
            mappings[str(source_path)] = target_path
        return mappings

    @staticmethod
    def _parse_extensions(value: Any) -> List[str]:
        """将扩展名配置整理为宿主文件扫描工具可接受的列表。"""
        extensions = [item.strip() for item in str(value or "").split(",") if item.strip()]
        return extensions or [item.strip() for item in FileCopy._DEFAULT_EXTENSIONS.split(",")]

    @staticmethod
    def _parse_delay(value: Any) -> Optional[Tuple[int, int, int]]:
        """解析 ``次数,秒数`` 或 ``次数,最小秒数-最大秒数``，坏值不阻断复制。"""
        text = str(value or "").strip()
        parts = [part.strip() for part in text.split(",")]
        if len(parts) != 2:
            return None
        try:
            batch_size = int(parts[0])
        except (TypeError, ValueError):
            return None
        if batch_size <= 0:
            return None

        delay_value = parts[1]
        if "-" in delay_value:
            range_parts = [part.strip() for part in delay_value.split("-")]
            if len(range_parts) != 2:
                return None
            try:
                minimum, maximum = (int(part) for part in range_parts)
            except (TypeError, ValueError):
                return None
        else:
            try:
                minimum = maximum = int(delay_value)
            except (TypeError, ValueError):
                return None
        if minimum < 0 or maximum < minimum:
            return None
        return batch_size, minimum, maximum

    @staticmethod
    def _map_target_path(
        source_dir: Path | str, target_dir: Path | str, file_path: Path | str
    ) -> Optional[Path]:
        """使用相对路径映射文件，拒绝源目录前缀相同但不在其下的路径。"""
        if not target_dir:
            logger.warning(f"拒绝空目标目录：{file_path}")
            return None
        try:
            source_root = Path(source_dir).resolve(strict=False)
            relative_path = Path(file_path).resolve(strict=False).relative_to(source_root)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(f"拒绝源目录之外的文件 {file_path}：{error}")
            return None
        if not relative_path or relative_path == Path("."):
            logger.warning(f"拒绝映射目录本身：{file_path}")
            return None

        target_root = Path(target_dir).resolve(strict=False)
        target_path = target_root / relative_path
        try:
            target_path.parent.resolve(strict=False).relative_to(target_root)
        except (OSError, RuntimeError, ValueError) as error:
            logger.warning(f"拒绝目标目录之外的路径 {target_path}：{error}")
            return None
        return target_path

    @staticmethod
    def _target_exists(path: Path) -> bool:
        """把损坏的符号链接也视为已存在，避免复制时覆盖既有目标。"""
        return path.exists() or path.is_symlink()

    def _copy_one(self, source_path: Path, target_path: Path) -> Optional[bool]:
        """复制一个文件；返回 ``None`` 表示目标已存在而未尝试复制。"""
        if self._target_exists(target_path):
            logger.info(f"{target_path} 文件已存在，跳过")
            return None

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            result = SystemUtils.copy(source_path, target_path)
        except Exception as error:
            logger.error(f"{source_path} -> {target_path} 复制失败：{error}")
            return False

        if isinstance(result, tuple):
            state = result[0] if result else -1
            error_message = result[1] if len(result) > 1 else ""
        else:
            state = result
            error_message = ""
        success = state == 0
        if success:
            logger.info(f"{source_path} -> {target_path} 复制成功")
            return True
        logger.error(
            f"{source_path} -> {target_path} 复制失败：{error_message or '宿主复制工具返回失败'}"
        )
        return False

    def _consume_once(self) -> bool:
        """在任务开始时持久化消费一次性请求，失败时保留待执行状态。"""
        if not self._onlyonce:
            return False
        self._onlyonce = False
        try:
            saved = self.__update_config()
        except Exception as error:
            saved = False
            logger.error(f"保存一次性执行状态失败：{error}")
        if not saved:
            self._onlyonce = True
            logger.error("一次性文件复制状态未能保存，本次任务未执行")
            return False
        return True

    def copy_files(self, once: bool = False) -> bool:
        """全量复制监控目录；同一插件实例的重叠任务只允许一个执行。"""
        if not self._run_lock.acquire(blocking=once):
            logger.info("文件复制任务已在运行，跳过本次执行")
            return False

        try:
            if self._startup_pending:
                self._startup_pending = False
            if once:
                if not self._consume_once():
                    return False
                logger.info("文件复制服务启动，立即运行一次")
            logger.info("开始全量复制监控目录 ...")
            delay = self._parse_delay(self._delay)
            all_successful = True
            extensions = self._parse_extensions(self._rmt_mediaext)
            for source_dir, target_dir in self._dirconf.items():
                processed = 0
                files = SystemUtils.list_files(Path(source_dir), extensions)
                for source_path in files:
                    target_path = self._map_target_path(
                        source_dir, target_dir, Path(source_path)
                    )
                    if target_path is None:
                        all_successful = False
                    else:
                        copy_result = self._copy_one(Path(source_path), target_path)
                        if copy_result is not None:
                            if not copy_result:
                                all_successful = False
                            processed += 1
                            if delay and processed >= delay[0]:
                                wait_time = random.randint(delay[1], delay[2])
                                logger.info(f"延迟 {wait_time} 秒")
                                if wait_time:
                                    time.sleep(wait_time)
                                processed = 0
            logger.info("全量复制监控目录完成！")
            return all_successful
        except Exception as error:
            logger.error(f"全量复制监控目录失败：{error}")
            return False
        finally:
            self._run_lock.release()

    def __update_config(self) -> bool:
        """持久化插件配置，保留表单字段名称与旧版本兼容。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "monitor_dirs": self._monitor_dirs,
                "cron": self._cron,
                "delay": self._delay,
                "rmt_mediaext": self._rmt_mediaext,
            }
        )

    def _run_scheduled_copy(self) -> Tuple[bool, str]:
        """把复制结果转换为宿主调度器识别的标准成功或失败返回值。"""
        if self.copy_files():
            return True, ""
        return False, "文件复制任务执行失败"

    def _run_once_copy(self) -> Tuple[bool, str]:
        """可靠串行执行一次性请求，并返回宿主可识别的失败语义。"""
        if self.copy_files(once=True):
            return True, ""
        return False, "一次性文件复制任务执行失败"

    def get_state(self) -> bool:
        """只要插件启用或有待执行的一次性任务，就允许宿主投影能力。"""
        return self._enabled or self._onlyonce

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """向宿主声明周期和一次性任务，避免插件自行创建调度器。"""
        services: List[Dict[str, Any]] = []
        if self._startup_pending:
            services.append(
                {
                    "id": "FileCopyStartup",
                    "name": "文件复制",
                    "trigger": "date",
                    "func": self._run_scheduled_copy,
                    "kwargs": {
                        "run_date": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        )
                        + datetime.timedelta(seconds=3)
                    },
                }
            )
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron, timezone=settings.TZ)
            except (TypeError, ValueError) as error:
                logger.warning(f"定时全量复制周期配置错误：{error}")
            else:
                services.append(
                    {
                        "id": "FileCopy",
                        "name": "文件复制",
                        "trigger": trigger,
                        "func": self._run_scheduled_copy,
                        "kwargs": {},
                    }
                )

        if self._onlyonce:
            services.append(
                {
                    "id": "FileCopyOnce",
                    "name": "文件复制",
                    "trigger": "date",
                    "func": self._run_once_copy,
                    "kwargs": {
                        "run_date": datetime.datetime.now(
                            tz=pytz.timezone(settings.TZ)
                        )
                        + datetime.timedelta(seconds=3)
                    },
                }
            )
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时全量同步周期",
                                            "placeholder": "5位cron表达式，留空关闭",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay",
                                            "label": "随机延时",
                                            "placeholder": "20,1-10  处理10个文件后随机延迟1-10秒",
                                        },
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "monitor_dirs",
                                            "label": "监控目录",
                                            "rows": 5,
                                            "placeholder": "监控目录:转移目的目录",
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rmt_mediaext",
                                            "label": "文件格式",
                                            "rows": 2,
                                            "placeholder": ".nfo, .jpg",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "monitor_dirs": "",
            "cron": "",
            "delay": "20,1-10",
            "rmt_mediaext": self._DEFAULT_EXTENSIONS,
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self) -> None:
        """保留宿主生命周期接口；插件不持有私有调度资源。"""
        return None
