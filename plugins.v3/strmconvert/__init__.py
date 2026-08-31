from __future__ import annotations

import os
import stat
import tempfile
import threading
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger


class StrmConvert(_PluginBase):
    """按配置把 STRM 文件转换为本地路径或云盘 API 路径。"""

    plugin_name = "Strm文件模式转换"
    plugin_desc = "Strm文件内容转为本地路径或者cd2/alist API路径。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/convert.png"
    plugin_version = "2.0.1"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "strmconvert_"
    plugin_order = 27
    auth_level = 1

    def __init__(self) -> None:
        """初始化一次性转换任务的实例状态。"""
        super().__init__()
        self._to_local = False
        self._to_api = False
        self._convert_confs = ""
        self._run_once = False
        self._run_mode: Optional[str] = None
        self._run_lock = threading.Lock()
        self._once_lock = threading.Lock()

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并执行一次本地路径或 API 路径转换。"""
        config = config or {}
        self._to_local = bool(config.get("to_local"))
        self._to_api = bool(config.get("to_api"))
        raw_confs = config.get("convert_confs")
        self._convert_confs = raw_confs if isinstance(raw_confs, str) else ""

        if raw_confs is not None and not isinstance(raw_confs, str):
            logger.error("转换配置必须是文本格式，已跳过处理")

        if self._to_local and self._to_api:
            logger.error("本地模式和API模式同时只能开启一个")
            return

        with self._once_lock:
            self._run_mode = (
                "local" if self._to_local else "api" if self._to_api else None
            )
            self._run_once = self._run_mode is not None

    def __consume_once(self) -> Optional[Tuple[str, str]]:
        """在宿主回调开始时消费一次性请求并返回不可变执行快照。"""
        with self._once_lock:
            if not self._run_once or self._run_mode is None:
                return None
            mode = self._run_mode
            convert_confs = self._convert_confs
            self._run_once = False
            self._run_mode = None
            self._to_local = False
            self._to_api = False
            try:
                self.update_config(
                    {
                        "to_local": False,
                        "to_api": False,
                        "convert_confs": convert_confs,
                    }
                )
            except Exception as error:  # noqa: BLE001 - 状态写回失败不能重复进入当前实例
                logger.error("保存一次性转换状态失败：%s", error)
            return mode, convert_confs

    def __run_conversion(self) -> bool:
        """以 single-flight 门禁执行一次由宿主调度的转换任务。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("STRM 转换任务正在运行，本次触发已跳过")
            return False
        try:
            execution = self.__consume_once()
            if execution is None:
                return False
            mode, convert_confs = execution
            lines = convert_confs.splitlines()
            if mode == "local":
                self.__convert_to_local(lines)
            else:
                self.__convert_to_api(lines)
            return True
        finally:
            self._run_lock.release()

    def __convert_to_local(self, convert_confs: Iterable[str]) -> None:
        """逐条执行本地媒体库路径转换。"""
        for raw_conf in convert_confs:
            parsed = self.__parse_config_line(raw_conf, expected_parts=2)
            if parsed is None:
                continue
            source_path, library_path = parsed
            logger.info("%s 开始转为本地模式", source_path)
            self.__to_local(source_path, library_path)
            logger.info("%s 转换本地模式已结束", source_path)

    def __to_local(self, source_path: str, library_path: str) -> None:
        """将 STRM 文件改写为媒体库中的本地媒体路径。"""
        source_root = Path(source_path).expanduser()
        library_root = Path(library_path).expanduser()
        for file_path in self.__list_files(source_root, [".strm"]):
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                suffix = self.__media_suffix(content)
                if suffix is None:
                    logger.warning("STRM 文件内容没有可用媒体扩展名：%s", file_path)
                    continue

                mapped_path = self.__map_path(file_path, source_root, library_root)
                if mapped_path is None:
                    continue
                target_path = mapped_path.with_suffix(suffix)
                self.__write_if_changed(file_path, str(target_path))
            except (OSError, UnicodeError, ValueError) as error:
                logger.error("转换本地路径失败：%s，原因：%s", file_path, error)

    def __convert_to_api(self, convert_confs: Iterable[str]) -> None:
        """逐条执行 cd2 或 alist API 路径转换。"""
        for raw_conf in convert_confs:
            parsed = self.__parse_config_line(raw_conf, expected_parts=4)
            if parsed is None:
                continue
            source_path, library_path, cloud_type, cloud_url = parsed
            cloud_type = cloud_type.casefold()
            if cloud_type not in {"cd2", "alist"}:
                logger.error("转换配置 %s 的云盘类型无效，已跳过处理", raw_conf)
                continue
            logger.info("%s 开始转为 API 模式", source_path)
            self.__to_api(source_path, library_path, cloud_type, cloud_url)
            logger.info("%s 转换 API 模式已结束", source_path)

    def __to_api(
        self,
        source_path: str,
        library_path: str,
        cloud_type: str,
        cloud_url: str,
    ) -> None:
        """将 STRM 文件改写为对应的 cd2 或 alist API 地址。"""
        source_root = Path(source_path).expanduser()
        library_root = Path(library_path).expanduser()
        endpoint, endpoint_ref = self.__api_endpoint(cloud_url)
        if endpoint is None or endpoint_ref is None:
            logger.error("API 地址为空，已跳过路径转换")
            return

        for file_path in self.__list_files(source_root, [".strm"]):
            try:
                mapped_path = self.__map_path(file_path, source_root, library_root)
                if mapped_path is None:
                    continue
                encoded_path = urllib.parse.quote(str(mapped_path), safe="")
                if cloud_type == "cd2":
                    api_file = (
                        f"{endpoint}/static/http/{endpoint_ref}/False/{encoded_path}"
                    )
                else:
                    api_file = f"{endpoint}/d/{encoded_path}"
                self.__write_if_changed(file_path, api_file)
            except (OSError, UnicodeError, ValueError) as error:
                logger.error("转换 API 路径失败：%s，原因：%s", file_path, error)

    @staticmethod
    def __api_endpoint(cloud_url: str) -> Tuple[Optional[str], Optional[str]]:
        """规范化服务地址，同时保留 cd2 URL 中的服务引用格式。"""
        cloud_url = cloud_url.strip().rstrip("/")
        if not cloud_url:
            return None, None
        if cloud_url.startswith(("http://", "https://")):
            endpoint = cloud_url
            endpoint_ref = cloud_url.split("://", 1)[1]
        else:
            endpoint = f"http://{cloud_url}"
            endpoint_ref = cloud_url
        return endpoint, endpoint_ref

    @staticmethod
    def __parse_config_line(
        raw_conf: str,
        expected_parts: int,
    ) -> Optional[Tuple[str, ...]]:
        """解析一条以 ``#`` 分隔的配置，并拒绝空字段或多余分隔符。"""
        conf = str(raw_conf).strip()
        if not conf:
            return None
        parts = tuple(part.strip() for part in conf.split("#"))
        if len(parts) != expected_parts or any(not part for part in parts):
            logger.error("转换配置 %s 格式错误，已跳过处理", raw_conf)
            return None
        return parts

    @staticmethod
    def __media_suffix(content: str) -> Optional[str]:
        """从本地路径或 URL 中提取媒体扩展名，忽略查询串和 URL 编码。"""
        content = content.strip()
        if not content:
            return None
        parsed = urllib.parse.urlsplit(content)
        path = parsed.path if parsed.scheme and parsed.netloc else content
        suffix = Path(urllib.parse.unquote(path)).suffix
        return suffix or None

    @staticmethod
    def __map_path(
        file_path: Path,
        source_root: Path,
        library_root: Path,
    ) -> Optional[Path]:
        """把文件映射到目标根目录，避免相似前缀路径被误替换。"""
        try:
            if source_root.is_file():
                if file_path != source_root:
                    raise ValueError
                relative_path = file_path.relative_to(source_root.parent)
            else:
                relative_path = file_path.relative_to(source_root)
        except ValueError:
            logger.error("文件不在转换源目录内：%s", file_path)
            return None
        return library_root / relative_path

    @staticmethod
    def __write_if_changed(file_path: Path, content: str) -> bool:
        """仅在内容变化时以同目录临时文件原子替换原文件。"""
        try:
            if file_path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeError) as error:
            logger.error("读取 STRM 文件失败：%s，原因：%s", file_path, error)
            return False

        temporary_path: Optional[str] = None
        try:
            original_mode = stat.S_IMODE(file_path.stat().st_mode)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                temporary_path = temporary_file.name
            # 原子替换会生成新 inode，临时文件默认权限为 0600。
            # 复制原文件权限位，避免收窄 STRM 文件访问权限。
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, file_path)
            return True
        except (OSError, UnicodeError) as error:
            logger.error("写入 STRM 文件失败：%s，原因：%s", file_path, error)
            return False
        finally:
            if temporary_path:
                try:
                    Path(temporary_path).unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def __list_files(
        directory: Path,
        extensions: Iterable[str] = (".strm",),
        min_filesize: int = 0,
    ) -> List[Path]:
        """递归列出指定扩展名的常规文件，不跟随符号链接。"""
        directory = Path(directory)
        if directory.is_symlink():
            return []

        allowed_extensions = {
            extension.casefold()
            if extension.startswith(".")
            else f".{extension}".casefold()
            for extension in extensions
        }
        try:
            minimum_size = max(0, int(min_filesize)) * 1024 * 1024
        except (TypeError, ValueError):
            minimum_size = 0

        if directory.is_file():
            try:
                if (
                    directory.suffix.casefold() in allowed_extensions
                    and directory.stat().st_size >= minimum_size
                ):
                    return [directory]
            except OSError:
                return []
            return []
        if not directory.is_dir():
            return []

        files: List[Path] = []
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if path.suffix.casefold() not in allowed_extensions:
                continue
            try:
                if path.stat().st_size >= minimum_size:
                    files.append(path)
            except OSError:
                continue
        return sorted(files, key=lambda path: str(path))

    def get_state(self) -> bool:
        """一次性转换请求在宿主回调消费前保持可见。"""
        with self._once_lock:
            return self._run_once

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不注册 HTTP API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """把一次性文件转换登记为宿主 date 服务。"""
        with self._once_lock:
            run_once = self._run_once
        if not run_once:
            return []
        timezone = pytz.timezone(str(settings.TZ))
        return [
            {
                "id": "StrmConvert.Once",
                "name": "STRM 文件模式转换（立即运行）",
                "trigger": "date",
                "func": self.__run_conversion,
                "kwargs": {
                    "run_date": datetime.now(tz=timezone) + timedelta(seconds=3)
                },
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回本地模式和 API 模式共用的配置表单。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "to_local",
                                            "label": "转为本地模式",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "to_api",
                                            "label": "转为API模式",
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
                                            "model": "convert_confs",
                                            "label": "转换配置",
                                            "rows": 3,
                                            "placeholder": "strm文件根路径#转换路径",
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
                                            "text": (
                                                "转换配置（转为本地模式）："
                                                "strm文件根路径#转换路径。"
                                                "转换路径为源文件挂载进媒体服务器的路径。"
                                            ),
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
                                            "text": (
                                                "转换配置（转为API模式）："
                                                "strm文件根路径#转换路径#cd2/alist#cd2/alist服务地址(ip:port)。"
                                                "转换路径为云盘根路径。"
                                            ),
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
                                            "text": (
                                                "配置说明："
                                                "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/StrmConvert.md"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "to_local": False,
            "to_api": False,
            "convert_confs": "",
        }

    def get_page(self) -> List[dict]:
        """本插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """本插件没有需要停止的后台服务。"""
        return None
