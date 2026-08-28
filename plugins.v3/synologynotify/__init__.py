from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.logging import logger


class SynologyNotify(_PluginBase):
    """接收群晖 Webhook 消息并转发到 MoviePilot 通知中心。"""

    plugin_name = "群辉Webhook通知"
    plugin_desc = "接收群辉webhook通知并推送。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/synology.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "synologynotify_"
    plugin_order = 30
    auth_level = 1

    def __init__(self) -> None:
        """初始化实例状态，避免热重载复用旧配置。"""
        super().__init__()
        self._enabled = False
        self._notify = False
        self._msgtype = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取 Webhook 转发开关和通知类型配置。"""
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._msgtype = config.get("msgtype") or ""

    def _get_message_type(self) -> MessageType:
        """把配置中的消息类型名称解析为 V3 消息枚举。"""
        if isinstance(self._msgtype, MessageType):
            return self._msgtype
        return MessageType.__members__.get(
            str(self._msgtype), MessageType.Manual
        )

    def send_notify(
        self,
        text: Optional[str] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        url: Optional[str] = None,
    ) -> schemas.Response[None]:
        """接收 Synology Webhook 参数并按插件配置转发通知。"""
        logger.info(
            "收到群辉 webhook 消息，字段状态 text=%s title=%s content=%s url=%s",
            bool(text),
            bool(title),
            bool(content),
            bool(url),
        )
        if not text and not title and not content:
            return schemas.Response(success=False, message="消息内容不能为空")

        if self._enabled and self._notify:
            message_text = text or content or ""
            if not text and url:
                message_text = f"{message_text}\n[查看详情]({url})"
            self.post_message(
                title="群辉通知" if text else title,
                mtype=self._get_message_type(),
                text=message_text,
            )

        return schemas.Response(success=True, message="发送成功")

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册供 Synology 调用的 Webhook API。"""
        return [
            {
                "path": "/webhook",
                "endpoint": self.send_notify,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "群辉webhook",
                "description": "接受群辉webhook通知并推送",
                "response_model": schemas.Response[None],
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回 Webhook 开关和通知类型配置表单。"""
        message_type_options = [
            {"title": item.value, "value": item.name} for item in MessageType
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
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "开启通知",
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
                                            "multiple": False,
                                            "chips": True,
                                            "model": "msgtype",
                                            "label": "消息类型",
                                            "items": message_type_options,
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
                                                "群辉webhook配置"
                                                "http://ip:3001/api/v1/plugin/SynologyNotify/webhook?apikey=*****&text=hello world。"
                                                "text参数类型是消息内容。此插件安装完需要重启生效api。消息类型默认为手动处理通知。"
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
                                            "text": "如安装完插件后，群晖发送webhook提示404，重启MoviePilot即可。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"enabled": False, "notify": False, "msgtype": ""}

    def get_page(self) -> List[dict]:
        """当前插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """当前插件没有需要停止的后台服务。"""
        return None
