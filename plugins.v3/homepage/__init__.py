"""HomePage V3 插件。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Tuple

from pydantic import BaseModel

from app.chain.dashboard import DashboardChain
from app.chain.storage import StorageChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import MediaType, StorageAction
from app.sdk.logging import logger
from app.sdk.services import StorageHelper
from app.sdk.utilities import StringUtils


class HomePageStatistic(BaseModel):
    """HomePage 自定义 API 的稳定返回模型。

    存储容量按已配置存储提供方的 ``usage`` 能力汇总；不再直接读取目录配置。
    不支持或读取失败的存储提供方不会阻塞其它统计项。
    """

    movie_count: int = 0
    tv_count: int = 0
    episode_count: int = 0
    user_count: int = 0
    total_storage: str = "0.0B"
    free_storage: str = "0.0B"
    used_storage: str = "0.0B"
    movie_subscribes: int = 0
    tv_subscribes: int = 0


class HomePage(_PluginBase):
    """提供媒体、订阅和存储容量摘要。"""

    plugin_name = "HomePage"
    plugin_desc = "HomePage自定义API。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/homepage.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "homepage_"
    plugin_order = 30
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件实例状态，避免多个实例共享启用标志。"""
        super().__init__()
        self._enabled = False

    def init_plugin(self, config: dict | None = None) -> None:
        """读取插件启用状态。"""
        self._enabled = bool((config or {}).get("enabled", False))

    def get_state(self) -> bool:
        """返回插件是否已启用。"""
        return self._enabled

    async def statistic(self) -> HomePageStatistic:
        """异步返回统计摘要，并将同步数据库/存储调用移出事件循环。"""
        return await asyncio.to_thread(self._collect_statistics)

    def _collect_statistics(self) -> HomePageStatistic:
        """通过宿主链和公开存储服务收集一次统计快照。"""
        movie_count = 0
        tv_count = 0
        episode_count = 0
        user_count = 0
        for media_statistic in DashboardChain().media_statistic() or []:
            movie_count += media_statistic.movie_count or 0
            tv_count += media_statistic.tv_count or 0
            episode_count += media_statistic.episode_count or 0
            user_count += media_statistic.user_count or 0

        movie_subscribes = 0
        tv_subscribes = 0
        for subscribe in SubscribeOper().list() or []:
            if subscribe.type == MediaType.MOVIE.value:
                movie_subscribes += 1
            elif subscribe.type == MediaType.TV.value:
                tv_subscribes += 1

        total_storage, free_storage = self._storage_usage()
        return HomePageStatistic(
            movie_count=movie_count,
            tv_count=tv_count,
            episode_count=episode_count,
            user_count=user_count,
            total_storage=StringUtils.str_filesize(total_storage),
            free_storage=StringUtils.str_filesize(free_storage),
            used_storage=StringUtils.str_filesize(
                max(total_storage - free_storage, 0)
            ),
            movie_subscribes=movie_subscribes,
            tv_subscribes=tv_subscribes,
        )

    @staticmethod
    def _storage_usage() -> tuple[float, float]:
        """汇总已配置存储提供方报告的总容量和可用容量。"""
        try:
            storages = StorageHelper.get_storagies()
        except Exception as error:
            logger.warning(f"读取存储配置失败，跳过容量统计：{error}")
            return 0.0, 0.0

        storage_types = dict.fromkeys(
            storage.type for storage in storages if storage.type
        )
        total_storage = 0.0
        free_storage = 0.0
        storage_chain = StorageChain()
        for storage_type in storage_types:
            try:
                result = storage_chain.manage_storage(
                    storage=storage_type,
                    action=StorageAction.USAGE.value,
                )
            except Exception as error:
                logger.warning(
                    f"读取 {storage_type} 存储容量失败，跳过该存储：{error}"
                )
                continue
            if not isinstance(result, dict) or not result.get("success"):
                continue
            usage = result.get("data") or {}
            if not isinstance(usage, dict):
                continue
            try:
                provider_total = float(usage.get("total") or 0)
                provider_free = float(usage.get("available") or 0)
            except (TypeError, ValueError):
                continue
            if provider_total < 0 or provider_free < 0:
                continue
            total_storage += provider_total
            free_storage += min(provider_free, provider_total)

        return total_storage, min(free_storage, total_storage)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """HomePage 不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册使用宿主标准 Bearer/API 兼容认证的统计接口。"""
        return [
            {
                "path": "/statistic",
                "endpoint": self.statistic,
                "methods": ["GET"],
                "auth": "bear",
                "response_model": HomePageStatistic,
                "summary": "数据统计",
                "description": "订阅数量、媒体数量和已配置存储容量统计",
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
                                "props": {"cols": 12, "md": 6},
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "success",
                                            "variant": "tonal",
                                        },
                                        "content": [
                                            {
                                                "component": "span",
                                                "text": "配置教程请参考：",
                                            },
                                            {
                                                "component": "a",
                                                "props": {
                                                    "href": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/HomePage.md",
                                                    "target": "_blank",
                                                },
                                                "text": "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/HomePage.md",
                                            },
                                        ],
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
                                            "text": "如安装完启用插件后，HomePage提示404，重启MoviePilot即可。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False}

    def get_page(self) -> List[dict]:
        """返回插件详情页中的统计卡片。"""
        statistics = self._collect_statistics()
        cards = [
            ("电影订阅", statistics.movie_subscribes),
            ("电视剧订阅", statistics.tv_subscribes),
            ("总空间", statistics.total_storage),
            ("剩余空间", statistics.free_storage),
            ("电影数量", statistics.movie_count),
            ("电视剧数量", statistics.tv_count),
            ("电影剧集数量", statistics.episode_count),
            ("用户数量", statistics.user_count),
        ]
        return [
            {
                "component": "VRow",
                "content": [self._metric_card(label, value) for label, value in cards],
            }
        ]

    @staticmethod
    def _metric_card(label: str, value: int | str) -> dict[str, Any]:
        """构造一个 Vuetify 统计卡片。"""
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": 3, "sm": 6},
            "content": [
                {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {
                            "component": "VCardText",
                            "props": {"class": "d-flex align-center"},
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "span",
                                            "props": {"class": "text-caption"},
                                            "text": label,
                                        },
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "d-flex align-center flex-wrap"
                                            },
                                            "content": [
                                                {
                                                    "component": "span",
                                                    "props": {"class": "text-h6"},
                                                    "text": value,
                                                }
                                            ],
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }

    def stop_service(self) -> None:
        """HomePage 无常驻服务需要停止。"""
        return None
