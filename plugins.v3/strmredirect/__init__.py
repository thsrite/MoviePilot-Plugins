from __future__ import annotations

import os
import re
import stat
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from app.plugins import _PluginBase
from app.sdk.logging import logger


class StrmRedirect(_PluginBase):
    """按配置重写普通 STRM 文件内容，并把一次性执行交给宿主调度器。"""

    plugin_name = "Strm重定向"
    plugin_desc = "重写Strm文件内容。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/softlinkredirect.png"
    plugin_version = "2.0.1"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "strmredirect_"
    plugin_order = 27
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._run_lock = threading.Lock()
        self._reset_config()

    def _reset_config(self) -> None:
        """恢复稳定默认配置，避免热重载沿用上一实例的执行开关。"""
        self._onlyonce = False
        self._unquote = False
        self._strm_path = ""
        self._origin_path = ""
        self._redirect_path = ""

    def init_plugin(self, config: dict = None):
        """读取配置；实际扫描由宿主通过 ``get_service`` 调度。"""
        self._reset_config()
        config = config or {}
        self._onlyonce = bool(config.get("onlyonce"))
        self._unquote = bool(config.get("unquote"))
        self._strm_path = str(config.get("strm_path") or "")
        self._origin_path = str(config.get("origin_path") or "")
        self._redirect_path = str(config.get("redirect_path") or "")

    def _can_run(self) -> bool:
        """判断配置是否包含目录及至少一种有效变换。"""
        return bool(
            self._strm_path
            and (self._unquote or (self._origin_path and self._redirect_path))
        )

    def _save_config(self) -> bool:
        """持久化一次性开关，并向调用方报告是否成功。"""
        try:
            return bool(
                self.update_config(
                    {
                        "onlyonce": self._onlyonce,
                        "unquote": self._unquote,
                        "strm_path": self._strm_path,
                        "origin_path": self._origin_path,
                        "redirect_path": self._redirect_path,
                    }
                )
            )
        except Exception as error:
            logger.error(f"保存 Strm重定向配置失败：{error}")
            return False

    def _run_service(self) -> int:
        """执行一次宿主调度任务，并在锁内消费一次性执行标记。"""
        if not self._run_lock.acquire(blocking=False):
            logger.info("Strm重定向任务正在运行，跳过重复调度")
            return 0

        try:
            if not self._onlyonce:
                logger.info("Strm重定向一次性任务已消费，跳过重复调度")
                return 0

            if not self._can_run():
                return 0

            self._onlyonce = False
            if not self._save_config():
                # 持久化失败时不执行任务，保留标记以便下一次调度重试；否则重载后会重复执行。
                self._onlyonce = True
                logger.error("Strm重定向一次性标记保存失败，本次任务未执行")
                return 0

            return self._update_strm(
                self._origin_path,
                self._redirect_path,
                self._strm_path,
            )
        finally:
            self._run_lock.release()

    def update_strm(
        self,
        target_from: str = "",
        target_to: str = "",
        directory: str | Path = "",
    ) -> int:
        """扫描目录并重写文件，直接调用也遵守 single-flight。"""
        if not self._run_lock.acquire(blocking=False):
            logger.info("Strm重定向任务正在运行，跳过重复执行")
            return 0

        try:
            return self._update_strm(
                str(target_from or ""),
                str(target_to or ""),
                directory or self._strm_path,
            )
        finally:
            self._run_lock.release()

    def _update_strm(
        self,
        target_from: str,
        target_to: str,
        directory: str | Path,
    ) -> int:
        """在已取得执行锁的前提下处理目录中的普通 STRM 文件。"""
        if not directory:
            return 0

        updated = 0
        for file_path in self._iter_strm_files(directory):
            try:
                with file_path.open("r", encoding="utf-8", newline="") as file:
                    original = file.read()
            except (OSError, UnicodeError) as error:
                logger.warning(f"读取 Strm 文件失败：{file_path} - {error}")
                continue

            decoded = urllib.parse.unquote(original)
            content = decoded if self._unquote else original
            if target_from and target_to:
                replaced = self._replace_prefix(decoded, target_from, target_to)
                if replaced != decoded:
                    content = replaced

            if content == original:
                continue

            try:
                self._atomic_write(file_path, content)
            except OSError as error:
                logger.error(f"写入 Strm 文件失败：{file_path} - {error}")
                continue

            updated += 1
            logger.info(f"Strm重定向完成：{file_path}")

        return updated

    @staticmethod
    def _iter_strm_files(directory: str | Path) -> Iterator[Path]:
        """只枚举目录下的普通、非符号链接 ``.strm`` 文件。"""
        root = Path(directory)
        if root.is_symlink() or not root.is_dir():
            return

        for current_root, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current_root)
            directories[:] = [
                name
                for name in directories
                if not (current_path / name).is_symlink()
            ]
            for name in files:
                file_path = current_path / name
                if (
                    file_path.is_symlink()
                    or not file_path.is_file()
                    or file_path.suffix.casefold() != ".strm"
                ):
                    continue
                yield file_path

    @classmethod
    def _replace_prefix(cls, content: str, origin: str, target: str) -> str:
        """仅替换开头的完整路径或 URL 前缀，避免误伤相似名称和后续文本。"""
        if not origin or not target:
            return content

        if cls._is_url(origin):
            suffix = cls._url_prefix_remainder(content, origin)
            if suffix is None:
                return content
            return cls._join_prefix(target, suffix, is_url=True)
        else:
            suffix = cls._path_prefix_remainder(content, origin)
            if suffix is None:
                return content
            return cls._join_prefix(target, suffix, is_url=False)

    @staticmethod
    def _is_url(value: str) -> bool:
        """识别带协议分隔符的 URL，避免把 Windows 盘符当作 URL。"""
        return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value))

    @classmethod
    def _path_prefix_remainder(cls, value: str, prefix: str) -> str | None:
        """返回路径前缀后的文本，并允许源前缀带一个或多个尾随分隔符。"""
        separators = "/\\"
        normalized = prefix.rstrip(separators)
        if not normalized:
            if not value or value[0] not in separators:
                return None
            return value.lstrip(separators)

        if not value.startswith(normalized):
            return None

        suffix = value[len(normalized):]
        if suffix and suffix[0] not in separators:
            return None
        return suffix

    @classmethod
    def _path_prefix_matches(cls, value: str, prefix: str) -> bool:
        """按路径组件边界匹配前缀，而不是按相似字符串匹配。"""
        return cls._path_prefix_remainder(value, prefix) is not None

    @classmethod
    def _url_prefix_remainder(cls, value: str, prefix: str) -> str | None:
        """返回 URL 前缀后的文本，并允许源路径前缀带尾随斜杠。"""
        normalized = prefix.rstrip("/")
        if not value.startswith(normalized):
            return None

        try:
            value_parts = urllib.parse.urlsplit(value)
            prefix_parts = urllib.parse.urlsplit(prefix)
        except ValueError:
            return None

        if (
            value_parts.scheme.casefold() != prefix_parts.scheme.casefold()
            or value_parts.netloc.casefold() != prefix_parts.netloc.casefold()
        ):
            return None

        suffix = value[len(normalized):]
        if suffix and suffix[0] not in "/?#":
            return None
        return suffix

    @classmethod
    def _url_prefix_matches(cls, value: str, prefix: str) -> bool:
        """按 scheme、authority 和 URL 路径边界匹配前缀。"""
        return cls._url_prefix_remainder(value, prefix) is not None

    @staticmethod
    def _join_prefix(target: str, suffix: str, is_url: bool) -> str:
        """拼接替换目标与后缀，在连接处保留恰好一个分隔符。"""
        if suffix.startswith(("?", "#")):
            return target + suffix

        separators = "/" if is_url else "/\\"
        target_base = target.rstrip(separators)
        suffix_base = suffix.lstrip(separators)

        if not suffix_base:
            if suffix:
                separator = target[-1] if target[-1:] in separators else separators[0]
                return target_base + separator
            return target

        if not target_base:
            separator = next((char for char in target if char in separators), separators[0])
            return separator + suffix_base

        separator = next(
            (char for char in reversed(target) if char in separators),
            separators[0],
        )
        return f"{target_base}{separator}{suffix_base}"

    @staticmethod
    def _atomic_write(file_path: Path, content: str) -> None:
        """在原文件同目录写入 UTF-8 临时文件，再以原子替换提交结果。"""
        temporary_path: Path | None = None
        original_mode = stat.S_IMODE(file_path.stat().st_mode)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, file_path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册动态 HTTP API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """为宿主注册一次性执行服务。"""
        if not self.get_state():
            return []
        return [
            {
                "id": "StrmRedirect",
                "name": "Strm重定向服务",
                "trigger": "date",
                "func": self._run_service,
                "kwargs": {"run_date": datetime.now() + timedelta(seconds=3)},
                "func_kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单及默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "unquote",
                                            "label": "解码URL",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "strm_path",
                                            "label": "strm路径",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "origin_path",
                                            "label": "源路径",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "redirect_path",
                                            "label": "新路径",
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
                                            "text": "源路径->新路径，将会替换所有.strm文件中的源路径为新路径。",
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
                                            "text": "如想解码Strm中的url路径，仅需勾选解码URL和填写strm路径即可。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "onlyonce": False,
            "unquote": False,
            "strm_path": "",
            "origin_path": "",
            "redirect_path": "",
        }

    def get_page(self) -> List[dict]:
        """当前插件不提供详情页。"""
        return []

    def get_state(self) -> bool:
        """只有待执行且配置完整时才向宿主暴露服务。"""
        return self._onlyonce and self._can_run()

    def stop_service(self):
        """当前插件不持有宿主之外的后台资源。"""
        return None
