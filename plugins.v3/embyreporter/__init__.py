from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import pytz
from PIL import Image, ImageDraw, ImageFont
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper
from app.sdk.utilities import StringUtils


@dataclass(frozen=True)
class _EmbyConnection:
    """记录一次报告执行所需的 Emby 外部服务连接上下文。"""

    name: str
    host: str
    api_key: str
    user_id: Optional[str] = None


class EmbyReporter(_PluginBase):
    """从 Emby Playback Report 生成观影排行海报并发送通知。"""

    plugin_name = "Emby观影报告"
    plugin_desc = "推送Emby观影报告，需Emby安装Playback Report插件。"
    plugin_icon = "Pydiocells_A.png"
    plugin_version = "3.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embyreporter_"
    plugin_order = 30
    auth_level = 1

    PLAYBACK_REPORTING_TYPE_MOVIE = "ItemName"
    PLAYBACK_REPORTING_TYPE_TVSHOWS = "substr(ItemName,0, instr(ItemName, ' - '))"
    _REPORT_PART_HEIGHTS = (250, 330, 335)

    def __init__(self) -> None:
        """初始化插件运行状态；外部连接只在报告执行期间绑定。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._run_once = False
        self._res_dir = ""
        self._cron: Optional[str] = None
        self._days = 7
        self._type = MessageType.MediaServer.name
        self._cnt = 10
        self._mp_host = ""
        self._emby_host = ""
        self._emby_api_key = ""
        self._show_time = True
        self._mediaservers: list[str] = []
        self._black_library = ""
        self._mediaserver_helper: Optional[MediaServerHelper] = None
        self._connection: Optional[_EmbyConnection] = None
        self._run_lock = threading.Lock()
        self._run_once_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并准备由宿主调度器管理的报告任务。"""
        self.stop_service()
        config = config or {}

        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._cron = self._normalize_cron(config.get("cron"))
        self._res_dir = str(config.get("res_dir") or "").strip()
        self._days = self._positive_int(config.get("days"), 7)
        self._cnt = self._positive_int(config.get("cnt"), 10)
        self._type = str(config.get("type") or MessageType.MediaServer.name)
        self._mp_host = str(config.get("mp_host") or "").strip()
        self._show_time = bool(config.get("show_time", True))
        self._black_library = str(config.get("black_library") or "").strip()
        self._emby_host = str(config.get("emby_host") or "").strip()
        self._emby_api_key = str(config.get("emby_api_key") or "").strip()
        self._mediaservers = [
            str(name).strip()
            for name in (config.get("mediaservers") or [])
            if str(name).strip()
        ]
        self._mediaserver_helper = MediaServerHelper()

        if self._run_once:
            self._onlyonce = False
            self._save_config()

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """把用户配置转换为正整数，非法值回退到稳定默认值。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _normalize_cron(value: Any) -> Optional[str]:
        """规范化可选五段 cron 文本，空值表示不注册周期任务。"""
        normalized = str(value or "").strip()
        return " ".join(normalized.split()) if normalized else None

    def _save_config(self) -> bool:
        """保存归一化配置，并让一次性开关只消费一次。"""
        return self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron or "",
                "res_dir": self._res_dir,
                "days": self._days,
                "cnt": self._cnt,
                "mp_host": self._mp_host,
                "show_time": self._show_time,
                "black_library": self._black_library,
                "emby_host": self._emby_host,
                "emby_api_key": self._emby_api_key,
                "mediaservers": self._mediaservers,
                "type": self._type,
            }
        )

    def get_state(self) -> bool:
        """返回插件是否启用或仍有待执行的一次性任务。"""
        return self._enabled or self._run_once

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不暴露宿主 HTTP API。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """按 V3 公共服务合同注册一次性和周期报告任务。"""
        services: list[dict[str, Any]] = []
        if self._run_once:
            services.append(
                {
                    "id": "EmbyReporter.Once",
                    "name": "Emby观影报告（立即运行）",
                    "trigger": "date",
                    "func": self._run_once_report,
                    "kwargs": {
                        "run_date": datetime.now(
                            pytz.timezone(str(settings.TZ))
                        )
                        + timedelta(seconds=3)
                    },
                }
            )

        if self._enabled and self._cron:
            try:
                services.append(
                    {
                        "id": "EmbyReporter",
                        "name": "Emby观影报告",
                        "trigger": CronTrigger.from_crontab(
                            self._cron,
                            timezone=pytz.timezone(str(settings.TZ)),
                        ),
                        "func": self.report,
                        "kwargs": {},
                    }
                )
            except (TypeError, ValueError) as error:
                logger.error(f"定时任务配置错误：{error}")
                self.systemmessage.put(f"执行周期配置错误：{error}")
        return services

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回 V3 Vuetify 配置页和默认配置。"""
        message_types = [
            {"title": item.value, "value": item.name} for item in MessageType
        ]
        media_server_items = []
        helper = self._mediaserver_helper or MediaServerHelper()
        try:
            media_server_items = [
                {"title": config.name, "value": config.name}
                for config in helper.get_configs().values()
                if config.type == "emby"
            ]
        except (RuntimeError, TypeError, AttributeError):
            # 配置组合根尚未装配时，配置页仍应能返回静态表单。
            media_server_items = []

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
                                        "props": {"model": "enabled", "label": "启用插件"},
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，留空自动",
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
                                            "model": "res_dir",
                                            "label": "素材路径",
                                            "placeholder": "本地素材路径",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "days",
                                            "label": "报告天数",
                                            "placeholder": "向前获取数据的天数",
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
                                            "model": "cnt",
                                            "label": "观影记录数量",
                                            "placeholder": "获取观影数据数量，默认10",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "mp_host",
                                            "label": "MoviePilot域名",
                                            "placeholder": "必填，末尾不带/",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "chips": True,
                                            "model": "type",
                                            "label": "推送方式",
                                            "items": message_types,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "show_time",
                                            "label": "是否显示观看时长",
                                            "items": [
                                                {"title": "是", "value": True},
                                                {"title": "否", "value": False},
                                            ],
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
                                            "model": "black_library",
                                            "label": "黑名单媒体库名称",
                                            "placeholder": "多个名称用英文逗号分隔",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "emby_host",
                                            "label": "自定义emby host",
                                            "placeholder": "IP:PORT",
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
                                            "model": "emby_api_key",
                                            "label": "自定义emby apiKey",
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
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "model": "mediaservers",
                                            "label": "媒体服务器",
                                            "items": media_server_items,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "如生成观影报告有空白记录，可酌情调大观影记录数量。",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "未设置自定义emby配置时，读取已配置的Emby媒体服务器。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "res_dir": "",
            "days": 7,
            "cnt": 10,
            "emby_host": "",
            "emby_api_key": "",
            "mp_host": "",
            "black_library": "",
            "show_time": True,
            "type": "",
            "mediaservers": [],
        }

    def get_page(self) -> list[dict]:
        """本插件没有详情页。"""
        return []

    def stop_service(self) -> None:
        """释放插件自有的执行上下文；公共任务由宿主调度器负责撤销。"""
        self._connection = None

    @staticmethod
    def _normalize_host(value: Any) -> str:
        """把 Emby 地址规范化为不带尾部斜杠的 HTTP 基地址。"""
        host = str(value or "").strip()
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host.rstrip("/")

    def _build_connection(self, name: str, service: Any) -> Optional[_EmbyConnection]:
        """从 V3 媒体服务器服务投影提取 Emby 外部协议参数。"""
        config = service.config.config if service.config else {}
        config = config if isinstance(config, dict) else {}
        host = self._normalize_host(config.get("host"))
        api_key = str(config.get("apikey") or "").strip()
        if not host or not api_key:
            logger.warning(f"媒体服务器 {name} 配置不完整，跳过观影报告")
            return None

        user_id = None
        if service.instance:
            try:
                resolved_user = service.instance.get_user()
                user_id = str(resolved_user) if resolved_user is not None else None
            except Exception as error:  # 外部媒体服务器实例的用户查询失败不应中断其他服务
                logger.debug(f"获取媒体服务器 {name} 用户失败：{error}")
        return _EmbyConnection(
            name=str(name),
            host=host,
            api_key=api_key,
            user_id=user_id,
        )

    def _connections(self) -> list[_EmbyConnection]:
        """按配置返回启用的 Emby 服务，并优先使用完整的旧自定义连接配置。"""
        custom_host = self._normalize_host(self._emby_host)
        if custom_host and self._emby_api_key:
            return [
                _EmbyConnection(
                    name="Emby",
                    host=custom_host,
                    api_key=self._emby_api_key,
                )
            ]

        helper = self._mediaserver_helper or MediaServerHelper()
        try:
            services = helper.get_services(
                name_filters=self._mediaservers,
                type_filter="emby",
            )
        except (RuntimeError, TypeError, AttributeError) as error:
            logger.error(f"获取Emby媒体服务器失败：{error}")
            services = {}

        connections = []
        for name, service in (services or {}).items():
            connection = self._build_connection(name, service)
            if connection:
                connections.append(connection)

        return connections

    def _run_once_report(self) -> None:
        """消费一次性服务标记，再执行一轮观影报告。"""
        with self._run_once_lock:
            if not self._run_once:
                return
            self._run_once = False
        self.report()

    def report(self) -> None:
        """读取各 Emby 服务的观影记录、生成海报并发送两张排行图片。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Emby观影报告任务正在运行，本次触发已跳过")
            return

        try:
            self._report()
        finally:
            self._run_lock.release()

    def _report(self) -> None:
        """在 single-flight 门禁内执行一次观影报告。"""
        if not self._mp_host or not self._type:
            logger.warning("未配置MoviePilot域名或推送方式，跳过Emby观影报告")
            return

        connections = self._connections()
        if not connections:
            logger.error("未配置Emby媒体服务器")
            return

        report_time = datetime.now().strftime("%Y%m%d%H%M%S")
        report_host = self._normalize_host(self._mp_host)
        message_type = self._message_type()
        for connection in connections:
            self._connection = connection
            try:
                movie_ok, movies = self.get_report(
                    types=self.PLAYBACK_REPORTING_TYPE_MOVIE,
                    days=self._days,
                    limit=self._cnt,
                )
                if not movie_ok:
                    logger.error(f"{connection.name} 获取电影数据失败：{movies}")
                    movies = []

                tv_ok, tvshows = self.get_report(
                    types=self.PLAYBACK_REPORTING_TYPE_TVSHOWS,
                    days=self._days,
                    limit=self._cnt,
                )
                if not tv_ok:
                    logger.error(f"{connection.name} 获取电视剧数据失败：{tvshows}")
                    tvshows = []

                report_path = self.draw(
                    res_path=self._res_dir,
                    movies=movies if isinstance(movies, list) else [],
                    tvshows=tvshows if isinstance(tvshows, list) else [],
                    show_time=self._show_time,
                    emby_name=connection.name,
                )
                if not report_path:
                    logger.error(f"{connection.name} 生成观影报告失败")
                    continue

                safe_name = self._safe_name(connection.name)
                self.__split_image_by_height(
                    report_path,
                    self._public_dir() / f"report_{safe_name}",
                    self._REPORT_PART_HEIGHTS,
                )
                for part_number, title in (
                    (2, f"Movies 近{self._days}日观影排行"),
                    (3, f"TV Shows 近{self._days}日观影排行"),
                ):
                    relative_path = f"/report_{safe_name}_part_{part_number}.jpg"
                    report_url = (
                        f"{report_host}{relative_path}?_timestamp={report_time}"
                    )
                    self.post_message(
                        title=title,
                        mtype=message_type,
                        image=report_url,
                    )
                    logger.info(f"{connection.name} 观影记录推送成功 {report_url}")
            finally:
                self._connection = None

    def _message_type(self) -> MessageType:
        """把旧配置值和 V3 消息类型名称归一为 MessageType。"""
        configured = str(self._type or "").strip()
        for message_type in MessageType:
            if configured in {message_type.name, message_type.value}:
                return message_type
        return MessageType.MediaServer

    @staticmethod
    def _safe_name(value: Any) -> str:
        """生成可用于静态报告文件名的媒体服务器名称。"""
        cleaned = StringUtils.clear_file_name(str(value or "emby")).strip()
        return cleaned or "emby"

    @staticmethod
    def _public_dir() -> Path:
        """返回宿主静态前端目录，报告图片由该目录的 HTTP 静态服务提供。"""
        configured = Path(str(settings.FRONTEND_PATH or "/public"))
        if configured.is_absolute():
            return configured
        return Path(settings.ROOT_PATH) / configured

    @staticmethod
    def __split_image_by_height(
        image_path: str | Path,
        output_path_prefix: str | Path,
        heights: tuple[int, ...] | list[int],
    ) -> list[Path]:
        """按指定高度切分报告，并返回实际生成的静态图片路径。"""
        output_path_prefix = Path(output_path_prefix)
        output_path_prefix.parent.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        with Image.open(image_path) as source:
            image = source.convert("RGB") if source.mode == "RGBA" else source.copy()
        try:
            top = 0
            for index, requested_height in enumerate(heights, start=1):
                if top >= image.height:
                    break
                height = min(max(int(requested_height), 0), image.height - top)
                if height <= 0:
                    continue
                part_path = output_path_prefix.parent / (
                    f"{output_path_prefix.name}_part_{index}.jpg"
                )
                image.crop((0, top, image.width, top + height)).save(part_path)
                parts.append(part_path)
                top += height
        finally:
            image.close()
        return parts

    @staticmethod
    def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """加载自定义字体，素材不完整时使用 Pillow 内置字体继续生成报告。"""
        try:
            return ImageFont.truetype(str(path), size)
        except (OSError, IOError):
            return ImageFont.load_default()

    @staticmethod
    def _normalize_row(row: Any) -> Optional[tuple[str, str, str, str, int, int]]:
        """校验 Playback Report 行并转换为绘图所需的稳定标量。"""
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return None
        try:
            user_id = str(row[0])
            item_id = str(row[1])
            item_type = str(row[2])
            name = str(row[3])
            count = int(row[4] or 0)
            duration = int(float(row[5] or 0))
        except (TypeError, ValueError):
            return None
        if not all((user_id, item_id, name)):
            return None
        return user_id, item_id, item_type, name, count, max(duration, 0)

    def _is_blacklisted(self, info: Any) -> bool:
        """按媒体服务器返回的库名、路径等字段过滤配置中的黑名单文本。"""
        if not self._black_library or not isinstance(info, dict):
            return False
        black_names = [
            name.strip().casefold()
            for name in self._black_library.split(",")
            if name.strip()
        ]
        candidates = [
            str(info.get(key) or "").casefold()
            for key in ("LibraryName", "CollectionName", "Path", "ParentId")
        ]
        return any(
            black_name in candidate
            for black_name in black_names
            for candidate in candidates
            if candidate
        )

    def _prepare_entry(
        self,
        row: Any,
        *,
        movie: bool,
    ) -> Optional[tuple[str, int, bytes]]:
        """读取排行行的封面和媒体详情，返回可直接绘制的条目。"""
        normalized = self._normalize_row(row)
        if not normalized:
            return None
        user_id, item_id, item_type, name, _count, duration = normalized
        info: Optional[dict] = None
        if not movie or self._black_library:
            success, item_info = self.items(user_id, item_id)
            if not success or not isinstance(item_info, dict):
                return None
            info = item_info
            if self._is_blacklisted(info):
                logger.info(f"{name} 已在媒体库黑名单中，已过滤")
                return None

        cover_id = item_id
        if not movie:
            cover_id = str((info or {}).get("SeriesId") or "")
            if not cover_id:
                return None
        success, cover = self.primary(cover_id)
        if not success or not cover:
            return None
        return name, duration, cover

    @staticmethod
    def _layout_entries(
        movie_entries: list[tuple[str, int, bytes]],
        tv_entries: list[tuple[str, int, bytes]],
    ) -> list[tuple[tuple[str, int, bytes], int, int]]:
        """固定电影和电视剧的独立行，避免电影不足时电视剧填入电影区域。"""
        return [
            (entry, column, 0)
            for column, entry in enumerate(movie_entries)
        ] + [
            (entry, column, 331)
            for column, entry in enumerate(tv_entries)
        ]

    def draw(
        self,
        res_path: str | Path,
        movies: list[Any],
        tvshows: list[Any],
        show_time: bool = True,
        emby_name: Optional[str] = None,
    ) -> Optional[Path]:
        """根据排行数据绘制报告海报并保存到宿主静态目录。"""
        resource_dir = Path(res_path) if str(res_path or "").strip() else Path(__file__).parent / "res"
        bg_dir = resource_dir / "bg"
        mask_path = resource_dir / "cover-ranks-mask-2.png"
        font_path = resource_dir / "PingFang Bold.ttf"
        try:
            backgrounds = [path for path in bg_dir.iterdir() if path.is_file()]
        except OSError:
            logger.error(f"观影报告素材目录不存在：{bg_dir}")
            return None
        if not backgrounds or not mask_path.is_file():
            logger.error(f"观影报告素材不完整：{resource_dir}")
            return None

        movie_entries = [
            entry
            for row in movies
            for entry in [self._prepare_entry(row, movie=True)]
            if entry
        ][:5]
        tv_entries = [
            entry
            for row in tvshows
            for entry in [self._prepare_entry(row, movie=False)]
            if entry
        ][:5]
        layout_entries = self._layout_entries(movie_entries, tv_entries)
        if not layout_entries:
            return None

        try:
            with Image.open(random.choice(backgrounds)) as source:
                background = source.convert("RGB").copy()
            with Image.open(mask_path) as mask:
                background.paste(mask, (0, 0), mask if mask.mode in {"RGBA", "L"} else None)
            font = self._load_font(font_path, 18)
            font_small = self._load_font(font_path, 14)
            font_count = self._load_font(font_path, 8)
            text_draw = ImageDraw.Draw(background)

            for (name, duration, cover_data), column, offset_y in layout_entries:
                try:
                    with Image.open(BytesIO(cover_data)) as cover_source:
                        cover = cover_source.convert("RGB").resize((108, 159))
                    background.paste(cover, (73 + 145 * column, 379 + offset_y))
                    cover.close()

                    display_name = name
                    display_font = font
                    font_offset_y = 0
                    try:
                        if font.getlength(display_name) > 110:
                            display_font = font_small
                            font_offset_y = 4
                            while len(display_name) > 1 and font_small.getlength(display_name) > 110:
                                display_name = display_name[:-1]
                            display_name += ".."
                    except (AttributeError, UnicodeError):
                        pass
                    if show_time:
                        duration_text = StringUtils.str_secends(duration)
                        self.draw_text_psd_style(
                            text_draw,
                            (
                                177 + 145 * column - font_count.getlength(duration_text),
                                355 + offset_y,
                            ),
                            duration_text,
                            font_count,
                            126,
                        )
                    self.draw_text_psd_style(
                        text_draw,
                        (74 + 145 * column, 542 + font_offset_y + offset_y),
                        display_name,
                        display_font,
                        126,
                    )
                except (OSError, TypeError, ValueError, UnicodeError) as error:
                    logger.debug(f"绘制观影报告条目失败：{error}")

            public_dir = self._public_dir()
            public_dir.mkdir(parents=True, exist_ok=True)
            output_path = public_dir / f"report_{self._safe_name(emby_name)}.jpg"
            output_path.unlink(missing_ok=True)
            background.save(output_path, format="JPEG")
            background.close()
            return output_path
        except (OSError, ValueError, TypeError) as error:
            logger.error(f"保存观影报告失败：{error}")
            return None

    @staticmethod
    def draw_text_psd_style(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        tracking: float = 0,
        leading: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        """按 PSD tracking 语义逐字符绘制文本。"""
        def stutter_chunk(values: str, size: int, overlap: int = 0) -> Any:
            step = max(size - overlap, 1)
            for index in range(0, len(values), step):
                chunk = list(values[index : index + size])
                while len(chunk) < size:
                    chunk.append(" ")
                yield chunk

        x, y = xy
        font_size = getattr(font, "size", 12)
        lines = str(text).splitlines() or [""]
        leading = leading if leading is not None else font_size * 1.2
        for line in lines:
            for first, second in stutter_chunk(line, 2, 1):
                width = font.getlength(first + second) - font.getlength(second)
                draw.text((x, y), first, font=font, **kwargs)
                x += width + (tracking / 1000) * font_size
            y += leading
            x = xy[0]

    def _image_request(
        self,
        path: str,
        params: dict[str, Any],
        *,
        ret_url: bool = False,
    ) -> tuple[bool, Any] | str:
        """请求 Emby 图片并在读取内容后释放 HTTP 响应。"""
        if not self._connection:
            return False, "🤕Emby 服务器未连接!"
        url = f"{self._connection.host}/{path.lstrip('/')}"
        if ret_url:
            query = "&".join(f"{key}={value}" for key, value in params.items())
            return f"{url}?{query}" if query else url
        response = None
        try:
            request_params = dict(params)
            request_params["api_key"] = self._connection.api_key
            response = RequestUtils().get_res(url=url, params=request_params)
            if response is None or response.status_code not in (200, 204):
                return False, "🤕Emby 服务器连接失败!"
            return True, response.content
        except Exception:
            return False, "🤕Emby 服务器连接失败!"
        finally:
            if response is not None:
                response.close()

    def primary(
        self,
        item_id: str,
        width: int = 720,
        height: int = 1440,
        quality: int = 90,
        ret_url: bool = False,
    ) -> tuple[bool, Any] | str:
        """获取 Emby 条目的主海报。"""
        return self._image_request(
            f"emby/Items/{item_id}/Images/Primary",
            {"maxHeight": height, "maxWidth": width, "quality": quality},
            ret_url=ret_url,
        )

    def backdrop(
        self,
        item_id: str,
        width: int = 1920,
        quality: int = 70,
        ret_url: bool = False,
    ) -> tuple[bool, Any] | str:
        """获取 Emby 条目的背景图。"""
        return self._image_request(
            f"emby/Items/{item_id}/Images/Backdrop/0",
            {"maxWidth": width, "quality": quality},
            ret_url=ret_url,
        )

    def logo(
        self,
        item_id: str,
        quality: int = 70,
        ret_url: bool = False,
    ) -> tuple[bool, Any] | str:
        """获取 Emby 条目的 Logo 图片。"""
        return self._image_request(
            f"emby/Items/{item_id}/Images/Logo",
            {"quality": quality},
            ret_url=ret_url,
        )

    def items(self, user_id: str, item_id: str) -> tuple[bool, Any]:
        """读取 Emby 用户范围内的媒体条目详情。"""
        if not self._connection:
            return False, "🤕Emby 服务器未连接!"
        response = None
        try:
            response = RequestUtils().get_res(
                url=(
                    f"{self._connection.host}/emby/Users/{user_id}/Items/{item_id}"
                ),
                params={"api_key": self._connection.api_key},
            )
            if response is None or response.status_code not in (200, 204):
                return False, "🤕Emby 服务器连接失败!"
            return True, response.json()
        except Exception:
            return False, "🤕Emby 服务器连接失败!"
        finally:
            if response is not None:
                response.close()

    def get_report(
        self,
        days: int,
        types: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 10,
    ) -> tuple[bool, Any]:
        """通过 Playback Report 的原生查询接口读取电影或电视剧排行。"""
        if not self._connection:
            return False, "🤕Emby 服务器未连接!"
        report_type = types or self.PLAYBACK_REPORTING_TYPE_MOVIE
        if report_type not in {
            self.PLAYBACK_REPORTING_TYPE_MOVIE,
            self.PLAYBACK_REPORTING_TYPE_TVSHOWS,
        }:
            report_type = self.PLAYBACK_REPORTING_TYPE_MOVIE
        item_type = "Movie" if report_type == self.PLAYBACK_REPORTING_TYPE_MOVIE else "Episode"
        timezone = pytz.timezone(str(settings.TZ))
        now = datetime.now(timezone)
        start_time = (now - timedelta(days=self._positive_int(days, 7))).strftime(
            "%Y-%m-%d 00:00:00"
        )
        end_time = now.strftime("%Y-%m-%d 23:59:59")
        safe_limit = min(self._positive_int(limit, 10), 100)
        sql = (
            "SELECT UserId, ItemId, ItemType, "
            f"{report_type} AS name, "
            "COUNT(1) AS play_count, "
            "SUM(PlayDuration - PauseDuration) AS total_duration "
            "FROM PlaybackActivity "
            f"WHERE ItemType = '{item_type}' "
            f"AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}' "
            "AND UserId not IN (select UserId from UserList) "
        )
        if user_id:
            escaped_user_id = str(user_id).replace("'", "''")
            sql += f"AND UserId = '{escaped_user_id}' "
        sql += f"GROUP BY name ORDER BY total_duration DESC LIMIT {safe_limit}"

        response = None
        try:
            response = RequestUtils().post_res(
                url=(
                    f"{self._connection.host}/emby/"
                    "user_usage_stats/submit_custom_query"
                ),
                params={"api_key": self._connection.api_key},
                data={"CustomQueryString": sql, "ReplaceUserId": False},
            )
            if response is None or response.status_code not in (200, 204):
                return False, "🤕Emby 服务器连接失败!"
            payload = response.json()
            if not isinstance(payload, dict):
                return False, "🤕Emby 返回数据格式错误!"
            columns = payload.get("colums")
            if columns is None:
                columns = payload.get("columns")
            if not columns:
                return False, payload.get("message") or "🤕Emby 未返回观影数据!"
            results = payload.get("results")
            if not isinstance(results, list):
                return False, "🤕Emby 返回数据格式错误!"
            return True, results
        except Exception:
            return False, "🤕Emby 服务器连接失败!"
        finally:
            if response is not None:
                response.close()
