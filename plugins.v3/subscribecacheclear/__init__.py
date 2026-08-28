from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import MediaType
from app.sdk.logging import logger


class SubscribeCacheClear(_PluginBase):
    """清理电视剧订阅的已下载集数缓存。"""

    plugin_name = "清理订阅缓存"
    plugin_desc = "清理订阅已下载集数。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/broom.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "subscribecacheclear_"
    plugin_order = 28
    auth_level = 2

    def __init__(self) -> None:
        """初始化订阅访问句柄，避免实例间共享配置状态。"""
        super().__init__()
        self._subscribe_oper = SubscribeOper()
        self._subscribe_ids: list[int] = []

    def init_plugin(self, config: dict | None = None) -> None:
        """按配置清空选中电视剧订阅的已下载集数记录。"""
        config = config or {}
        self._subscribe_ids = config.get("subscribe_ids") or []
        if not self._subscribe_ids:
            return

        for subscribe_id in self._subscribe_ids:
            subscribe = self._subscribe_oper.update(subscribe_id, {"note": ""})
            if subscribe:
                logger.info(f"订阅 {subscribe_id} 下载缓存已清理")
            else:
                logger.warning(f"订阅 {subscribe_id} 不存在，跳过下载缓存清理")

        self.update_config({"subscribe_ids": []})

    def get_state(self) -> bool:
        """该插件只在保存配置时执行一次清理，不保持后台运行状态。"""
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册 HTTP API。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回电视剧订阅选择表单及其默认配置。"""
        subscribe_options = [
            {"title": subscribe.name, "value": subscribe.id}
            for subscribe in self._subscribe_oper.list("R")
            if subscribe.type == MediaType.TV.value
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
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "chips": True,
                                            "multiple": True,
                                            "model": "subscribe_ids",
                                            "label": "电视剧订阅",
                                            "items": subscribe_options,
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
                                                "请选择需要清理缓存的订阅，用于清理该订阅已下载集数。"
                                                "注意！！！未入库的会被重新下载。"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"subscribe_ids": []}

    def get_page(self) -> List[dict]:
        """当前插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """当前插件没有需要停止的后台服务。"""
        return None
