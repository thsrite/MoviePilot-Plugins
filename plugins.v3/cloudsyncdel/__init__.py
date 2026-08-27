"""云盘同步删除插件的 V3 实现。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app import schemas
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaImageType, MediaSource, MediaType, MessageType
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import build_media_key, resolve_media_identity
from app.sdk.network import RequestUtils


class CloudSyncDel(_PluginBase):
    """媒体库删除后，在明确映射的云盘和本地目录中同步删除媒体文件。"""

    plugin_name = "云盘同步删除"
    plugin_desc = "媒体库删除软连接/strm文件后，同步删除云盘文件。"
    plugin_icon = "clouddisk.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "cloudsyncdel_"
    plugin_order = 9
    auth_level = 2

    _video_formats = (
        ".mp4",
        ".avi",
        ".rmvb",
        ".wmv",
        ".mov",
        ".mkv",
        ".flv",
        ".ts",
        ".webm",
        ".iso",
        ".mpg",
    )
    _default_image = "https://emby.media/notificationicon.png"

    def __init__(self):
        """初始化实例级状态，避免热重载复用上一份路径映射。"""
        super().__init__()
        self._enabled = False
        self._cloud_paths: Dict[str, str] = {}
        self._local_paths: Dict[str, str] = {}
        self._notify = False
        self._url: Optional[str] = None
        self._del_history = False

    def init_plugin(self, config: dict = None):
        """读取配置并重建本次运行所需的路径映射。"""
        config = dict(config or {})
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._url = str(config.get("url") or "").strip() or None
        self._del_history = bool(config.get("del_history"))
        self._cloud_paths = self._parse_mappings(config.get("path"))
        self._local_paths = self._parse_mappings(config.get("local_path"))

        if self._del_history:
            self.del_data(key="history")
            self._del_history = False
            self.update_config({
                "enabled": self._enabled,
                "notify": self._notify,
                "path": config.get("path") or "",
                "local_path": config.get("local_path") or "",
                "url": self._url or "",
                "del_history": False,
            })
        else:
            self._migrate_history_identity()

    def _migrate_history_identity(self) -> None:
        """为可恢复的旧删除历史补齐规范媒体身份，并保留无法判断的记录。"""
        history = self.get_data("history") or []
        changed = False
        for item in history:
            if not isinstance(item, dict):
                continue
            media_source, media_id = resolve_media_identity(media=item)
            if media_source and media_id:
                normalized_source = media_source.value
                if (
                    item.get("media_source") != normalized_source
                    or item.get("media_id") != media_id
                ):
                    item["media_source"] = normalized_source
                    item["media_id"] = media_id
                    changed = True
                continue

            title = str(item.get("title") or "").strip()
            unique = str(item.get("unique") or "").strip()
            legacy_prefix = f"{title} " if title else ""
            legacy_id = unique[len(legacy_prefix):].strip() if unique.startswith(legacy_prefix) else ""
            if legacy_id.isdigit() and legacy_id != "0":
                item["media_source"] = MediaSource.TMDB.value
                item["media_id"] = legacy_id
                changed = True

        if changed:
            self.save_data("history", history)

    @staticmethod
    def _payload_value(payload: Any, key: str, default: Any = None) -> Any:
        """读取插件动作的 dict 或类型化事件载荷字段。"""
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    @staticmethod
    def _parse_mappings(value: Any) -> Dict[str, str]:
        """解析源路径到目标根的映射，并拒绝不完整或相对路径配置。"""
        mappings: Dict[str, str] = {}
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            source, target = line.split(":", 1)
            source = source.strip().replace("\\", "/")
            target = target.strip().replace("\\", "/")
            if not source or not target:
                continue
            if not Path(source).is_absolute() or not Path(target).is_absolute():
                logger.warning("路径映射必须使用绝对路径，已忽略无效配置")
                continue
            mappings[os.path.normpath(source)] = os.path.normpath(target)
        return mappings

    @staticmethod
    def _is_within(path: Path, root: Path, allow_root: bool = True) -> bool:
        """使用真实路径校验边界，阻止路径穿越和符号链接逃逸。"""
        path_real = os.path.realpath(os.path.abspath(str(path)))
        root_real = os.path.realpath(os.path.abspath(str(root)))
        try:
            within = os.path.commonpath((path_real, root_real)) == root_real
        except ValueError:
            return False
        return within and (allow_root or path_real != root_real)

    @classmethod
    def _locate_mapping(
        cls,
        mappings: Dict[str, str],
        source_path: str,
    ) -> Tuple[bool, Optional[Tuple[Path, Path]]]:
        """区分路径映射未命中、安全命中和命中后越界三种结果。"""
        if not source_path or not mappings:
            return False, None

        normalized_source_path = Path(os.path.abspath(os.path.normpath(source_path)))
        # 最长源根优先，避免父级映射遮蔽更具体的目录映射。
        candidates = sorted(mappings.items(), key=lambda item: len(item[0]), reverse=True)
        for source_root_text, target_root_text in candidates:
            source_root = Path(source_root_text)
            try:
                relative_path = normalized_source_path.relative_to(source_root)
            except ValueError:
                continue
            target_root = Path(target_root_text)
            target_path = target_root / relative_path
            # 最终叶子符号链接只解除链接；中间目录符号链接越界仍必须拒绝。
            if not cls._safe_delete_target(target_path, target_root):
                logger.warning("路径映射目标超出配置根目录，已停止删除")
                return True, None
            return True, (target_path, target_root)
        return False, None

    @classmethod
    def _resolve_mapping(
        cls,
        mappings: Dict[str, str],
        source_path: str,
    ) -> Optional[Tuple[Path, Path]]:
        """将事件路径映射到安全目标；未命中或越界时返回空。"""
        _, mapping = cls._locate_mapping(mappings, source_path)
        return mapping

    def __get_path(self, paths: Dict[str, str], file_path: str) -> Optional[str]:
        """返回安全映射后的路径；映射失配时严格返回空值。"""
        mapping = self._resolve_mapping(paths, file_path)
        return str(mapping[0]) if mapping else None

    @staticmethod
    def _media_suffixes() -> set[str]:
        """读取宿主声明的媒体扩展名，并统一大小写。"""
        return {str(suffix).lower() for suffix in (settings.RMT_MEDIAEXT or ())}

    @classmethod
    def _is_media_file(cls, path: Path) -> bool:
        """判断路径是否为宿主认定的媒体文件。"""
        return path.suffix.lower() in cls._media_suffixes()

    @classmethod
    def _safe_delete_target(cls, path: Path, root: Path) -> bool:
        """只允许删除根内目标；最终叶子符号链接按链接自身而非其指向校验。"""
        path_absolute = os.path.abspath(str(path))
        root_absolute = os.path.abspath(str(root))
        if path_absolute == root_absolute:
            return False
        if path.is_symlink():
            return cls._is_within(path.parent, root)
        return cls._is_within(path, root, allow_root=False)

    @classmethod
    def _remove_empty_parents(cls, path: Path, root: Path) -> None:
        """只沿映射目标根向上移除空目录，不触碰映射根或其父目录。"""
        current = path
        root_real = Path(os.path.realpath(os.path.abspath(str(root))))
        while cls._is_within(current, root, allow_root=False):
            current_real = Path(os.path.realpath(os.path.abspath(str(current))))
            if current_real == root_real or current.is_symlink() or not current.is_dir():
                break
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @classmethod
    def _matching_siblings(cls, path: Path, root: Path) -> List[Path]:
        """查找同名媒体及 sidecar 文件，并限制在映射根内。"""
        if not path.parent.is_dir() or not cls._is_within(path.parent, root, allow_root=False):
            return []
        siblings: List[Path] = []
        for candidate in path.parent.iterdir():
            if (
                candidate.stem == path.stem
                and candidate != path
                and cls._safe_delete_target(candidate, root)
                and (candidate.is_file() or candidate.is_symlink())
            ):
                siblings.append(candidate)
        return sorted(siblings)

    @classmethod
    def _unlink_file(cls, path: Path, root: Path) -> bool:
        """删除映射根内的单个文件或符号链接。"""
        if not cls._safe_delete_target(path, root):
            return False
        if not (path.is_file() or path.is_symlink()):
            return False
        path.unlink(missing_ok=True)
        cls._remove_empty_parents(path.parent, root)
        return True

    @classmethod
    def _remove_directory(cls, path: Path, root: Path) -> bool:
        """删除映射根内的目录；符号链接只解除链接，不跟随到根外。"""
        if not cls._safe_delete_target(path, root):
            return False
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            return False
        cls._remove_empty_parents(path.parent, root)
        return True

    def _delete_local(
        self,
        mapped_path: Path,
        mapped_root: Path,
        media_data: Dict[str, Any],
    ) -> bool:
        """删除本地映射目标，并向媒体同步删除插件转发完整身份。"""
        if not self._safe_delete_target(mapped_path, mapped_root):
            return False

        removed_paths: List[Path] = []
        removed_directory = mapped_path.is_dir() and not mapped_path.is_file()
        removed_directory_link = mapped_path.is_symlink() and not mapped_path.suffix
        if mapped_path.is_dir() and not mapped_path.is_symlink():
            if self._remove_directory(mapped_path, mapped_root):
                removed_paths.append(mapped_path)
        elif mapped_path.is_file() or mapped_path.is_symlink():
            if self._unlink_file(mapped_path, mapped_root):
                removed_paths.append(mapped_path)
        elif mapped_path.suffix:
            for sibling in self._matching_siblings(mapped_path, mapped_root):
                if self._unlink_file(sibling, mapped_root):
                    removed_paths.append(sibling)
            thumb_file = mapped_path.parent / f"{mapped_path.stem}-thumb.jpg"
            if self._unlink_file(thumb_file, mapped_root):
                removed_paths.append(thumb_file)

        if not removed_paths:
            return False

        media_path = next(
            (removed_path for removed_path in removed_paths if self._is_media_file(removed_path)),
            None,
        )
        if media_path or removed_directory or removed_directory_link:
            self._send_media_sync_delete(media_path or mapped_path, media_data)
        return True

    def _send_media_sync_delete(self, media_path: Path, media_data: Dict[str, Any]) -> None:
        """转发删除事件时始终保留来源与来源原生 ID。"""
        self.eventmanager.send_event(EventType.PluginAction, {
            "media_type": media_data.get("media_type"),
            "media_name": media_data.get("media_name"),
            "media_path": str(media_path),
            "media_source": media_data["media_source"],
            "media_id": media_data["media_id"],
            "season_num": media_data.get("season_num"),
            "episode_num": media_data.get("episode_num"),
            "action": "media_sync_del",
        })

    def _delete_cloud(self, mapped_path: Path, mapped_root: Path) -> Optional[Path]:
        """删除云盘映射目标，返回用于回调和通知的代表路径。"""
        if not self._safe_delete_target(mapped_path, mapped_root):
            return None

        removed_paths: List[Path] = []
        if mapped_path.is_dir() and not mapped_path.is_symlink():
            if self._remove_directory(mapped_path, mapped_root):
                removed_paths.append(mapped_path)
        elif mapped_path.is_file() or mapped_path.is_symlink():
            if self._unlink_file(mapped_path, mapped_root):
                removed_paths.append(mapped_path)
            if mapped_path.suffix:
                for sibling in self._matching_siblings(mapped_path, mapped_root):
                    if self._unlink_file(sibling, mapped_root):
                        removed_paths.append(sibling)
                thumb_file = mapped_path.parent / f"{mapped_path.stem}-thumb.jpg"
                if self._unlink_file(thumb_file, mapped_root):
                    removed_paths.append(thumb_file)
        elif mapped_path.suffix:
            for sibling in self._matching_siblings(mapped_path, mapped_root):
                if self._unlink_file(sibling, mapped_root):
                    removed_paths.append(sibling)
            thumb_file = mapped_path.parent / f"{mapped_path.stem}-thumb.jpg"
            if self._unlink_file(thumb_file, mapped_root):
                removed_paths.append(thumb_file)

        if not removed_paths:
            return None
        for removed_path in removed_paths:
            if self._is_media_file(removed_path):
                return removed_path
        return removed_paths[0]

    @eventmanager.register(EventType.PluginAction)
    def clouddisk_del(self, event: Event = None):
        """处理媒体同步删除事件，并在映射不明确时停止整个删除链路。"""
        if not self._enabled or not event:
            return

        event_data = event.event_data or {}
        action = self._payload_value(event_data, "action")
        if action not in ("networkdisk_del", "cloudsyncdel"):
            return

        media_path = str(self._payload_value(event_data, "media_path") or "").strip()
        if not media_path:
            logger.error("未获取到删除媒体路径，跳过处理")
            return

        media_source, media_id = resolve_media_identity(media=event_data)
        if not media_source or not media_id:
            logger.error("删除事件缺少完整媒体身份，跳过处理")
            return

        media_data = {
            "media_type": self._payload_value(event_data, "media_type"),
            "media_name": self._payload_value(event_data, "media_name"),
            "media_source": media_source.value,
            "media_id": media_id,
            "season_num": self._payload_value(event_data, "season_num"),
            "episode_num": self._payload_value(event_data, "episode_num"),
        }

        local_matched, local_mapping = self._locate_mapping(self._local_paths, media_path)
        if local_matched and not local_mapping:
            return
        if local_mapping:
            local_path, local_root = local_mapping
            if self._delete_local(local_path, local_root, media_data):
                return

        cloud_matched, cloud_mapping = self._locate_mapping(self._cloud_paths, media_path)
        if not cloud_matched:
            logger.warning("删除媒体路径未匹配云盘映射，跳过云盘删除")
            return
        if not cloud_mapping:
            return

        cloud_path, cloud_root = cloud_mapping
        deleted_path = self._delete_cloud(cloud_path, cloud_root)
        if not deleted_path:
            return

        self._notify_cloud_delete(deleted_path, media_data)
        self._save_history(media_path, media_data)

    def _notify_cloud_delete(self, deleted_path: Path, media_data: Dict[str, Any]) -> None:
        """按配置回调外部同步服务并发送可选的 MoviePilot 通知。"""
        if self._url and (not deleted_path.suffix or self._is_media_file(deleted_path)):
            try:
                RequestUtils(content_type="application/json").post(
                    url=self._url,
                    json={"path": str(deleted_path), "type": "del"},
                )
            except Exception as error:
                logger.error("云盘删除回调失败：%s", error)

        if not self._notify:
            return

        media_type = self._media_type(media_data.get("media_type"))
        media_source = MediaSource(media_data["media_source"])
        image = self._default_image
        if media_source == MediaSource.TMDB:
            image = self.chain.obtain_specific_image(
                mediaid=media_data["media_id"],
                mtype=media_type,
                image_type=MediaImageType.Backdrop,
                season=media_data.get("season_num"),
                episode=media_data.get("episode_num"),
            ) or image

        media_name = media_data.get("media_name") or "未知媒体"
        identity = build_media_key(media_source, media_data["media_id"])
        season = media_data.get("season_num")
        episode = media_data.get("episode_num")
        if media_type == MediaType.MOVIE:
            message = f"电影 {media_name} {identity}"
        elif season and episode and str(episode).isdigit():
            message = f"剧集 {media_name} S{season}E{episode} {identity}"
        elif season:
            message = f"剧集 {media_name} S{season} {identity}"
        else:
            message = f"剧集 {media_name} {identity}"

        self.post_message(
            mtype=MessageType.Plugin,
            title="云盘同步删除任务完成",
            image=image,
            text=(
                f"{message}\n"
                f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
            ),
        )

    @staticmethod
    def _media_type(value: Any) -> MediaType:
        """将旧事件中的电影/剧集标记归一为 V3 媒体类型。"""
        if value in ("Movie", "MOV", MediaType.MOVIE, MediaType.MOVIE.value):
            return MediaType.MOVIE
        return MediaType.TV

    def _save_history(self, media_path: str, media_data: Dict[str, Any]) -> None:
        """保存带完整媒体身份的删除历史。"""
        media_source = MediaSource(media_data["media_source"])
        media_type = self._media_type(media_data.get("media_type"))
        image = self._default_image
        if media_source == MediaSource.TMDB:
            image = self.chain.obtain_specific_image(
                mediaid=media_data["media_id"],
                mtype=media_type,
                image_type=MediaImageType.Poster,
            ) or image

        history = self.get_data("history") or []
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        history.append({
            "type": media_type.value,
            "title": media_data.get("media_name"),
            "path": media_path,
            "media_source": media_source.value,
            "media_id": media_data["media_id"],
            "season": (
                media_data.get("season_num")
                if media_data.get("season_num") and str(media_data.get("season_num")).isdigit()
                else None
            ),
            "episode": (
                media_data.get("episode_num")
                if media_data.get("episode_num") and str(media_data.get("episode_num")).isdigit()
                else None
            ),
            "image": image,
            "del_time": now,
            "unique": f"{media_data.get('media_name')}:{build_media_key(media_source, media_data['media_id'])}:{now}",
        })
        self.save_data("history", history)

    def delete_history(self, key: str) -> schemas.Response[None]:
        """删除详情页中指定的插件历史记录。"""
        history = self.get_data("history")
        if not history:
            return schemas.Response(success=False, message="未找到历史记录")
        self.save_data("history", [item for item in history if item.get("unique") != key])
        return schemas.Response(success=True, message="删除成功")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义远程控制命令。"""
        return [{
            "cmd": "/cloudsyncdel",
            "event": EventType.PluginAction,
            "desc": "云盘同步删除",
            "category": "",
            "data": {"action": "cloudsyncdel"},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/delete_history",
            "endpoint": self.delete_history,
            "methods": ["GET"],
            "auth": "bear",
            "summary": "删除同步历史记录",
            "response_model": schemas.Response[None],
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装插件配置页面和默认配置。"""
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
                                    "props": {"model": "notify", "label": "开启通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "del_history", "label": "清空历史"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "path",
                            "rows": "2",
                            "label": "媒体库路径映射（删除云盘文件）",
                            "placeholder": "媒体服务器软连接/strm路径:MoviePilot云盘路径（一行一个）",
                        },
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "local_path",
                            "rows": "2",
                            "label": "本地路径映射（回调【媒体文件同步删除】插件删除本地文件）",
                            "placeholder": "媒体服务器软连接/strm路径:MoviePilot本地文件路径（一行一个）",
                        },
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "url",
                            "label": "任务推送url",
                            "placeholder": "post请求json方式推送path和type(del)字段",
                        },
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "需要开启媒体库删除插件且正确配置排除路径。路径映射必须使用绝对路径。",
                        },
                    },
                ],
            },
        ], {
            "enabled": False,
            "path": "",
            "url": "",
            "local_path": "",
            "notify": False,
            "del_history": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """拼装删除历史详情页。"""
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "div",
                "text": "暂无数据",
                "props": {"class": "text-center"},
            }]

        contents = []
        for item in sorted(history, key=lambda value: value.get("del_time", ""), reverse=True):
            unique = item.get("unique")
            details = [
                {"component": "VCardText", "text": f"类型：{item.get('type')}"},
                {
                    "component": "VCardText",
                    "text": f"身份：{item.get('media_source')}:{item.get('media_id')}",
                },
            ]
            if item.get("season"):
                details.append({"component": "VCardText", "text": f"季：{item.get('season')}"})
            if item.get("episode"):
                details.append({"component": "VCardText", "text": f"集：{item.get('episode')}"})
            details.append({"component": "VCardText", "text": f"时间：{item.get('del_time')}"})
            contents.append({
                "component": "VCard",
                "content": [
                    {
                        "component": "VDialogCloseBtn",
                        "props": {"innerClass": "absolute top-0 right-0"},
                        "events": {
                            "click": {
                                "api": "plugin/CloudSyncDel/delete_history",
                                "method": "get",
                                "params": {"key": unique},
                            }
                        },
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-start flex-nowrap flex-row pa-2"},
                        "content": [
                            {
                                "component": "VImg",
                                "props": {
                                    "src": item.get("image") or self._default_image,
                                    "height": 120,
                                    "width": 80,
                                    "aspect-ratio": "2/3",
                                    "class": "object-cover shadow ring-gray-500",
                                    "cover": True,
                                },
                            },
                            {
                                "component": "div",
                                "content": [
                                    {
                                        "component": "VCardTitle",
                                        "text": item.get("title") or "未知媒体",
                                    },
                                    *details,
                                ],
                            },
                        ],
                    },
                ],
            })
        return [{
            "component": "div",
            "props": {"class": "grid gap-3 grid-info-card"},
            "content": contents,
        }]

    def stop_service(self):
        """插件无常驻任务。"""
        return None
