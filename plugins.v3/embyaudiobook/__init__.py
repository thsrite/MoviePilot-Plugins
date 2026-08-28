"""Emby 有声书媒体信息整理插件的 V3 实现。"""

from __future__ import annotations

import datetime
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.schemas.types import EventType, MessageType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


# 配置、调度器和当前 Emby 请求上下文属于同一个插件生命周期，不能拆成互相独立的全局状态。
# pylint: disable=too-many-instance-attributes
class EmbyAudioBook(_PluginBase):
    """同步 Emby 有声书的专辑和剧集信息，并支持交互修正作者。"""

    plugin_name = "Emby有声书整理"
    plugin_desc = "还在为Emby有声书整理烦恼吗？入库存在很多单集？"
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/audiobook.png"
    )
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embyaudiobook_"
    plugin_order = 30
    auth_level = 1

    _metadata_fields = (
        "Album",
        "AlbumId",
        "AlbumPrimaryImageTag",
        "Artists",
        "ArtistItems",
        "Composers",
        "AlbumArtist",
        "AlbumArtists",
        "ParentIndexNumber",
    )

    def __init__(self) -> None:
        """初始化实例级状态，避免热重载复用上一份调度器或服务器上下文。"""
        super().__init__()
        self._run_lock = threading.Lock()
        self._run_once_lock = threading.Lock()
        self._enabled = False
        self._notify = False
        self._rename = False
        self._onlyonce = False
        self._run_once = False
        self._cron = ""
        self._library_id = ""
        self._msgtype: Optional[str] = None
        self._mediaservers: List[str] = []
        self._mediaserver_helper = MediaServerHelper()
        self._emby_host = ""
        self._emby_user = ""
        self._emby_api_key = ""

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """停止旧任务、读取配置并按开关重建后台调度器。"""
        self.stop_service()
        with self._run_lock:
            self.__init_plugin(config)

    def __init_plugin(self, config: Optional[dict]) -> None:
        """在没有整理任务运行时更新配置和调度器。"""
        config = dict(config or {})
        self._mediaserver_helper = MediaServerHelper()
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._rename = bool(config.get("rename"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._run_once = self._onlyonce
        self._cron = str(config.get("cron") or "").strip()
        self._library_id = str(config.get("library_id") or "").strip()
        self._msgtype = config.get("msgtype") or MessageType.Manual.name
        self._mediaservers = self._normalize_string_list(config.get("mediaservers"))

        if self._run_once:
            logger.info("Emby有声书整理服务启动，立即运行一次")
            self._onlyonce = False
            self.__update_config()

    @staticmethod
    def _normalize_string_list(value: Any) -> List[str]:
        """把配置中的媒体服务器列表规范为去重后的非空字符串。"""
        if isinstance(value, str):
            values = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            values = value
        else:
            values = []
        result = []
        for item in values:
            item = str(item or "").strip()
            if item and item not in result:
                result.append(item)
        return result

    def __update_config(self) -> None:
        """保存一次性开关归零后的插件配置。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "library_id": self._library_id,
                "rename": self._rename,
                "cron": self._cron,
                "notify": self._notify,
                "msgtype": self._msgtype,
                "mediaservers": self._mediaservers,
            }
        )

    def get_state(self) -> bool:
        """返回插件是否启用或仍有待消费的一次性任务。"""
        return self._enabled or self._run_once

    def _run_once_check(self) -> None:
        """消费一次性服务标记，再执行一轮有声书整理。"""
        with self._run_once_lock:
            if not self._run_once:
                return
            self._run_once = False
        self.check()

    def check(self) -> None:
        """串行执行一次全量检查，避免并发切换共享 Emby 请求上下文。"""
        if not self._run_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            logger.warning("Emby有声书整理任务正在运行，本次触发已跳过")
            return
        try:
            self.__check()
        finally:
            self._run_lock.release()

    def __check(self) -> None:  # pylint: disable=too-many-branches
        """检查配置媒体库中的有声书，并锁定已经整理完成的专辑。"""
        if not self._library_id:
            logger.error("请设置有声书文件夹ID！")
            return

        emby_servers = self._mediaserver_helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            if not emby_server.instance:
                logger.warning(f"Emby媒体服务器 {emby_name} 未连接")
                continue
            if not self.__set_server_context(emby_server):
                logger.warning(f"Emby媒体服务器 {emby_name} 配置不完整")
                continue

            logger.info(f"开始处理媒体服务器 {emby_name}")
            books = self.__get_items(self._library_id)
            if not books:
                logger.error(f"获取媒体库 {self._library_id} 有声书列表失败！")
                continue

            for book in books:
                if not isinstance(book, dict):
                    continue
                book_id = book.get("Id")
                book_name = book.get("Name") or book_id
                if not book_id:
                    logger.warning(f"媒体服务器 {emby_name} 存在缺少 ID 的有声书，已跳过")
                    continue
                episodes = self.__get_items(book_id)
                if not episodes:
                    logger.error(f"获取 {book_name} {book_id} 有声书失败！")
                    continue

                needs_organize = any(not episode.get("AlbumId") for episode in episodes)
                if needs_organize:
                    logger.info(f"有声书 {book_name} 需要整理，共 {len(episodes)} 集")
                    if self._notify:
                        self.post_message(
                            title="Emby有声书整理",
                            mtype=self._message_type(),
                            text=f"有声书 {book_name} 需要整理，共 {len(episodes)} 集",
                        )
                    continue

                book_info = self.__get_item_info(book_id)
                if not book_info:
                    logger.warning(f"获取有声书 {book_name} 详情失败，未锁定")
                    continue
                book_info["LockData"] = True
                updated = self.__update_item_info(book_id, book_info)
                logger.info(
                    f"有声书 {book_name} 不需要整理，已{'锁定' if updated else '尝试锁定'}"
                )

            logger.info(f"{emby_name} 有声书整理服务执行完毕")

    def __set_server_context(self, emby_server: Any) -> bool:
        """读取当前服务的 Emby 地址、用户和 API key，并规范化地址。"""
        self._emby_host = ""
        self._emby_user = ""
        self._emby_api_key = ""
        if not emby_server.instance:
            return False
        server_config = (
            emby_server.config.config
            if emby_server.config and emby_server.config.config
            else {}
        )
        if not isinstance(server_config, Mapping):
            return False

        host = self._normalize_host(server_config.get("host"))
        api_key = str(server_config.get("apikey") or "").strip()
        try:
            user = str(emby_server.instance.get_user() or "").strip()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"读取 Emby 用户失败：{err}")
            return False
        if not host or not api_key or not user:
            return False

        self._emby_host = host
        self._emby_user = user
        self._emby_api_key = api_key
        return True

    @staticmethod
    def _normalize_host(host: Any) -> str:
        """把 Emby 地址规范为带单个尾斜杠的 HTTP URL。"""
        host = str(host or "").strip()
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return host.rstrip("/") + "/"

    @staticmethod
    def _event_data(event: Optional[Event], action: str) -> Optional[Dict[str, Any]]:
        """读取并校验插件动作载荷，拒绝其他动作或非映射对象。"""
        if not event or not isinstance(event.event_data, Mapping):
            return None
        data = dict(event.event_data)
        return data if data.get("action") == action else None

    def _command_args(self, data: Mapping[str, Any]) -> Optional[List[str]]:
        """按远程命令约定解析媒体库、书名和修正参数。"""
        raw_args = str(data.get("arg_str") or "").strip()
        if not raw_args:
            return None

        # 媒体服务器和书名允许包含空格；优先使用已配置服务名确定第一段边界，
        # 再从右侧拆出集数或演播作者，避免把合法名称误拆成多个参数。
        servers = self._mediaserver_helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        for server_name in sorted(
            (str(name).strip() for name in (servers or {})),
            key=len,
            reverse=True,
        ):
            prefix = f"{server_name} "
            if raw_args.casefold().startswith(prefix.casefold()):
                remainder = raw_args[len(prefix) :].strip()
                parts = remainder.rsplit(maxsplit=1)
                if len(parts) == 2 and all(parts):
                    return [server_name, parts[0], parts[1]]

        args = raw_args.split()
        return args if len(args) == 3 else None

    def _message_type(self) -> MessageType:
        """把配置中的消息类型名称或旧值转换为 V3 消息枚举。"""
        value = self._msgtype
        if isinstance(value, MessageType):
            return value
        for message_type in MessageType:
            if value in (message_type.name, message_type.value):
                return message_type
        return MessageType.Manual

    def _send_command_message(
        self,
        data: Mapping[str, Any],
        title: str,
        text: Optional[str] = None,
    ) -> None:
        """向触发命令的渠道回复结果，并沿用宿主消息类型配置。"""
        self.post_message(
            channel=data.get("channel"),
            mtype=self._message_type(),
            title=title,
            text=text,
            userid=data.get("user"),
        )

    @eventmanager.register(EventType.PluginAction)
    def audiobook(self, event: Optional[Event] = None) -> None:
        """响应 `/ab 媒体库 书名 正确信息集数` 命令。"""
        data = self._event_data(event, "audiobook")
        if data is None or not self._enabled:
            return
        if not self._run_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            logger.warning("Emby有声书整理任务正在运行，本次命令已跳过")
            return
        try:
            self.__handle_audiobook(data)
        finally:
            self._run_lock.release()

    @eventmanager.register(EventType.PluginAction)
    def audiobook_artist(self, event: Optional[Event] = None) -> None:
        """响应 `/aba 媒体库 书名 正确的演播作者名称` 命令。"""
        data = self._event_data(event, "audiobook_artist")
        if data is None or not self._enabled:
            return
        if not self._run_lock.acquire(  # pylint: disable=consider-using-with
            blocking=False
        ):
            logger.warning("Emby有声书整理任务正在运行，本次命令已跳过")
            return
        try:
            self.__handle_audiobook_artist(data)
        finally:
            self._run_lock.release()

    def __handle_audiobook(  # pylint: disable=too-many-branches,too-many-locals,too-many-return-statements,too-many-statements
        self, data: Mapping[str, Any]
    ) -> None:
        """从指定剧集提取信息并同步到整本有声书。"""
        if not self._library_id:
            logger.error("请设置有声书文件夹ID！")
            self._send_command_message(data, "请设置有声书文件夹ID！")
            return

        args = self._command_args(data)
        if args is None:
            logger.error(f"参数错误：{data.get('arg_str')}")
            self._send_command_message(data, "参数错误！ /ab 媒体库 书名 正确信息集数")
            return
        library_name, book_name, book_index_text = args
        try:
            book_index = int(book_index_text)
        except ValueError:
            self._send_command_message(data, "集数必须是数字！")
            return

        server = self.__find_server(library_name)
        if server is None:
            self._send_command_message(data, f"未找到 {library_name} Emby媒体服务器！")
            return
        if not self.__set_server_context(server):
            self._send_command_message(data, f"Emby媒体服务器 {library_name} 配置不完整！")
            return

        books = self.__get_items(self._library_id)
        book = self.__find_book(books, book_name)
        if book is None:
            logger.error(f"未找到 {book_name} 有声书！")
            self._send_command_message(data, f"未找到 {book_name} 有声书！")
            return

        book_id = book.get("Id")
        episodes = self.__get_items(book_id)
        if not book_id or not episodes:
            logger.error(f"获取 {book_name} {book_id} 有声书失败！")
            self._send_command_message(data, f"获取 {book_name} 有声书失败！")
            return

        if not self.__organize_items(episodes, book_index):
            self._send_command_message(data, f"{book_name} 有声书整理失败！")
            return

        book_info = self.__get_item_info(book_id)
        if book_info:
            book_info["LockData"] = True
            self.__update_item_info(book_id, book_info)
        self._send_command_message(data, f"{book_name} 有声书整理完成！")

    def __handle_audiobook_artist(  # pylint: disable=too-many-branches,too-many-locals,too-many-return-statements,too-many-statements
        self, data: Mapping[str, Any]
    ) -> None:
        """把指定演播作者写入有声书及其全部剧集。"""
        if not self._library_id:
            logger.error("请设置有声书文件夹ID！")
            self._send_command_message(data, "请设置有声书文件夹ID！")
            return

        args = self._command_args(data)
        if args is None:
            logger.error(f"参数错误：{data.get('arg_str')}")
            self._send_command_message(
                data,
                "参数错误！ /aba 媒体库 书名 正确的演播作者名称",
            )
            return
        library_name, book_name, book_artist = args
        server = self.__find_server(library_name)
        if server is None:
            self._send_command_message(data, f"未找到 {library_name} Emby媒体服务器！")
            return
        if not self.__set_server_context(server):
            self._send_command_message(data, f"Emby媒体服务器 {library_name} 配置不完整！")
            return

        books = self.__get_items(self._library_id)
        book = self.__find_book(books, book_name)
        if book is None or not book.get("Id"):
            logger.error(f"未找到 {book_name} 有声书！")
            self._send_command_message(data, f"未找到 {book_name} 有声书！")
            return
        book_id = book["Id"]
        book_info = self.__get_item_info(book_id)
        if not book_info:
            self._send_command_message(data, f"获取 {book_name} 有声书详情失败！")
            return

        artist = next(
            (
            item
            for item in self.__get_artists()
            if isinstance(item, Mapping)
            if str(item.get("Name") or "") == book_artist
            ),
            None,
        )
        if not artist or not artist.get("Id"):
            logger.error(f"未找到 {book_artist} 作者！")
            self._send_command_message(data, f"未找到 {book_artist} 作者！")
            return

        artist_item = {"Id": artist["Id"], "Name": book_artist}
        old_artists = [
            item
            for item in self.__artist_items(book_info)
            if item.get("Name") != book_artist
        ]
        self.__set_artist_fields(book_info, artist_item)
        book_info["LockData"] = True
        if not self.__update_item_info(book_id, book_info):
            logger.error(f"更新 {book_name} 作者信息-> {book_artist} 失败！")
            self._send_command_message(data, f"更新 {book_name} 作者信息-> {book_artist} 失败！")
            return

        episodes = self.__get_items(book_id)
        if not episodes:
            logger.error(f"获取有声书 {book_name} 剧集失败！")
            self._send_command_message(data, f"获取有声书 {book_name} 剧集失败！")
            return

        failed = False
        for episode in episodes:
            episode_id = episode.get("Id") if isinstance(episode, dict) else None
            episode_info = self.__get_item_info(episode_id)
            if not episode_info:
                failed = True
                logger.error(f"获取有声书 {book_name} 剧集 {episode_id} 详情失败！")
                continue
            self.__set_artist_fields(episode_info, artist_item)
            episode_info["LockData"] = True
            updated = self.__update_item_info(episode_id, episode_info)
            failed = failed or not updated
            logger.info(
                f"更新 {book_name} 剧集 {episode.get('Name')} 作者信息-> {book_artist} "
                f"{'成功' if updated else '失败'}！"
            )

        for old_artist in old_artists:
            old_id = old_artist.get("Id")
            if not old_id:
                continue
            deleted = self.__delete_by_id(old_id)
            logger.info(
                f"删除 {book_name} 原作者信息-> {old_artist.get('Name')} "
                f"{'成功' if deleted else '失败'}！"
            )

        if failed:
            self._send_command_message(data, f"更新 {book_name} 作者信息-> {book_artist} 部分失败！")
        else:
            self._send_command_message(data, f"更新 {book_name} 作者信息-> {book_artist} 成功！")

    @staticmethod
    def __find_book(items: List[dict], book_name: str) -> Optional[dict]:
        """从媒体库项目中按名称片段选择有声书。"""
        return next(
            (
                item
                for item in items
                if isinstance(item, dict)
                and book_name in str(item.get("Name") or "")
            ),
            None,
        )

    def __find_server(self, name: str) -> Any:
        """按交互命令中的媒体库名称选择 Emby 服务。"""
        servers = self._mediaserver_helper.get_services(
            name_filters=self._mediaservers,
            type_filter="emby",
        )
        name_casefold = name.casefold()
        return next(
            (
                server
                for server_name, server in (servers or {}).items()
                if str(server_name).casefold() == name_casefold
            ),
            None,
        )

    def __organize_items(self, items: List[dict], book_index: int) -> bool:
        """从指定剧集提取元数据并写回全部剧集，返回是否全部成功。"""
        source = self.__source_item(items, book_index)
        if source is None:
            logger.error("未找到可用的有声书信息来源剧集")
            return False

        source_metadata = {
            key: source.get(key) for key in self._metadata_fields
        }
        failed = False
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict) or not item.get("Id"):
                failed = True
                continue
            episode = self.__episode_number(item, index)
            metadata_matches = all(
                source_metadata[key] == item.get(key) for key in self._metadata_fields
            )
            if metadata_matches and not self._rename:
                logger.info(f"有声书 第{episode}集 {item.get('Name')} 信息完整，跳过！")
                continue

            item_info = None
            for retry in range(3):
                item_info = self.__get_item_info(item["Id"])
                if item_info:
                    break
                logger.error(
                    f"更新有声书 第{episode}集 {item.get('Name')} 信息出错，"
                    f"开始重试...{retry + 1} / 3"
                )
            if not item_info:
                failed = True
                continue

            if self._rename or item_info.get("Name") == "filename":
                path = str(item_info.get("Path") or "")
                if path:
                    item_info["Name"] = Path(path).stem
            item_info.update(source_metadata)
            item_info.update(
                {
                    "IndexNumber": episode,
                    "LockData": True,
                }
            )
            updated = self.__update_item_info(item["Id"], item_info)
            failed = failed or not updated
            logger.info(
                f"{source_metadata.get('Album')} 第{episode}集 "
                f"{item_info.get('Name')} 更新{'成功' if updated else '失败'}"
            )
            time.sleep(0.5)
        return not failed

    @staticmethod
    def __source_item(items: List[dict], book_index: int) -> Optional[dict]:
        """选择指定剧集，或选择第一条具备完整专辑信息的剧集。"""
        if book_index == -1:
            return next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and item.get("AlbumId")
                    and item.get("Album")
                    and item.get("Artists")
                    and item.get("AlbumArtist")
                    and item.get("AlbumArtists")
                    and item.get("ParentIndexNumber")
                ),
                None,
            )
        if book_index < 1 or book_index > len(items):
            return None
        item = items[book_index - 1]
        return item if isinstance(item, dict) else None

    @staticmethod
    def __episode_number(item: Mapping[str, Any], fallback: int) -> int:
        """从中文集数或文件名数字中提取集数，提取失败时使用列表序号。"""
        name = str(item.get("Name") or "")
        match = re.search(r"第(\d+)集", name)
        if match:
            return int(match.group(1))
        match = re.search(r"\d+", name)
        return int(match.group()) if match else fallback

    @staticmethod
    def __set_artist_fields(item_info: Dict[str, Any], artist_item: Dict[str, Any]) -> None:
        """写入 Emby 有声书作者字段，保持旧版接口所需的数据形状。"""
        item_info.update(
            {
                "Artists": [artist_item["Name"]],
                "AlbumArtist": artist_item["Name"],
                "ArtistItems": dict(artist_item),
                "Composers": dict(artist_item),
                "AlbumArtists": dict(artist_item),
            }
        )

    @staticmethod
    def __artist_items(item_info: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """兼容 Emby 作者字段返回单对象或对象列表的两种形状。"""
        result: List[Dict[str, Any]] = []
        seen = set()
        for key in ("ArtistItems", "Composers", "AlbumArtists"):
            value = item_info.get(key)
            values = [value] if isinstance(value, Mapping) else value
            if not isinstance(values, (list, tuple)):
                continue
            for artist in values:
                if not isinstance(artist, Mapping):
                    continue
                artist_id = str(artist.get("Id") or "")
                artist_name = str(artist.get("Name") or "")
                identity = artist_id or artist_name
                if identity and identity not in seen:
                    result.append(dict(artist))
                    seen.add(identity)
        return result

    def __request_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """通过 SDK 网络门面读取 Emby JSON，并始终释放响应连接。"""
        if not self._emby_host or not self._emby_api_key:
            return None
        request_params = dict(params or {})
        request_params["api_key"] = self._emby_api_key
        try:
            with RequestUtils().response_manager(
                "GET",
                self._emby_host + path.lstrip("/"),
                params=request_params,
            ) as response:
                if response is None or response.status_code != 200:
                    return None
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"连接 Emby 接口 {path} 出错：{err}")
            return None

    def __get_items(self, parent_id: Any) -> List[dict]:
        """读取指定父项目下的 Emby 项目列表。"""
        if not parent_id or not self._emby_user:
            return []
        payload = self.__request_json(
            f"emby/Users/{self._emby_user}/Items",
            {"ParentId": parent_id},
        )
        items = payload.get("Items") if payload else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def __get_item_info(self, item_id: Any) -> Dict[str, Any]:
        """读取单个 Emby 项目的完整更新数据。"""
        if not item_id or not self._emby_user:
            return {}
        payload = self.__request_json(
            f"emby/Users/{self._emby_user}/Items/{item_id}",
            {
                "fields": "ShareLevel",
                "ExcludeFields": "Chapters,Overview,People,MediaStreams,Subviews",
            },
        )
        return payload or {}

    def __get_artists(self) -> List[dict]:
        """读取 Emby 作者列表。"""
        payload = self.__request_json("emby/Artists")
        artists = payload.get("Items") if payload else None
        return artists if isinstance(artists, list) else []

    def __update_item_info(self, item_id: Any, data: Dict[str, Any]) -> bool:
        """通过 Emby 更新接口提交项目元数据，并释放响应连接。"""
        if not item_id or not self._emby_host or not self._emby_api_key:
            return False
        try:
            with RequestUtils(content_type="application/json").response_manager(
                "POST",
                f"{self._emby_host}emby/Items/{item_id}",
                params={"api_key": self._emby_api_key},
                json=data,
            ) as response:
                return response is not None and response.status_code == 204
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"更新 Emby 项目 {item_id} 出错：{err}")
            return False

    def __delete_by_id(self, item_id: Any) -> bool:
        """删除旧作者项目，避免修改作者后残留重复条目。"""
        if not item_id or not self._emby_host or not self._emby_api_key:
            return False
        try:
            with RequestUtils().response_manager(
                "POST",
                f"{self._emby_host}emby/Items/Delete",
                params={"Ids": item_id, "api_key": self._emby_api_key},
            ) as response:
                return response is not None and response.status_code == 204
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(f"删除 Emby 作者 {item_id} 出错：{err}")
            return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册有声书整理和演播作者修正命令。"""
        return [
            {
                "cmd": "/ab",
                "event": EventType.PluginAction,
                "desc": "emby有声书整理",
                "category": "",
                "data": {"action": "audiobook"},
            },
            {
                "cmd": "/aba",
                "event": EventType.PluginAction,
                "desc": "emby有声书演播者整理",
                "category": "",
                "data": {"action": "audiobook_artist"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """该插件不暴露 MoviePilot REST API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """通过 V3 宿主服务合同注册一次性和周期整理任务。"""
        services: List[Dict[str, Any]] = []
        if self._run_once:
            services.append(
                {
                    "id": "EmbyAudioBook.Once",
                    "name": "Emby有声书整理（立即运行）",
                    "trigger": "date",
                    "func": self._run_once_check,
                    "kwargs": {
                        "run_date": datetime.datetime.now(
                            tz=pytz.timezone(str(settings.TZ))
                        )
                        + datetime.timedelta(seconds=3)
                    },
                }
            )
        if self._enabled and self._cron:
            try:
                services.append(
                    {
                        "id": "EmbyAudioBook",
                        "name": "Emby有声书整理",
                        "trigger": CronTrigger.from_crontab(
                            self._cron,
                            timezone=pytz.timezone(str(settings.TZ)),
                        ),
                        "func": self.check,
                        "kwargs": {},
                    }
                )
            except (TypeError, ValueError) as err:
                logger.error(f"定时任务配置错误：{err}")
                self.systemmessage.put(f"执行周期配置错误：{err}")
        return services

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """构造 V3 Vuetify 配置表单。"""
        message_type_options = [
            {"title": item.value, "value": item.name} for item in MessageType
        ]
        emby_configs = [
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
                                "props": {"cols": 12, "md": 3},
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
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "rename",
                                            "label": "重命名有声书",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "定时全量同步周期",
                                            "placeholder": "5位cron表达式，留空关闭",
                                        },
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
                                            "multiple": False,
                                            "chips": True,
                                            "model": "msgtype",
                                            "label": "消息类型",
                                            "items": message_type_options,
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
                                        "props": {
                                            "model": "library_id",
                                            "label": "有声书文件夹ID",
                                            "placeholder": "媒体库有声书-->文件夹-->看URL里的ParentId",
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
                                            "items": emby_configs,
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
                                                "仅支持交互命令运行：/ab 媒体库 书名 正确信息集数；"
                                                "/aba 媒体库 书名 正确的演播作者名称。"
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
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "rename": False,
            "cron": "",
            "msgtype": "",
            "library_id": "",
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        """该插件不提供详情页。"""
        return []

    def stop_service(self) -> None:
        """公共调度任务由宿主撤销，插件没有额外后台资源需要释放。"""
