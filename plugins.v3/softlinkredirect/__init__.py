"""安全重写软链接目标，不修改其指向的文件内容。"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger


class SoftLinkRedirect(_PluginBase):
    """将配置目录中的软链接来源路径安全地重写为目标路径。"""

    plugin_name = "软连接重定向"
    plugin_desc = "重定向软连接指向。"
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/softlinkredirect.png"
    )
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "softlinkredirect_"
    plugin_order = 9
    auth_level = 2

    def __init__(self) -> None:
        """初始化配置状态和单实例任务互斥门禁。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._run_once = False
        self._cron: Optional[str] = None
        self._soft_path = ""
        self._origin_path = ""
        self._redirect_path = ""
        self._run_lock = threading.Lock()
        self._run_once_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置，并将一次性执行交给宿主服务调度。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._cron = self._normalize_cron(config.get("cron"))
        self._soft_path = str(config.get("soft_path") or "").strip()
        self._origin_path = str(config.get("origin_path") or "").strip()
        self._redirect_path = str(config.get("redirect_path") or "").strip()

    @staticmethod
    def _normalize_cron(value: Any) -> Optional[str]:
        """规范化可选的五段 Cron 文本。"""
        normalized = " ".join(str(value or "").split())
        return normalized or None

    def _save_config(self) -> bool:
        """保存归一化配置，并持久化已消费的一次性开关。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron or "",
                "soft_path": self._soft_path,
                "origin_path": self._origin_path,
                "redirect_path": self._redirect_path,
            }
        )

    @staticmethod
    def _path_parts(path: str) -> tuple[str, ...]:
        """返回不依赖文件是否存在的规范化路径组件。"""
        return Path(os.path.normpath(path)).parts

    @staticmethod
    def _absolute_path(path: str | Path) -> Path:
        """规范化路径但不解析符号链接，保留断链的可处理性。"""
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _has_component_prefix(
        path_parts: tuple[str, ...], prefix_parts: tuple[str, ...]
    ) -> bool:
        """按完整路径组件判断前缀，避免 ``src`` 命中 ``src-old``。"""
        return bool(prefix_parts) and path_parts[: len(prefix_parts)] == prefix_parts

    @staticmethod
    def _suffix(path_parts: tuple[str, ...], prefix_parts: tuple[str, ...]) -> str:
        """把已匹配前缀后的组件拼接为平台路径。"""
        return (
            os.path.join(*path_parts[len(prefix_parts) :])
            if len(path_parts) > len(prefix_parts)
            else ""
        )

    @classmethod
    def _replacement_target(
        cls,
        link_path: Path,
        current_target: str,
        origin_path: str,
        redirect_path: str,
    ) -> Optional[str]:
        """计算新链接文本；相对链接优先保持原有相对路径语义。"""
        target_text = os.path.normpath(current_target)
        origin_text = os.path.normpath(origin_path)
        redirect_text = os.path.normpath(redirect_path)
        target_parts = cls._path_parts(target_text)
        origin_parts = cls._path_parts(origin_text)

        if cls._has_component_prefix(target_parts, origin_parts):
            suffix = cls._suffix(target_parts, origin_parts)
            return os.path.normpath(
                os.path.join(redirect_text, suffix) if suffix else redirect_text
            )

        target_absolute = cls._absolute_path(
            target_text
            if Path(target_text).is_absolute()
            else link_path.parent / target_text
        )
        if Path(origin_text).is_absolute():
            origin_candidates = [cls._absolute_path(origin_text)]
        else:
            # 配置通常相对当前工作目录；同时接受相对链接目录的旧式配置。
            origin_candidates = [
                cls._absolute_path(origin_text),
                cls._absolute_path(link_path.parent / origin_text),
            ]

        for origin_absolute in origin_candidates:
            origin_parts_absolute = origin_absolute.parts
            if not cls._has_component_prefix(target_absolute.parts, origin_parts_absolute):
                continue
            suffix = cls._suffix(target_absolute.parts, origin_parts_absolute)
            redirect_absolute = cls._absolute_path(redirect_text)
            rewritten_absolute = os.path.join(
                os.fspath(redirect_absolute), suffix
            ) if suffix else os.fspath(redirect_absolute)
            if Path(target_text).is_absolute():
                return os.path.normpath(rewritten_absolute)
            return os.path.normpath(
                os.path.relpath(rewritten_absolute, start=link_path.parent)
            )
        return None

    @staticmethod
    def _link_without_replacement(source: Path, target: Path) -> bool:
        """创建不覆盖既有目录项的硬链接，作为条件安装的文件系统原语。"""
        try:
            # POSIX 系统的 link(2) 在目标存在时以 EEXIST 失败，不会覆盖目标。
            os.link(source, target, follow_symlinks=False)
        except FileExistsError:
            return False
        except (NotImplementedError, OSError):
            if os.name != "nt":
                raise
            # Windows 的 os.rename 在目标存在时失败，可作为无覆盖回退。
            try:
                os.rename(source, target)
            except FileExistsError:
                return False
            return True
        return True

    @classmethod
    def _restore_displaced(cls, displaced_path: Path, link_path: Path) -> bool:
        """在目标空闲时无覆盖恢复被移出的目录项，失败则保留备份。"""
        try:
            if not cls._link_without_replacement(displaced_path, link_path):
                return False
            os.unlink(displaced_path)
            return True
        except OSError as error:
            logger.warning(f"恢复软链接事务对象失败，保留备份：{displaced_path}：{error}")
            return False

    @classmethod
    def _replace_symlink(
        cls,
        link_path: Path,
        current_target: str,
        new_target: str,
        original_stat: os.stat_result,
    ) -> bool:
        """以移出、条件安装和恢复事务替换软链接，避免覆盖竞态对象。"""
        temporary_path = link_path.with_name(
            f".{link_path.name}.softlinkredirect-{uuid4().hex}.tmp"
        )
        displaced_path = link_path.with_name(
            f".{link_path.name}.softlinkredirect-{uuid4().hex}.old"
        )
        installed = False
        try:
            os.symlink(new_target, temporary_path)
            current_stat = os.lstat(link_path)
            if (current_stat.st_dev, current_stat.st_ino) != (
                original_stat.st_dev,
                original_stat.st_ino,
            ) or os.readlink(link_path) != current_target:
                logger.warning(f"软链接在替换前已变化，跳过：{link_path}")
                return False

            # 先把当前目录项移到同目录备份。rename 不会覆盖随机生成的备份名，
            # 即使最后一次校验后目标被替换为普通文件，也只会移动并保留该对象。
            os.rename(link_path, displaced_path)
            displaced_stat: Optional[os.stat_result] = None
            displaced_target: Optional[str] = None
            try:
                displaced_stat = os.lstat(displaced_path)
                displaced_target = os.readlink(displaced_path)
            except OSError:
                # 非软链接或已被并发删除的事务对象均按变化处理，并保留备份。
                pass

            if (
                displaced_stat is None
                or (displaced_stat.st_dev, displaced_stat.st_ino)
                != (original_stat.st_dev, original_stat.st_ino)
                or displaced_target != current_target
            ):
                if not cls._restore_displaced(displaced_path, link_path):
                    logger.warning(f"软链接替换对象已变化，备份保留：{displaced_path}")
                logger.warning(f"软链接在替换过程中已变化，跳过：{link_path}")
                return False

            # link(2) 的不覆盖语义保护移出后重新出现的对象；若无法安装，
            # 恢复原链接，目标被占用时则留下备份而不删除任何对象。
            if not cls._link_without_replacement(temporary_path, link_path):
                if not cls._restore_displaced(displaced_path, link_path):
                    logger.warning(f"软链接替换失败对象已保留：{displaced_path}")
                logger.warning(f"软链接目标在替换过程中已被占用，跳过：{link_path}")
                return False
            installed = True
            try:
                os.unlink(displaced_path)
            except OSError as error:
                # 新链接已安全安装；旧链接保留为可恢复的事务残留，不影响结果。
                logger.warning(f"清理软链接事务备份失败：{displaced_path}：{error}")
            return True
        except OSError as error:
            if not installed and os.path.lexists(displaced_path):
                if not cls._restore_displaced(displaced_path, link_path):
                    logger.warning(f"软链接替换失败对象已保留：{displaced_path}")
            logger.error(f"替换软链接失败：{link_path} -> {new_target}：{error}")
            return False
        finally:
            try:
                if os.path.lexists(temporary_path):
                    os.unlink(temporary_path)
            except OSError as error:
                logger.warning(f"清理软链接临时文件失败：{temporary_path}：{error}")

    @classmethod
    def update_symlink(
        cls,
        target_from: str,
        target_to: str,
        directory: str | Path,
    ) -> int:
        """扫描目录内的真实软链接并返回成功重定向数量。"""
        origin_path = str(target_from or "").strip()
        redirect_path = str(target_to or "").strip()
        if not origin_path or not redirect_path:
            logger.warning("软链接重定向路径未配置，跳过本轮任务")
            return 0

        root_path = Path(directory).absolute()
        if root_path.is_symlink():
            logger.error(f"软链接扫描目录不能是软链接，跳过：{root_path}")
            return 0
        if not root_path.is_dir():
            logger.warning(f"软链接扫描目录不存在或不是目录，跳过：{root_path}")
            return 0

        updated_count = 0
        for current_root, dirs, files in os.walk(
            root_path, topdown=True, followlinks=False
        ):
            current_root_path = Path(current_root)
            symlink_dirs = [
                name for name in dirs if (current_root_path / name).is_symlink()
            ]
            # 不进入软链接目录，但仍检查目录项本身是否需要重定向。
            dirs[:] = [name for name in dirs if name not in symlink_dirs]
            for name in (*symlink_dirs, *files):
                link_path = current_root_path / name
                try:
                    if not link_path.is_symlink():
                        continue
                    original_stat = os.lstat(link_path)
                    current_target = os.readlink(link_path)
                except OSError as error:
                    logger.warning(f"读取软链接失败，跳过：{link_path}：{error}")
                    continue

                target_path = (
                    Path(current_target)
                    if Path(current_target).is_absolute()
                    else link_path.parent / current_target
                )
                if not os.path.lexists(target_path):
                    logger.warning(
                        f"发现断链，仍按目标文本检查：{link_path} -> {current_target}"
                    )

                new_target = cls._replacement_target(
                    link_path, current_target, origin_path, redirect_path
                )
                if new_target is None or new_target == current_target:
                    continue
                if cls._replace_symlink(
                    link_path, current_target, new_target, original_stat
                ):
                    updated_count += 1
                    logger.info(f"软链接已重定向：{link_path} -> {new_target}")
        return updated_count

    def redirect(self) -> int:
        """以 single-flight 门禁执行一轮软链接重定向。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("软链接重定向任务正在运行，本次触发已跳过")
            return 0
        try:
            if not self._soft_path or not self._origin_path or not self._redirect_path:
                logger.warning("软链接重定向配置不完整，跳过本轮任务")
                return 0
            return self.update_symlink(
                self._origin_path,
                self._redirect_path,
                self._soft_path,
            )
        finally:
            self._run_lock.release()

    def _run_once_redirect(self) -> int:
        """消费一次性服务标记后执行一轮重定向。"""
        with self._run_once_lock:
            if not self._run_once:
                return 0
            if not self._soft_path or not self._origin_path or not self._redirect_path:
                logger.warning("软链接重定向配置不完整，保留一次性任务")
                return 0

            # 只有状态成功写回后才消费内存标记，避免持久化失败时本次执行和重启行为分叉。
            previous_onlyonce = self._onlyonce
            self._onlyonce = False
            try:
                saved = self._save_config()
            except Exception as error:  # noqa: BLE001 - 状态写回失败不能重复进入当前实例
                self._onlyonce = previous_onlyonce
                logger.error(f"保存一次性重定向状态失败：{error}")
                return 0
            if not saved:
                self._onlyonce = previous_onlyonce
                logger.error("保存一次性重定向状态失败，保留一次性任务")
                return 0
            self._run_once = False
        return self.redirect()

    def get_state(self) -> bool:
        """返回插件是否启用或仍有待执行的一次性任务。"""
        with self._run_once_lock:
            return bool(self._enabled or self._run_once)

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不暴露额外 HTTP API。"""
        return []

    @staticmethod
    def _timezone() -> timezone | ZoneInfo:
        """返回调度器使用的宿主时区，配置异常时回退 UTC。"""
        try:
            return ZoneInfo(str(settings.TZ))
        except (KeyError, TypeError, ValueError):
            logger.warning("宿主时区配置无效，软链接服务回退 UTC")
            return timezone.utc

    def get_service(self) -> list[dict[str, Any]]:
        """注册由宿主统一调度的一次性和 Cron 服务。"""
        services: list[dict[str, Any]] = []
        with self._run_once_lock:
            run_once = self._run_once
        if run_once:
            services.append(
                {
                    "id": "SoftLinkRedirect.Once",
                    "name": "软链接重定向（立即运行）",
                    "trigger": "date",
                    "func": self._run_once_redirect,
                    "kwargs": {
                        "run_date": datetime.now(self._timezone())
                        + timedelta(seconds=3)
                    },
                }
            )

        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(
                    self._cron, timezone=self._timezone()
                )
            except (TypeError, ValueError) as error:
                message = f"软链接重定向 Cron 配置错误：{error}"
                logger.error(message)
                self.systemmessage.put(message)
            else:
                services.append(
                    {
                        "id": "SoftLinkRedirect",
                        "name": "软链接重定向服务",
                        "trigger": trigger,
                        "func": self.redirect,
                        "kwargs": {},
                    }
                )
        return services

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回 V3 Vuetify 配置表单和默认值。"""
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，留空不启用",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    *[
                        {
                            "component": "VRow",
                            "content": [
                                {
                                    "component": "VCol",
                                    "props": {"cols": 12},
                                    "content": [
                                        {
                                            "component": "VTextField",
                                            "props": {
                                                "model": model,
                                                "label": label,
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                        for model, label in (
                            ("soft_path", "软连接路径"),
                            ("origin_path", "原来源文件路径"),
                            ("redirect_path", "重定向源文件路径"),
                        )
                    ],
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
                                            "text": "软连接指向由原来源路径改为重定向路径。",
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
            "cron": "",
            "soft_path": "",
            "origin_path": "",
            "redirect_path": "",
        }

    def get_page(self) -> list[dict]:
        """本插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """本插件不持有宿主调度器之外的服务资源。"""
        return None
