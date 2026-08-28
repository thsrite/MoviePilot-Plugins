"""按发布日期重排 Emby 合集媒体的入库时间。"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


@dataclass(frozen=True)
class _EmbyConnection:
    """保存一次排序任务使用的 Emby 连接参数。"""

    host: str
    user_id: str
    api_key: str


class EmbyCollectionSort(_PluginBase):
    """按发布日期重排 Emby 合集媒体的入库时间。"""

    plugin_name = "Emby合集媒体排序"
    plugin_desc = "Emby保留按照加入时间倒序的前提下，把合集中的媒体按照发布日期排序，修改加入时间已到达顺序排列的目的。"
    plugin_icon = "Element_A.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embycollectionsort_"
    plugin_order = 15
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件状态，避免热重载复用旧配置和远端连接。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._run_once = False
        self._cron: Optional[str] = None
        self._sort_type = "asc"
        self._collection_library_id: Optional[str] = None
        self._black_collection = ""
        self._mediaservers: list[str] = []
        self._mediaserver_helper: Optional[MediaServerHelper] = None
        self._connection: Optional[_EmbyConnection] = None
        # 请求帮助方法只读取当前任务的连接投影，任务结束后必须整体清空。
        self._EMBY_HOST: Optional[str] = None
        self._EMBY_USER: Optional[str] = None
        self._EMBY_APIKEY: Optional[str] = None
        self._run_lock = threading.Lock()
        self._run_once_lock = threading.Lock()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置，并把调度任务交给宿主公共服务投影。"""
        with self._run_lock:
            self.__clear_connection()
            self.__init_plugin(config)

    def __init_plugin(self, config: Optional[dict]) -> None:
        """在没有排序任务运行时更新插件配置。"""
        config = dict(config or {})

        self._enabled = bool(config.get("enabled"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._cron = self._normalize_cron(config.get("cron"))
        self._sort_type = str(config.get("sort_type") or "asc").strip()
        self._collection_library_id = str(
            config.get("collection_library_id") or ""
        ).strip() or None
        self._black_collection = str(config.get("black_collection") or "").strip()
        self._mediaservers = self._normalize_string_list(config.get("mediaservers"))
        self._mediaserver_helper = MediaServerHelper()

        if self._run_once:
            # 一次性标志由配置触发、由宿主服务消费；写回配置避免热重载重复执行。
            self._onlyonce = False
            self.__update_config()

    def get_state(self) -> bool:
        """返回插件是否启用或仍有待执行的一次性任务。"""
        return self._enabled or self._run_once

    def __update_config(self) -> None:
        """保存归一化配置，并清除已消费的一次性开关。"""
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "cron": self._cron or "",
                "enabled": self._enabled,
                "sort_type": self._sort_type,
                "collection_library_id": self._collection_library_id or "",
                "mediaservers": self._mediaservers,
                "black_collection": self._black_collection,
            }
        )

    def collection_sort(self) -> bool:
        """执行一次合集排序；已有任务运行时拒绝并发执行。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Emby合集媒体排序任务正在运行，本次触发已跳过")
            return False
        try:
            return self.__collection_sort()
        finally:
            self._run_lock.release()

    def __run_once_sort(self) -> bool:
        """消费一次性任务标志后执行排序，避免重复调度重复处理。"""
        with self._run_once_lock:
            if not self._run_once:
                return False
            self._run_once = False
        return self.collection_sort()

    def __collection_sort(self) -> bool:
        """遍历启用的 Emby 服务，并隔离每个服务的请求上下文。"""
        if not self._collection_library_id:
            logger.error("未配置合集所在媒体库")
            return False

        helper = self._mediaserver_helper or MediaServerHelper()
        try:
            emby_servers = helper.get_services(
                name_filters=self._mediaservers,
                type_filter="emby",
            )
        except (RuntimeError, TypeError, AttributeError) as error:
            logger.error("获取Emby媒体服务器失败：%s", error)
            return False
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return False

        processed_server = False
        for emby_name, emby_server in emby_servers.items():
            connection = self.__select_server(emby_server)
            if connection is None:
                logger.warning("媒体服务器 %s 缺少有效连接配置，已跳过", emby_name)
                continue

            processed_server = True
            try:
                logger.info("开始处理媒体服务器 %s", emby_name)
                # 不同 Emby 实例的时间线彼此独立，不能互相占用时间点。
                handled_times: set[datetime] = set()
                collections = self.__get_items(self._collection_library_id)
                for collection in collections:
                    self.__sort_collection(collection, handled_times)
                logger.info("更新 %s 合集媒体排序完成", emby_name)
            finally:
                self.__clear_connection()

        return processed_server

    def __sort_collection(
        self, collection: dict, handled_times: set[datetime]
    ) -> None:
        """读取一个合集并提交按发布日期生成的入库时间。"""
        collection_name = str(collection.get("Name") or "")
        collection_id = collection.get("Id")
        if not collection_id:
            logger.warning("合集 %s 缺少有效 ID，已跳过", collection_name)
            return
        if self._is_blacklisted(collection_name, self._black_collection):
            logger.info("跳过黑名单合集: %s %s", collection_name, collection_id)
            return

        try:
            logger.info("开始处理合集: %s %s", collection_name, collection_id)
            sorted_items = self.__sorted_items(self.__get_items(collection_id))
            if not sorted_items:
                logger.info("合集 %s 没有可排序的有效媒体", collection_name)
                return

            # 用合集当前最大的入库时间作为新序列起点，保持 Emby 倒序列表的顺序。
            current_time = max(item["created"] for item in sorted_items)
            updated_items: list[dict[str, Any]] = []
            for item in sorted_items:
                while current_time in handled_times:
                    current_time += timedelta(seconds=1)
                new_date_created = self.__format_emby_datetime(current_time)
                item_info = item["item_info"]
                if item_info.get("DateCreated") != new_date_created:
                    updated_info = dict(item_info)
                    updated_info["DateCreated"] = new_date_created
                    updated_items.append(updated_info)
                    logger.debug(
                        "合集媒体: %s 原入库时间 %s 新入库时间 %s",
                        item.get("Name"),
                        item.get("original_date_created"),
                        new_date_created,
                    )
                handled_times.add(current_time)
                current_time -= timedelta(seconds=1)

            if not updated_items:
                logger.warning("合集: %s %s 无需更新入库时间", collection_name, collection_id)
                return

            for item_info in updated_items:
                item_id = item_info.get("Id")
                update_flag = self.__update_item_info(item_id, item_info)
                if update_flag:
                    logger.info(
                        "%s 更新入库时间到%s成功",
                        item_info.get("Name"),
                        item_info.get("DateCreated"),
                    )
                else:
                    logger.error(
                        "%s 更新入库时间到%s失败",
                        item_info.get("Name"),
                        item_info.get("DateCreated"),
                    )
            logger.info("合集处理完成: %s %s", collection_name, collection_id)
        except Exception as error:  # 外部 Emby 单个合集异常不应中断其它合集
            logger.error("处理合集 %s %s 失败: %s", collection_name, collection_id, error)

    @staticmethod
    def _is_blacklisted(collection_name: str, blacklist: Any = None) -> bool:
        """判断合集名称是否命中逗号分隔的黑名单。"""
        if not blacklist:
            return False
        keywords = (
            blacklist
            if isinstance(blacklist, (list, tuple, set))
            else str(blacklist).split(",")
        )
        return any(
            str(keyword).strip() and str(keyword).strip() in collection_name
            for keyword in keywords
        )

    def __sorted_items(self, items: Optional[list[dict]]) -> list[dict[str, Any]]:
        """过滤缺少日期的条目，并按 Emby 首映日期排序。"""
        result: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item_id = item.get("Id")
            item_info = self.__get_item_info(item_id)
            if not isinstance(item_info, dict):
                item_info = {}
            premiere = self.__parse_emby_datetime(item_info.get("PremiereDate"))
            created = self.__parse_emby_datetime(item_info.get("DateCreated"))
            if not item_id or not premiere or not created:
                logger.warning(
                    "媒体 %s 缺少有效 PremiereDate/DateCreated，已跳过",
                    item.get("Name"),
                )
                continue
            result.append(
                {
                    "Name": item.get("Name"),
                    "Id": item_id,
                    "item_info": item_info,
                    "premiere": premiere,
                    "created": created,
                    "original_date_created": item_info.get("DateCreated"),
                }
            )
        return sorted(
            result,
            key=lambda item: item["premiere"],
            reverse=str(self._sort_type).strip().lower()
            in {"降序", "desc", "descending"},
        )

    def __select_server(self, service: Any) -> Optional[_EmbyConnection]:
        """从 V3 服务投影提取当前 Emby 的地址、用户和 API key。"""
        if not service or not service.config:
            return None
        config = service.config.config
        if not isinstance(config, dict):
            return None
        host = self._normalize_host(config.get("host"))
        api_key = str(config.get("apikey") or "").strip()
        instance = service.instance
        if not host or not api_key or not instance:
            return None
        try:
            user = instance.get_user()
        except (AttributeError, TypeError, ValueError) as error:
            logger.warning("获取 Emby 用户 ID 失败：%s", error)
            return None
        user_id = str(user or "").strip()
        if not user_id:
            return None

        connection = _EmbyConnection(host=host, user_id=user_id, api_key=api_key)
        self._connection = connection
        self._EMBY_HOST = connection.host
        self._EMBY_USER = connection.user_id
        self._EMBY_APIKEY = connection.api_key
        return connection

    def __clear_connection(self) -> None:
        """清理一次媒体服务器任务结束后的连接投影。"""
        self._connection = None
        self._EMBY_HOST = None
        self._EMBY_USER = None
        self._EMBY_APIKEY = None

    @staticmethod
    def _normalize_host(value: Any) -> str:
        """把 Emby 地址规范化为不带尾部斜杠的 HTTP 基地址。"""
        host = str(value or "").strip()
        if not host:
            return ""
        if not host.lower().startswith(("http://", "https://")):
            host = f"http://{host}"
        return host.rstrip("/")

    @staticmethod
    def __parse_emby_datetime(value: Any) -> Optional[datetime]:
        """解析 Emby ISO 时间，兼容七位小数秒和 UTC 偏移。"""
        text = str(value or "").strip()
        if not text:
            return None
        match = re.match(
            r"^(?P<date>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
            r"(?:\.(?P<fraction>\d+))?(?P<zone>Z|[+-]\d{2}:?\d{2})?$",
            text,
        )
        if not match:
            return None
        fraction = (match.group("fraction") or "")[:6]
        normalized = match.group("date")
        if fraction:
            normalized += f".{fraction.ljust(6, '0')}"
        zone = match.group("zone")
        if zone:
            normalized += "+00:00" if zone == "Z" else zone
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    @staticmethod
    def __format_emby_datetime(value: datetime) -> str:
        """将排序时间写回 Emby 接受的七位小数秒格式。"""
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f0Z")

    @staticmethod
    def __event_data(event: Optional[Event]) -> dict[str, Any]:
        """读取字典或 V3 类型化事件中的动作字段。"""
        if event is None:
            return {}
        payload = event.event_data
        if isinstance(payload, dict):
            return payload
        if payload is None:
            return {}
        model_dump = getattr(payload, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="python")
            return dumped if isinstance(dumped, dict) else {}
        return {
            key: getattr(payload, key, None)
            for key in ("action", "channel", "user")
        }

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Optional[Event] = None) -> None:
        """响应远程命令并执行一次合集排序。"""
        event_data = self.__event_data(event)
        if event is not None and event_data.get("action") != "collection_sort":
            return
        if event is not None:
            self.post_message(
                channel=event_data.get("channel"),
                title="开始更新Emby合集媒体排序 ...",
                userid=event_data.get("user"),
            )
        success = self.collection_sort()
        if event is not None:
            self.post_message(
                channel=event_data.get("channel"),
                title="更新Emby合集媒体排序完成！" if success else "更新Emby合集媒体排序失败！",
                userid=event_data.get("user"),
            )

    def __request_json(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> Optional[dict]:
        """通过 V3 网络 SDK 读取 Emby JSON，并始终释放响应连接。"""
        if not self._EMBY_HOST or not self._EMBY_USER or not self._EMBY_APIKEY:
            return None
        response = None
        try:
            request_params = dict(params or {})
            request_params["api_key"] = self._EMBY_APIKEY
            response = RequestUtils().get_res(
                url=f"{self._EMBY_HOST}/{path.lstrip('/')}",
                params=request_params,
            )
            if response is None or response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None
        except Exception as error:  # 外部 Emby 响应异常按空结果处理
            logger.warning("请求 Emby 接口 %s 出错：%s", path, error)
            return None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def __post_json(self, path: str, payload: dict[str, Any]) -> bool:
        """通过 V3 网络 SDK 写入 Emby JSON，并始终释放响应连接。"""
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        response = None
        try:
            response = RequestUtils(content_type="application/json").post_res(
                url=f"{self._EMBY_HOST}/{path.lstrip('/')}",
                params={"api_key": self._EMBY_APIKEY},
                json=payload,
            )
            return response is not None and response.status_code in (200, 204)
        except Exception as error:  # 外部 Emby 写入异常按失败处理
            logger.warning("请求 Emby 接口 %s 出错：%s", path, error)
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def __get_items(self, parent_id: Any) -> list[dict]:
        """读取 Emby 父条目下的媒体列表。"""
        if not parent_id or not self._EMBY_USER:
            return []
        result = self.__request_json(
            f"emby/Users/{self._EMBY_USER}/Items",
            {"ParentId": parent_id},
        )
        items = result.get("Items") if result else []
        return (
            [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else []
        )

    def __get_item_info(self, item_id: Any) -> dict[str, Any]:
        """读取单个 Emby 条目详情。"""
        if not item_id or not self._EMBY_USER:
            return {}
        return self.__request_json(
            f"emby/Users/{self._EMBY_USER}/Items/{item_id}"
        ) or {}

    def __update_item_info(self, item_id: Any, data: dict[str, Any]) -> bool:
        """更新 Emby 条目的入库时间。"""
        if not item_id:
            return False
        return self.__post_json(f"emby/Items/{item_id}", data)

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程合集排序命令。"""
        return [
            {
                "cmd": "/collection_sort",
                "event": EventType.PluginAction,
                "desc": "更新Emby合集媒体排序",
                "category": "",
                "data": {"action": "collection_sort"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """本插件不暴露额外 HTTP API。"""
        return []

    def get_service(self) -> list[dict[str, Any]]:
        """按 V3 公共服务合同注册一次性和周期排序任务。"""
        services: list[dict[str, Any]] = []
        if self._run_once:
            services.append(
                {
                    "id": "EmbyCollectionSort.Once",
                    "name": "Emby合集媒体排序（立即运行）",
                    "trigger": "date",
                    "func": self.__run_once_sort,
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
                        "id": "EmbyCollectionSort",
                        "name": "Emby合集媒体排序",
                        "trigger": CronTrigger.from_crontab(
                            self._cron,
                            timezone=pytz.timezone(str(settings.TZ)),
                        ),
                        "func": self.collection_sort,
                        "kwargs": {},
                    }
                )
            except (TypeError, ValueError) as error:
                logger.error("定时任务配置错误：%s", error)
                self.systemmessage.put(f"执行周期配置错误：{error}")
        return services

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回插件配置表单和默认配置。"""
        media_server_items: list[dict[str, str]] = []
        helper = self._mediaserver_helper or MediaServerHelper()
        try:
            media_server_items = [
                {"title": config.name, "value": config.name}
                for config in helper.get_configs().values()
                if config.type == "emby"
            ]
        except (RuntimeError, TypeError, AttributeError):
            # 配置组合根尚未装配时仍返回静态表单。
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "collection_library_id", "label": "合集媒体库ID"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sort_type",
                                            "label": "发布日期",
                                            "items": [
                                                {"title": "升序", "value": "升序"},
                                                {"title": "降序", "value": "降序"},
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
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "black_collection",
                                            "label": "黑名单合集名称",
                                            "placeholder": "多个名称用英文逗号分隔",
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
                                            "text": "保留按照加入时间倒序的前提下，把合集中的媒体放一块，不用到处找。注：只支持Emby。",
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
            "sort_type": "降序",
            "cron": "5 1 * * *",
            "collection_library_id": "",
            "black_collection": "",
            "mediaservers": [],
        }

    def get_page(self) -> list[dict]:
        """本插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """释放插件自有的连接投影；公共调度任务由宿主统一撤销。"""
        with self._run_lock:
            self.__clear_connection()

    @staticmethod
    def _normalize_cron(value: Any) -> Optional[str]:
        """规范化可选五段 cron 文本，空值表示不注册周期任务。"""
        normalized = " ".join(str(value or "").split())
        return normalized or None

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        """把配置中的字符串或列表归一为非空服务名称列表。"""
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]
