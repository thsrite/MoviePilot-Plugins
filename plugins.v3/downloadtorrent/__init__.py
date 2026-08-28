"""通过已配置站点认证和下载器服务添加种子任务。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import SitesHelper
from app.sdk.services import DownloaderHelper
from app.sdk.utilities import StringUtils


# pylint: disable=too-many-instance-attributes
class DownloadTorrent(_PluginBase):
    """通过已配置下载器添加站点种子任务。"""

    _supported_downloader_types = {"qbittorrent", "transmission"}

    # 插件名称
    plugin_name = "添加种子下载"
    # 插件描述
    plugin_desc = "选择下载器，添加种子任务。"
    # 插件图标
    plugin_icon = "download.png"
    # 插件版本
    plugin_version = "3.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloadtorrent_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    _is_paused: bool = False
    _enabled: bool = False
    _save_path: str = ""
    _mp_path: str = ""
    _downloader: str = ""
    _torrent_urls: str = ""

    def __init__(self) -> None:
        """初始化插件状态和稳定服务门面。"""
        super().__init__()
        self._downloader_helper: Optional[DownloaderHelper] = None
        self._sites_helper: Optional[SitesHelper] = None
        self._reset_state()

    def _reset_state(self) -> None:
        """为热重载建立不携带旧配置的一次性运行状态。"""
        self._is_paused = False
        self._enabled = False
        self._save_path = ""
        self._mp_path = ""
        self._downloader = ""
        self._torrent_urls = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置并在初始化结束时消费一次性种子链接。"""
        self._reset_state()
        self._downloader_helper = DownloaderHelper()
        self._sites_helper = SitesHelper()

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._is_paused = bool(config.get("is_paused"))
        self._save_path = str(config.get("save_path") or "")
        self._mp_path = str(config.get("mp_path") or "")
        self._downloader = str(config.get("downloader") or "")
        self._torrent_urls = str(config.get("torrent_urls") or "")

        pending_urls = self._pending_torrent_urls(self._torrent_urls)
        self._torrent_urls = ""
        try:
            saved = self.__update_config()
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("保存添加种子下载配置失败：%s", type(error).__name__)
            return
        if saved is False:
            logger.error("保存添加种子下载配置失败，已停止处理种子链接")
            return

        for index, torrent_url in enumerate(pending_urls, start=1):
            try:
                self.__download_torrent(torrent_url)
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error("第 %s 个种子链接处理失败：%s", index, type(error).__name__)

    @staticmethod
    def _pending_torrent_urls(value: str) -> List[str]:
        """按行拆分一次性链接并忽略空白输入。"""
        return [line.strip() for line in str(value or "").splitlines() if line.strip()]

    def __update_config(self) -> bool:
        """以一个稳定配置对象保存可持久化字段。"""
        return self.update_config(
            {
                "downloader": self._downloader,
                "save_path": self._save_path,
                "enabled": self._enabled,
                "mp_path": self._mp_path,
                "is_paused": self._is_paused,
            }
        )

    # pylint: disable=too-many-return-statements
    def __download_torrent(self, torrent_url: str) -> Tuple[Optional[str], Optional[str]]:
        """解析站点认证信息并添加一条种子任务。"""
        site_name: Optional[str] = None
        save_path = self._save_path or self._mp_path
        try:
            domain = StringUtils.get_url_domain(torrent_url)
            if not domain:
                logger.error("种子链接获取站点域名失败，跳过处理")
                return None, None

            if self._sites_helper is None:
                logger.error("站点 %s 的查询服务尚未初始化，跳过处理", domain)
                return None, None
            site = self._sites_helper.get_indexer(domain)
            cookie = site.get("cookie") if site else None
            if not cookie:
                logger.error("站点 %s 未配置可用 Cookie，跳过处理", domain)
                return None, None
            site_name = str(site.get("name") or domain)

            service = self.service_info(self._downloader)
            download_success = self.__download(
                service=service,
                content=torrent_url,
                save_path=save_path,
                cookie=cookie,
            )
            if download_success:
                logger.info("站点 %s 的种子添加下载成功，保存位置 %s", site_name, save_path)
                return site_name, f"种子添加下载成功, 保存位置 {save_path}"

            logger.error("站点 %s 的种子添加下载失败，保存位置 %s", site_name, save_path)
            return site_name, f"种子添加下载失败, 保存位置 {save_path}"
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("种子添加下载失败：%s", type(error).__name__)
            if site_name:
                return site_name, f"种子添加下载失败, 保存位置 {save_path}"
            return None, None

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Optional[Event] = None) -> None:
        """处理 `/dt` 事件，并从 V3 `arg_str` 读取种子链接。"""
        if event is None or not isinstance(event.event_data, dict):
            return

        event_data = event.event_data
        if event_data.get("action") != "download_torrent":
            return

        torrent_url = str(event_data.get("arg_str") or "").strip()
        if not torrent_url:
            logger.error("缺少种子链接参数")
            return

        site_name, result = self.__download_torrent(torrent_url)
        if not result:
            self.post_message(
                channel=event_data.get("channel"),
                title="添加种子下载失败",
                userid=event_data.get("user"),
            )
            return

        self.post_message(
            channel=event_data.get("channel"),
            title=f"{site_name} {result}",
            userid=event_data.get("user"),
        )

    def service_info(self, name: str) -> Optional[Any]:
        """从 V3 下载器服务门面取得可用服务实例。"""
        if not name:
            logger.warning("尚未配置下载器，请检查配置")
            return None
        if self._downloader_helper is None:
            logger.warning("下载器服务尚未初始化，请检查插件状态")
            return None

        service = self._downloader_helper.get_service(name)
        if not service or not service.instance:
            logger.warning("获取下载器 %s 实例失败，请检查配置", name)
            return None
        if service.instance.is_inactive():
            logger.warning("下载器 %s 未连接，请检查配置", name)
            return None
        return service

    def __download(
        self,
        service: Optional[Any],
        content: str,
        save_path: str,
        cookie: str,
    ) -> bool:
        """按服务类型调用 qBittorrent 或 Transmission 的统一添加接口。"""
        if not service or not service.instance or self._downloader_helper is None:
            return False

        downloader = service.instance
        if self._downloader_helper.is_downloader("qbittorrent", service=service):
            success, _torrent_ids = downloader.add_torrent(
                content=content,
                download_dir=save_path,
                is_paused=self._is_paused,
                cookie=cookie,
            )
            return bool(success)

        if self._downloader_helper.is_downloader("transmission", service=service):
            torrent = downloader.add_torrent(
                content=content,
                download_dir=save_path,
                is_paused=self._is_paused,
                cookie=cookie,
            )
            return bool(torrent and torrent.hashString)

        logger.error("不支持的下载器类型")
        return False

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册 `/dt` 种子下载命令。"""
        return [
            {
                "cmd": "/dt",
                "event": EventType.PluginAction,
                "desc": "种子下载",
                "category": "",
                "data": {"action": "download_torrent"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册 HTTP API。"""
        return []

    def _directory_options(self) -> List[Dict[str, str]]:
        """从系统配置投影本地下载目录，避免插件依赖内部目录帮助类。"""
        directories = self.systemconfig.get(SystemConfigKey.Directories) or []
        if not isinstance(directories, list):
            return []

        local_directories = [
            directory
            for directory in directories
            if isinstance(directory, dict)
            and directory.get("storage") == "local"
            and directory.get("download_path")
        ]
        local_directories.sort(key=lambda directory: directory.get("priority") or 0)

        options = []
        for directory in local_directories:
            download_path = str(directory["download_path"])
            options.append(
                {
                    "title": str(directory.get("name") or download_path),
                    "value": download_path,
                }
            )
        return options

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面和默认数据。"""
        downloader_helper = self._downloader_helper or DownloaderHelper()
        downloader_options = [
            {"title": config.name, "value": config.name}
            for config in downloader_helper.get_configs().values()
            if config.name
            and str(config.type or "").strip().lower()
            in self._supported_downloader_types
        ]
        dir_conf = self._directory_options()

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
                                            "model": "is_paused",
                                            "label": "暂停种子",
                                            "items": [
                                                {"title": "开启", "value": True},
                                                {"title": "不开启", "value": False},
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
                                            "model": "mp_path",
                                            "label": "MoviePilot保存路径",
                                            "items": dir_conf,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "save_path",
                                            "label": "自定义保存路径",
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
                                            "model": "torrent_urls",
                                            "rows": "3",
                                            "label": "种子链接",
                                            "placeholder": "种子链接，一行一个",
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
                                            "text": "自定义保存路径优先级高于MoviePilot保存路径。",
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
                                                "保存路径为下载器保存路径，种子链接一行一个。"
                                                "添加的种子链接需站点已在站点管理维护或公共站点。"
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
            "is_paused": False,
            "enabled": False,
            "save_path": "",
            "mp_path": "",
            "torrent_urls": "",
        }

    def get_page(self) -> List[dict]:
        """当前插件不注册详情页。"""
        return []

    def stop_service(self) -> None:
        """当前插件不持有后台服务。"""
        return None
