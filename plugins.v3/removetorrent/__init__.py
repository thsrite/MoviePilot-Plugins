"""按站点 tracker 清理下载器中的已完成种子。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.plugins import _PluginBase
from app.sdk.logging import logger
from app.sdk.services import DownloaderHelper


# pylint: disable=too-many-instance-attributes
class RemoveTorrent(_PluginBase):
    """按站点 tracker 清理下载器中的已完成种子。"""

    plugin_name = "删除站点种子"
    plugin_desc = "删除下载器中某站点种子。"
    plugin_icon = "delete.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "removetorrent_"
    plugin_order = 30
    auth_level = 1

    _supported_types = {"qbittorrent", "transmission"}
    _legacy_downloader_types = {"qb": "qbittorrent", "tr": "transmission"}

    def __init__(self) -> None:
        """初始化配置状态和下载器服务发现门面。"""
        super().__init__()
        self._downloader_helper = DownloaderHelper()
        self._downloader = ""
        self._onlyonce = False
        self._delete_type = False
        self._delete_torrent = False
        self._delete_file = False
        self._trackers = ""
        self._processed_hashes: set[str] = set()

    def init_plugin(self, config: dict = None) -> None:
        """读取配置，并在一次性任务执行前持久化消费状态。"""
        self._downloader_helper = DownloaderHelper()
        self._processed_hashes = set()
        config = config or {}
        configured_downloader = str(config.get("downloader") or "").strip()
        self._downloader = self.__resolve_downloader_name(configured_downloader) or ""
        self._onlyonce = bool(config.get("onlyonce"))
        self._delete_type = bool(config.get("delete_type"))
        self._delete_torrent = bool(config.get("delete_torrent"))
        self._delete_file = bool(config.get("delete_file"))
        self._trackers = config.get("trackers") or ""

        if not self._onlyonce:
            return

        self._onlyonce = False
        try:
            saved = self.__update_config(self._downloader or configured_downloader)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"保存删除站点种子一次性状态失败：{err}")
            return
        if saved is False:
            logger.error("保存删除站点种子一次性状态失败，已停止执行")
            return

        self.__run_once()

    def __update_config(self, configured_downloader: str) -> bool:
        """保存归一化后的配置，确保重载不会重复消费一次性任务。"""
        return self.update_config(
            {
                "downloader": configured_downloader,
                "delete_type": self._delete_type,
                "delete_torrent": self._delete_torrent,
                "delete_file": self._delete_file,
                "trackers": self._trackers,
                "onlyonce": self._onlyonce,
            }
        )

    def __run_once(self) -> None:
        """按配置顺序处理 tracker，并避免同一任务重复删除同一 hash。"""
        trackers = self.__parse_trackers(self._trackers)
        if not trackers:
            logger.warning("未配置站点 tracker，停止删除站点种子")
            return

        for index, tracker in enumerate(trackers, start=1):
            logger.info(f"下载器 {self._downloader} 开始处理第 {index} 个站点 tracker")
            self.__check_feed(tracker)
            logger.info(f"下载器 {self._downloader} 处理第 {index} 个站点 tracker 完成")

    # pylint: disable=too-many-locals,too-many-branches,too-many-return-statements,too-many-statements
    def __check_feed(self, tracker: str) -> None:
        """查询已完成种子并依据同名同大小的辅种数量决定是否处理。"""
        service = self.__get_downloader(self._downloader)
        if service is None:
            return

        downloader = service.instance
        downloader_type = str(service.type or "").strip().lower()
        try:
            torrents = downloader.get_completed_torrents()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"下载器 {self._downloader} 查询已完成种子失败：{err}")
            return
        if torrents is None:
            logger.error(f"下载器 {self._downloader} 查询已完成种子失败，未获得可靠结果")
            return
        if isinstance(torrents, (str, bytes, Mapping)):
            logger.error(f"下载器 {self._downloader} 返回了无效的种子列表，停止处理")
            return
        try:
            torrent_list = list(torrents)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"下载器 {self._downloader} 返回的种子列表无法读取：{err}")
            return
        if not torrent_list:
            logger.info(f"下载器 {self._downloader} 未获取到已完成种子")
            return

        torrent_counts: Dict[Tuple[str, str], int] = {}
        target_torrents: List[Tuple[Tuple[str, str], Any]] = []
        for torrent in torrent_list:
            torrent_key = self.__get_torrent_key(torrent, downloader_type)
            if torrent_key is None:
                logger.warning("种子缺少名称或大小，跳过辅种判定")
                continue
            torrent_counts[torrent_key] = torrent_counts.get(torrent_key, 0) + 1
            if self.__tracker_matches(
                self.__get_torrent_trackers(torrent, downloader_type), tracker
            ):
                target_torrents.append((torrent_key, torrent))

        if not target_torrents:
            logger.warning(
                f"下载器 {self._downloader} 未获取到命中目标 tracker 的已完成种子"
            )
            return

        logger.info(
            f"下载器 {self._downloader} 获取到命中目标 tracker "
            f"已完成种子 {len(target_torrents)} 个"
        )

        for torrent_key, torrent in target_torrents:
            torrent_name = self.__get_torrent_name(torrent, downloader_type)
            torrent_hash = self.__get_torrent_hash(torrent, downloader_type)
            if not torrent_name or not torrent_hash:
                logger.warning("命中 tracker 的种子缺少名称或 hash，跳过处理")
                continue
            if torrent_hash in self._processed_hashes:
                continue
            self._processed_hashes.add(torrent_hash)

            count = torrent_counts.get(torrent_key, 0)
            has_auxiliary = count > 1
            should_delete = has_auxiliary if self._delete_type else count == 1
            if not should_delete:
                relation = "有其他站辅种" if has_auxiliary else "无其他站辅种"
                logger.warning(
                    f"种子 {torrent_name} {torrent_hash} {relation}，如需删除请手动处理"
                )
                continue

            if not self._delete_torrent:
                relation = "有其他辅种" if has_auxiliary else "无其他辅种"
                logger.info(f"种子 {torrent_name} {torrent_hash} {relation}，可删除")
                continue

            try:
                deleted = downloader.delete_torrents(
                    delete_file=self._delete_file,
                    ids=torrent_hash,
                )
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.error(f"种子 {torrent_name} {torrent_hash} 删除失败：{err}")
                continue
            if not deleted:
                logger.error(f"种子 {torrent_name} {torrent_hash} 删除失败，下载器未确认成功")
                continue
            relation = "有其他辅种" if has_auxiliary else "无其他辅种"
            logger.info(f"种子 {torrent_name} {torrent_hash} {relation}，已删除")

    # pylint: disable=too-many-return-statements
    def __get_downloader(self, name: str) -> Optional[Any]:
        """按配置名称取得已连接且受支持的下载器服务。"""
        if not name:
            logger.warning("尚未配置下载器，请检查配置")
            return None
        try:
            service = self._downloader_helper.get_service(name)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"获取下载器 {name} 服务失败：{err}")
            return None
        if not service or not service.instance:
            logger.warning(f"获取下载器 {name} 实例失败，请检查配置")
            return None

        downloader_type = str(service.type or "").strip().lower()
        if downloader_type not in self._supported_types:
            logger.error(f"下载器 {name} 类型 {service.type!r} 不受支持，停止处理")
            return None
        try:
            inactive = service.instance.is_inactive()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"检查下载器 {name} 连接状态失败：{err}")
            return None
        if inactive:
            logger.warning(f"下载器 {name} 未连接，请检查配置")
            return None
        return service

    def __resolve_downloader_name(self, name: str) -> Optional[str]:
        """将旧版类型别名解析为唯一的 V3 下载器配置名。"""
        legacy_type = self._legacy_downloader_types.get(name.lower())
        if not legacy_type:
            return name
        try:
            configs = self._downloader_helper.get_configs()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"迁移旧下载器配置 {name!r} 失败：{err}")
            return None

        candidates = [
            config.name
            for config in configs.values()
            if config.name and str(config.type or "").strip().lower() == legacy_type
        ]
        if len(candidates) == 1:
            logger.info(f"旧下载器配置 {name!r} 已迁移为 {candidates[0]!r}")
            return candidates[0]
        if not candidates:
            logger.warning(f"旧下载器配置 {name!r} 没有可用的 {legacy_type} 服务")
        else:
            logger.warning(
                f"旧下载器配置 {name!r} 对应多个 {legacy_type} 服务，请重新选择下载器"
            )
        return None

    @classmethod
    def __get_torrent_key(
        cls, torrent: Any, downloader_type: str
    ) -> Optional[Tuple[str, str]]:
        """读取名称和大小作为辅种判定键，缺失时拒绝猜测。"""
        name = cls.__get_torrent_name(torrent, downloader_type)
        size = cls.__get_torrent_size(torrent, downloader_type)
        if name is None or size is None:
            return None
        name = str(name).strip()
        size = str(size).strip()
        if not name or not size:
            return None
        return name, size

    @classmethod
    def __get_torrent_trackers(cls, torrent: Any, downloader_type: str) -> List[str]:
        """兼容 qbittorrent 和 transmission 的 tracker 对象形态。"""
        if downloader_type == "qbittorrent":
            return cls.__normalise_tracker_values(cls.__get_field(torrent, "tracker"))
        if downloader_type == "transmission":
            for field_name in ("tracker_list", "trackerList", "trackers"):
                values = cls.__normalise_tracker_values(
                    cls.__get_field(torrent, field_name),
                    tracker_objects=field_name == "trackers",
                )
                if values:
                    return values
        return []

    @staticmethod
    def __tracker_matches(trackers: Iterable[str], tracker: str) -> bool:
        """保持旧版的 tracker 子串匹配语义，同时忽略配置行首尾空白。"""
        target = str(tracker or "").strip()
        return bool(target and any(target in value for value in trackers))

    @classmethod
    def __normalise_tracker_values(
        cls, value: Any, tracker_objects: bool = False
    ) -> List[str]:
        """把下载器返回的 tracker 字符串或对象统一为可匹配文本。"""
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            text = value.decode(errors="replace") if isinstance(value, bytes) else value
            return [text.strip()] if text.strip() else []
        if isinstance(value, Mapping) or not isinstance(value, Iterable):
            values = [value]
        else:
            values = value

        result = []
        for item in values:
            if (
                not isinstance(item, (str, bytes))
                and (tracker_objects or isinstance(item, Mapping))
            ):
                item = cls.__get_field(item, "announce") or cls.__get_field(item, "url")
            if isinstance(item, bytes):
                item = item.decode(errors="replace")
            if item is not None and str(item).strip():
                result.append(str(item).strip())
        return result

    @staticmethod
    def __get_field(value: Any, *names: str) -> Any:
        """读取第三方下载器对象的映射键或属性。"""
        if value is None:
            return None
        for name in names:
            if isinstance(value, Mapping):
                result = value.get(name)
            else:
                getter = getattr(value, "get", None)
                if callable(getter):
                    try:
                        result = getter(name)
                    except Exception:  # pylint: disable=broad-exception-caught
                        result = None
                else:
                    result = getattr(value, name, None)
            if result is not None:
                return result
        return None

    @classmethod
    def __get_torrent_name(cls, torrent: Any, downloader_type: str) -> Optional[str]:
        """读取 qbittorrent/transmission 的种子名称。"""
        field_names = ("name",) if downloader_type == "qbittorrent" else ("name",)
        value = cls.__get_field(torrent, *field_names)
        return None if value is None else str(value)

    @classmethod
    def __get_torrent_size(cls, torrent: Any, downloader_type: str) -> Optional[str]:
        """读取 qbittorrent/transmission 的种子总大小。"""
        field_names = (
            ("size", "total_size", "totalSize")
            if downloader_type == "qbittorrent"
            else ("total_size", "totalSize", "size")
        )
        value = cls.__get_field(torrent, *field_names)
        return None if value is None else str(value)

    @classmethod
    def __get_torrent_hash(cls, torrent: Any, downloader_type: str) -> Optional[str]:
        """读取下载器删除接口需要的种子 hash。"""
        field_names = (
            ("hash", "hash_string")
            if downloader_type == "qbittorrent"
            else ("hashString", "hash_string", "hash")
        )
        value = cls.__get_field(torrent, *field_names)
        return None if value is None else str(value).strip()

    @staticmethod
    def __parse_trackers(value: Any) -> List[str]:
        """解析多行 tracker 配置并忽略空行。"""
        if isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = str(value or "").splitlines()
        return [str(item).strip() for item in values if str(item).strip()]

    def get_state(self) -> bool:
        """一次性清理插件不提供常驻运行状态。"""
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """不注册额外 API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """不注册常驻调度服务。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置表单，并只展示当前已配置的 qbittorrent/transmission 服务。"""
        try:
            configs = self._downloader_helper.get_configs()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.warning(f"获取下载器配置失败：{err}")
            configs = {}
        downloader_options = [
            {"title": config.name, "value": config.name}
            for config in configs.values()
            if config.name and str(config.type).lower() in self._supported_types
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader",
                                            "label": "下载器",
                                            "items": downloader_options,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "delete_type",
                                            "label": "是否有辅种",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "delete_torrent",
                                            "label": "删除种子",
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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "delete_file",
                                            "label": "删除文件",
                                            "items": [
                                                {"title": "是", "value": True},
                                                {"title": "否", "value": False},
                                            ],
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
                                            "model": "trackers",
                                            "rows": "3",
                                            "label": "站点tracker域名",
                                            "placeholder": "站点tracker域名，一行一个",
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
                                                "输入要删除辅种的站点tracker域名。"
                                                "保留站点没有辅种的种子，其余在其他站有辅种的种子均删除。"
                                                "（适用于某个站点不想保种了，但是可能有孤种没法直接全部删除的情况）"
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
                                                "场景一：某个站不想保种了，但是有些种子没有辅种，需要保留。"
                                                "是否有辅种=是，删除种子=是，删除文件=否。"
                                                "（保留站点没有辅种的种子，其余在其他站有辅种的种子均删除（保留文件）。）"
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
                                                "场景二：想删除某个站没有辅种的种子。"
                                                "是否有辅种=否，删除种子=是，删除文件=是。"
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
            "downloader": "",
            "delete_type": True,
            "delete_torrent": False,
            "delete_file": False,
            "onlyonce": False,
            "trackers": "",
        }

    def get_page(self) -> Optional[List[dict]]:
        """不提供详情页。"""
        return None

    def stop_service(self) -> None:
        """一次性任务不持有需要停止的常驻资源。"""
        return None
