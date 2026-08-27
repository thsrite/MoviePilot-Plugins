import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


class EmbyMetaTag(_PluginBase):
    """按媒体库、媒体名称和音频信息为 Emby 媒体补充标签。"""

    # 插件名称
    plugin_name = "Emby媒体标签"
    # 插件描述
    plugin_desc = "自动给媒体库媒体添加标签。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/tag.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embymetatag_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    _enabled = False
    _rebuild = False
    _onlyonce = False
    _cron: Optional[str] = None
    _tag_confs: Optional[str] = None
    _aac_confs: Optional[str] = None
    _name_tag_confs: Optional[str] = None
    _mediaservers: List[str] = []

    _mediaserver_helper: Optional[MediaServerHelper] = None
    _emby: Any = None
    _emby_name: Optional[str] = None
    _emby_host = ""
    _emby_user: Optional[str] = None
    _emby_api_key: Optional[str] = None
    _scheduler: Optional[BackgroundScheduler] = None
    _audio_files_json: Optional[Path] = None

    _tags: Dict[str, List[str]] = {}
    _acc_tags: List[Dict[str, Any]] = []
    _media_tags: Dict[str, List[str]] = {}
    _media_type: Dict[str, List[str]] = {}
    _audio_files: Dict[str, List[str]] = {}

    def __init__(self) -> None:
        """初始化插件运行依赖与单实例任务互斥门禁。"""
        super().__init__()
        self._run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """停止旧任务后串行更新配置和媒体服务器服务门面。"""
        self.stop_service()
        with self._run_lock:
            self.__init_plugin(config)

    def __init_plugin(self, config: Optional[dict]) -> None:
        """在没有标签任务运行时重建配置和调度器。"""
        config = config or {}

        self._mediaserver_helper = MediaServerHelper()
        self._audio_files_json = self.get_data_path() / "audio_files.json"
        self._enabled = bool(config.get("enabled"))
        self._rebuild = bool(config.get("rebuild"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = config.get("cron")
        self._tag_confs = config.get("tag_confs")
        self._aac_confs = config.get("aac_confs")
        self._name_tag_confs = config.get("name_tag_confs")
        self._mediaservers = config.get("mediaservers") or []
        self._tags = self.__parse_library_tags(self._tag_confs)
        self._acc_tags = self.__parse_audio_tags(self._aac_confs)
        self._media_tags, self._media_type = self.__parse_name_tags(self._name_tag_confs)
        self._audio_files = {}

        if self._rebuild:
            self.__clear_audio_cache()
            self._rebuild = False
            self.__update_config()

        if self._enabled or self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._onlyonce:
                logger.info("Emby媒体标签服务启动，立即运行一次")
                self._scheduler.add_job(
                    self.auto_tag,
                    "date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="Emby媒体标签",
                )
                self._onlyonce = False
                self.__update_config()

            if self._cron:
                try:
                    self._scheduler.add_job(
                        func=self.auto_tag,
                        trigger=CronTrigger.from_crontab(self._cron),
                        name="Emby媒体标签",
                    )
                except Exception as err:
                    logger.error(f"定时任务配置错误：{err}")
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self) -> None:
        """保存运行时归一化后的配置，避免一次性和清理开关重复执行。"""
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "rebuild": self._rebuild,
                "cron": self._cron,
                "enabled": self._enabled,
                "tag_confs": self._tag_confs,
                "aac_confs": self._aac_confs,
                "name_tag_confs": self._name_tag_confs,
                "mediaservers": self._mediaservers,
            }
        )

    @staticmethod
    def __parse_library_tags(value: Optional[str]) -> Dict[str, List[str]]:
        """解析「媒体库#标签」配置，并保留同一媒体库的标签顺序。"""
        result: Dict[str, List[str]] = {}
        for line in (value or "").splitlines():
            if not line.strip():
                continue
            library_names, separator, tags = line.partition("#")
            if not separator:
                continue
            parsed_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            for library_name in library_names.split(","):
                library_name = library_name.strip()
                if library_name and parsed_tags:
                    result.setdefault(library_name, []).extend(parsed_tags)
        return result

    @staticmethod
    def __parse_audio_tags(value: Optional[str]) -> List[Dict[str, Any]]:
        """解析「音频正则#标签」配置。"""
        result = []
        for line in (value or "").splitlines():
            if not line.strip():
                continue
            pattern, separator, tags = line.partition("#")
            parsed_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            if separator and pattern.strip() and parsed_tags:
                result.append({"regex": pattern.strip(), "tags": parsed_tags})
        return result

    @staticmethod
    def __parse_name_tags(value: Optional[str]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """解析「媒体名称#媒体类型#标签」配置。"""
        media_tags: Dict[str, List[str]] = {}
        media_type: Dict[str, List[str]] = {}
        for line in (value or "").splitlines():
            if not line.strip():
                continue
            media_names, first_separator, rest = line.partition("#")
            media_types, second_separator, tags = rest.partition("#")
            parsed_types = [item.strip() for item in media_types.split(",") if item.strip()]
            parsed_tags = [tag.strip() for tag in tags.split(",") if tag.strip()]
            if not first_separator or not second_separator or not parsed_types or not parsed_tags:
                continue
            for media_name in media_names.split(","):
                media_name = media_name.strip()
                if media_name:
                    media_type[media_name] = parsed_types
                    media_tags.setdefault(media_name, []).extend(parsed_tags)
        return media_tags, media_type

    def __clear_audio_cache(self) -> None:
        """清理音频标签缓存文件，避免旧缓存阻止重新检查媒体。"""
        self._audio_files = {}
        if self._audio_files_json and self._audio_files_json.exists():
            self._audio_files_json.unlink()
        logger.info("媒体音频标签缓存清理完成")

    def __load_audio_cache(self) -> None:
        """加载按媒体服务器隔离的已处理媒体 ID；旧格式按空缓存重建。"""
        self._audio_files = {}
        if not self._audio_files_json or not self._audio_files_json.exists():
            return
        try:
            value = json.loads(self._audio_files_json.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                self._audio_files = {
                    str(server_name): [str(item_id) for item_id in item_ids if item_id]
                    for server_name, item_ids in value.items()
                    if server_name and isinstance(item_ids, list)
                }
        except (OSError, json.JSONDecodeError, TypeError):
            logger.warning("媒体音频标签缓存读取失败，将重新检查媒体")

    def __save_audio_cache(self) -> None:
        """按媒体服务器持久化已成功处理的媒体 ID。"""
        if not self._audio_files_json:
            return
        self._audio_files_json.write_text(
            json.dumps(self._audio_files, ensure_ascii=False),
            encoding="utf-8",
        )

    def auto_tag(self) -> bool:
        """串行执行标签任务，避免并发切换媒体服务器上下文。"""
        if not self._run_lock.acquire(blocking=False):
            logger.warning("Emby媒体标签任务正在运行，本次触发已跳过")
            return False
        try:
            self.__run_auto_tag()
            return True
        finally:
            self._run_lock.release()

    def __run_auto_tag(self) -> None:
        """为配置的媒体库、媒体名称和音频匹配结果添加标签。"""
        if not (self._tags or self._acc_tags or self._media_tags):
            logger.error("未配置Emby媒体标签")
            return
        if not self._mediaserver_helper:
            self._mediaserver_helper = MediaServerHelper()

        emby_servers = self._mediaserver_helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        self.__load_audio_cache()
        for emby_name, emby_server in emby_servers.items():
            if not emby_server.instance:
                logger.warning(f"媒体服务器 {emby_name} 未连接")
                continue
            self.__set_server_context(emby_name, emby_server)
            logger.info(f"开始处理媒体服务器 {emby_name}")
            if self._tags:
                self.__tag_libraries()
            if self._media_tags:
                self.__tag_named_media()
            if self._acc_tags:
                self.__tag_audio()
            logger.info(f"{emby_name} 媒体标签任务完成")
        self.__save_audio_cache()

    def __set_server_context(self, emby_name: str, emby_server: Any) -> None:
        """绑定当前媒体服务器实例和其原始 Emby API 连接参数。"""
        self._emby_name = emby_name
        self._emby = emby_server.instance
        config = emby_server.config.config if emby_server.config else {}
        host = str(config.get("host") or "").strip()
        if host and not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        self._emby_host = host.rstrip("/") + "/" if host else ""
        self._emby_api_key = config.get("apikey")
        self._emby_user = self._emby.get_user()

    def __tag_libraries(self) -> None:
        """遍历媒体库条目并按媒体库名称补充标签。"""
        libraries = self._emby.get_librarys() or []
        for library in libraries:
            library_tags = self._tags.get(library.name)
            if not library_tags:
                continue
            for item in self._emby.get_items(library.id) or []:
                if item and item.item_id:
                    self.__add_tags(item.title, item.item_id, library_tags, library.name)

    def __tag_named_media(self) -> None:
        """按媒体名称和类型搜索条目并补充标签。"""
        for media_name, media_tags in self._media_tags.items():
            for media_type in self._media_type.get(media_name, []):
                for media in self.__get_medias_by_name(media_name, media_type):
                    if media and media.get("Id"):
                        self.__add_tags(
                            media.get("Name") or media_name,
                            media.get("Id"),
                            media_tags,
                            "特殊媒体",
                        )

    def __tag_audio(self) -> None:
        """检查媒体音频流并为匹配正则的媒体添加标签。"""
        changed = False
        server_name = self._emby_name or ""
        server_audio_files = self._audio_files.setdefault(server_name, [])
        handled = set(server_audio_files)
        for media_item in self.__iter_media_items():
            media_item_id = str(media_item.item_id)
            if media_item_id in handled:
                continue

            item_id = media_item.item_id
            if str(media_item.item_type).casefold() not in {"movie", "电影"}:
                children = self.__get_items(media_item.item_id)
                item_id = children[0].get("Id") if children else None
            media_audio = self.__get_item_info(item_id)
            if not media_audio:
                continue

            add_tags: List[str] = []
            for audio_tag in self._acc_tags:
                try:
                    matched = any(
                        re.search(audio_tag["regex"], audio_value)
                        for audio_value in media_audio
                    )
                except (re.error, TypeError):
                    logger.warning(f"音频标签正则无效：{audio_tag.get('regex')}")
                    continue
                if matched:
                    add_tags.extend(audio_tag["tags"])

            add_tags = list(dict.fromkeys(add_tags))
            if add_tags and self.__add_tags(
                media_item.title,
                media_item.item_id,
                add_tags,
                "媒体音频",
            ):
                server_audio_files.append(media_item_id)
                handled.add(media_item_id)
                changed = True

        if changed:
            self.__save_audio_cache()

    def __iter_media_items(self):
        """从当前 Emby 实例枚举电影、剧集和音乐媒体，供音频规则使用。"""
        seen = set()
        for library in self._emby.get_librarys() or []:
            for item in self._emby.get_items(library.id) or []:
                if not item or not item.item_id or item.item_id in seen:
                    continue
                seen.add(item.item_id)
                yield item

    def __add_tags(self, item_name: str, item_id: str, media_tags: List[str], category: str) -> bool:
        """只提交缺失标签，并返回远端写入是否成功。"""
        item_tags = set(self.__get_item_tags(item_id))
        add_tags = [tag for tag in dict.fromkeys(media_tags) if tag not in item_tags]
        if not add_tags:
            logger.info(f"{category} 已有标签：{item_name} {media_tags}")
            return False

        payload = {"Tags": [{"Name": str(tag)} for tag in add_tags]}
        add_flag = self.__add_tag(item_id, payload)
        logger.info(f"{category} 添加标签：{item_name} {payload} {add_flag}")
        return add_flag

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """响应远程命令，执行一次媒体标签同步。"""
        if event:
            event_data = event.event_data
            if not isinstance(event_data, dict) or event_data.get("action") != "emby_meta_tag":
                return
            self.post_message(
                channel=event_data.get("channel"),
                title="开始添加媒体标签 ...",
                userid=event_data.get("user"),
            )
        executed = self.auto_tag()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="添加媒体标签完成！" if executed else "媒体标签任务正在运行，本次请求已跳过。",
                userid=event.event_data.get("user"),
            )

    def __request_json(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        """通过 SDK 网络门面读取 Emby JSON，并始终释放响应连接。"""
        if not self._emby_host or not self._emby_api_key:
            return None
        response = None
        try:
            request_params = dict(params or {})
            request_params["api_key"] = self._emby_api_key
            response = RequestUtils().get_res(
                url=f"{self._emby_host}{path.lstrip('/')}",
                params=request_params,
            )
            if response is None or response.status_code != 200:
                return None
            result = response.json()
            return result if isinstance(result, dict) else None
        except Exception as err:
            logger.error(f"请求 Emby 接口 {path} 出错：{err}")
            return None
        finally:
            if response is not None:
                response.close()

    def __add_tag(self, item_id: str, payload: dict) -> bool:
        """调用 Emby 标签写入接口。"""
        if not self._emby_host or not self._emby_api_key:
            return False
        response = None
        try:
            response = RequestUtils(content_type="application/json").post_res(
                url=f"{self._emby_host}emby/Items/{item_id}/Tags/Add",
                params={"api_key": self._emby_api_key},
                json=payload,
            )
            return response is not None and response.status_code == 204
        except Exception as err:
            logger.error(f"连接 Items/Id/Tags/Add 出错：{err}")
            return False
        finally:
            if response is not None:
                response.close()

    def __get_item_tags(self, item_id: str) -> List[str]:
        """获取单个 Emby 项目已有标签。"""
        if not item_id or not self._emby_user:
            return []
        item = self.__request_json(f"emby/Users/{self._emby_user}/Items/{item_id}") or {}
        tag_items = item.get("TagItems")
        if isinstance(tag_items, list):
            return [
                str(tag.get("Name"))
                for tag in tag_items
                if isinstance(tag, dict) and tag.get("Name")
            ]
        tags = item.get("Tags")
        return [str(tag) for tag in tags or [] if tag]

    def __get_items(self, item_id: str) -> List[dict]:
        """获取剧集或专辑下的首个媒体条目。"""
        if not item_id or not self._emby_user:
            return []
        result = self.__request_json(
            f"emby/Users/{self._emby_user}/Items",
            {
                "Limit": 1,
                "Recursive": "true",
                "ParentId": item_id,
                "IsFolder": "false",
            },
        )
        items = result.get("Items") if result else []
        return items if isinstance(items, list) else []

    def __get_item_info(self, item_id: Optional[str]) -> List[str]:
        """读取项目播放信息中的音频标题或语言字段。"""
        if not item_id or not self._emby_user:
            return []
        result = self.__request_json(
            f"emby/Items/{item_id}/PlaybackInfo",
            {"UserId": self._emby_user},
        )
        audio_values = []
        for source in (result or {}).get("MediaSources") or []:
            for stream in source.get("MediaStreams") or []:
                if stream.get("Type") != "Audio":
                    continue
                value = (
                    stream.get("Title")
                    or stream.get("Language")
                    or stream.get("DisplayTitle")
                    or stream.get("DisplayLanguage")
                )
                if value:
                    audio_values.append(str(value))
        return audio_values

    def __get_medias_by_name(self, media_name: str, media_type: str) -> List[dict]:
        """按媒体名称和类型搜索 Emby 条目。"""
        if not media_name or not self._emby_user:
            return []
        result = self.__request_json(
            f"emby/Users/{self._emby_user}/Items",
            {
                "IncludeItemTypes": media_type,
                "Recursive": "true",
                "SearchTerm": media_name,
            },
        )
        items = result.get("Items") if result else []
        return items if isinstance(items, list) else []

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/emby_meta_tag",
                "event": EventType.PluginAction,
                "desc": "Emby媒体标签",
                "category": "",
                "data": {"action": "emby_meta_tag"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单及默认配置。"""
        media_servers = []
        if self._mediaserver_helper:
            media_servers = [
                {"title": config.name, "value": config.name}
                for config in self._mediaserver_helper.get_configs().values()
                if config.type == "emby"
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
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "rebuild", "label": "清理媒体音频标签缓存"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VCronField",
                                "props": {
                                    "model": "cron",
                                    "label": "执行周期",
                                    "placeholder": "5位cron表达式，留空自动",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "aac_confs",
                                    "label": "媒体音频标签配置",
                                    "rows": 3,
                                    "placeholder": "cantonese|粤语|粤语Cantonese|Cantonese#标签名,标签名",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "tag_confs",
                                    "label": "媒体库标签配置",
                                    "rows": 3,
                                    "placeholder": "媒体库名,媒体库名#标签名,标签名",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "name_tag_confs",
                                    "label": "媒体名标签配置",
                                    "rows": 3,
                                    "placeholder": "媒体名称,媒体名称#Series,Movie#标签名,标签名",
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VSelect",
                                "props": {
                                    "multiple": True,
                                    "chips": True,
                                    "clearable": True,
                                    "model": "mediaservers",
                                    "label": "媒体服务器",
                                    "items": media_servers,
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "定时刷新Emby媒体库媒体，添加媒体库、媒体名（模糊匹配）自定义标签。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "rebuild": False,
            "cron": "5 1 * * *",
            "tag_confs": "",
            "name_tag_confs": "",
            "aac_confs": "",
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        """停止所有定时任务，保证插件热加载不遗留后台线程。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"退出插件失败：{err}")
