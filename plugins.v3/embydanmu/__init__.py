from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.services import MediaServerHelper


class EmbyDanmu(_PluginBase):
    """通知 Emby Danmu 插件为媒体库中的电影或剧集下载弹幕。"""

    plugin_name = "Emby弹幕下载"
    plugin_desc = "通知Emby Danmu插件下载弹幕。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/danmu.png"
    plugin_version = "3.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "embydanmu_"
    plugin_order = 30
    auth_level = 1

    def __init__(self) -> None:
        """初始化插件状态；所有外部连接均延迟到命令执行时建立。"""
        super().__init__()
        self._run_lock = threading.Lock()
        self._enabled = False
        self._disabled = False
        self._mediaservers: List[str] = []
        self._paths: Dict[str, str] = {}
        self._library_task: Dict[Tuple[str, str], List[str]] = {}
        self._danmu_source: List[str] = []
        self._emby_host = ""
        self._emby_user: Optional[str] = None
        self._emby_api_key: Optional[str] = None
        self.mediaserver_helper: Optional[MediaServerHelper] = None

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建运行态，允许宿主重复初始化插件。"""
        with self._run_lock:
            config = config or {}
            self._library_task.clear()
            self._enabled = bool(config.get("enabled"))
            self._disabled = bool(config.get("disabled"))
            self._mediaservers = list(config.get("mediaservers") or [])
            self._paths = self.__parse_path_mappings(config.get("dirs"))
            self._danmu_source = []
            self._emby_host = ""
            self._emby_user = None
            self._emby_api_key = None
            self.mediaserver_helper = MediaServerHelper()

    @staticmethod
    def __parse_path_mappings(value: Optional[str]) -> Dict[str, str]:
        """解析每行一个的 Emby 路径到 MoviePilot 路径映射。"""
        mappings: Dict[str, str] = {}
        for line in str(value or "").splitlines():
            source, separator, target = line.partition(":")
            source = source.strip()
            target = target.strip()
            if separator and source and target:
                mappings[source] = target
        return mappings

    @staticmethod
    def __normalize_host(value: Any) -> str:
        """规范化媒体服务器地址，避免请求路径出现重复或缺少斜杠。"""
        host = str(value or "").strip().rstrip("/")
        if not host:
            return ""
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        return f"{host}/"

    def __set_server_context(self, service: Any) -> bool:
        """从服务发现结果绑定本次命令使用的 Emby 连接参数。"""
        config = service.config.config if service.config else {}
        self._emby_host = self.__normalize_host(config.get("host"))
        self._emby_api_key = str(config.get("apikey") or "") or None
        self._emby_user = None
        if service.instance:
            try:
                self._emby_user = service.instance.get_user()
            except Exception as error:
                logger.error(f"获取 Emby 用户 ID 出错：{error}")
        return bool(self._emby_host and self._emby_api_key and self._emby_user)

    def __request_response(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
    ) -> Any:
        """通过 V3 网络 SDK 调用 Emby 外部 API，并隐藏 API key 不写入日志。"""
        if not self._emby_host or not self._emby_api_key:
            return None

        request_params = dict(params or {})
        request_params["api_key"] = self._emby_api_key
        url = f"{self._emby_host}{path.lstrip('/')}"
        try:
            request = RequestUtils(content_type="application/json") if payload is not None else RequestUtils()
            if method == "GET":
                return request.get_res(url=url, params=request_params)
            return request.post_res(url=url, params=request_params, json=payload)
        except Exception as error:
            logger.error(f"请求 Emby 接口 {path} 出错：{error}")
            return None

    def __get_json(
        self,
        path: str,
        params: Optional[dict] = None,
        expected_status: Tuple[int, ...] = (200,),
    ) -> Any:
        """读取 Emby JSON 响应，错误状态和解析失败均按空结果处理。"""
        response = self.__request_response("GET", path, params=params)
        try:
            if response is None or response.status_code not in expected_status:
                return None
            return response.json()
        except Exception as error:
            logger.error(f"解析 Emby 接口 {path} 响应出错：{error}")
            return None
        finally:
            if response is not None:
                response.close()

    def __get_text(self, path: str, params: Optional[dict] = None) -> str:
        """读取 Emby 文本响应并保证连接释放。"""
        response = self.__request_response("GET", path, params=params)
        try:
            if response is None or response.status_code != 200:
                return ""
            return str(response.text or "")
        except Exception as error:
            logger.error(f"读取 Emby 接口 {path} 响应出错：{error}")
            return ""
        finally:
            if response is not None:
                response.close()

    def __post_json(
        self,
        path: str,
        payload: dict,
        expected_status: Tuple[int, ...] = (200, 204),
    ) -> bool:
        """提交 Emby JSON 请求，并按 HTTP 状态判断写入是否成功。"""
        response = self.__request_response("POST", path, payload=payload)
        try:
            return response is not None and response.status_code in expected_status
        except Exception as error:
            logger.error(f"读取 Emby 接口 {path} 状态出错：{error}")
            return False
        finally:
            if response is not None:
                response.close()

    @eventmanager.register(EventType.PluginAction)
    def danmu(self, event: Event = None) -> None:
        """处理 `/danmu 媒体库 媒体 (季)` 命令。"""
        if not self._enabled or event is None:
            return

        event_data = event.event_data
        if not isinstance(event_data, dict) or event_data.get("action") != "embydanmu":
            return

        if not self._run_lock.acquire(blocking=False):
            self.__notify(event_data, "已有 Emby 弹幕下载任务正在执行，请稍后重试")
            return
        try:
            self.__run_danmu(event_data)
        finally:
            self._run_lock.release()

    def __run_danmu(self, event_data: dict) -> None:
        """串行执行一次命令，避免不同 Emby 服务共享连接上下文。"""

        args = str(event_data.get("arg_str") or "").split()
        if not 1 <= len(args) <= 3:
            self.__notify(event_data, "参数错误！ /danmu 媒体库名 媒体名 (季) 或 /danmu 华语电影 或 /danmu 国产剧")
            return

        season = None
        if len(args) == 3:
            try:
                season = int(args[2])
            except (TypeError, ValueError):
                self.__notify(event_data, "季数必须是整数")
                return
            if season <= 0:
                self.__notify(event_data, "季数必须大于 0")
                return

        if self.mediaserver_helper is None:
            self.mediaserver_helper = MediaServerHelper()
        try:
            services = self.mediaserver_helper.get_services(
                name_filters=self._mediaservers,
                type_filter="emby",
            )
        except Exception as error:
            logger.error(f"获取 Emby 媒体服务器失败：{error}")
            services = {}
        if not services:
            logger.error("未配置 Emby 媒体服务器")
            return

        for server_name, service in services.items():
            if not self.__set_server_context(service):
                logger.error(f"媒体服务器 {server_name} 配置不完整")
                continue
            self._danmu_source = self.__get_danmu_source()
            if not self._danmu_source:
                logger.error(f"{server_name} 未配置弹幕源")
                self.__notify(event_data, f"{server_name} 未正确配置弹幕源")
                continue
            try:
                self.__process_server(
                    server_name=server_name,
                    event_data=event_data,
                    library_name=args[0],
                    item_name=args[1] if len(args) > 1 else None,
                    season=season,
                )
            except Exception as error:
                logger.error(f"{server_name} 获取弹幕任务出错：{error}")

    def __process_server(
        self,
        server_name: str,
        event_data: dict,
        library_name: str,
        item_name: Optional[str],
        season: Optional[int],
    ) -> None:
        """在单个 Emby 服务中查找媒体库并处理目标媒体。"""
        libraries = self.__get_librarys()
        library = next(
            (
                item
                for item in libraries
                if item.get("Name") == library_name
            ),
            None,
        )
        if not library:
            self.__notify(event_data, f"{server_name} 未找到媒体库：{library_name}")
            return

        library_id = library.get("Id") or library.get("ItemId")
        library_options = library.get("LibraryOptions")
        if not library_id or not isinstance(library_options, dict):
            self.__notify(event_data, f"{server_name} 未找到媒体库：{library_name}")
            return

        disabled_fetchers = list(library_options.get("DisabledSubtitleFetchers") or [])
        if "Danmu" in disabled_fetchers:
            enabled_options = dict(library_options)
            enabled_options["DisabledSubtitleFetchers"] = [
                item for item in disabled_fetchers if item != "Danmu"
            ]
            if not self.__update_library(library_id, enabled_options):
                self.__notify(event_data, f"{server_name} 启用媒体库：{library_name}的Danmu插件失败")
                return
            logger.info(f"{server_name} 已启用媒体库：{library_name}的Danmu插件")

        is_special_library = item_name is None
        task_name = library_name if is_special_library else item_name
        task_key = (server_name, str(library_id))
        self._library_task.setdefault(task_key, []).append(task_name or library_name)
        try:
            items = self.__get_items(
                library_id,
                name_starts_with=None if is_special_library else item_name,
            )
            if not items:
                logger.error(f"{server_name} 获取媒体库：{library_name}的媒体列表失败")
                self.__notify(event_data, f"{server_name} 获取媒体库：{library_name}的媒体列表失败")
                return

            found = False
            library_type = library.get("CollectionType")
            for item in items:
                if not isinstance(item, dict):
                    continue
                current_name = str(item.get("Name") or "")
                if library_type == "tvshows":
                    if not is_special_library and current_name != item_name:
                        continue
                    found = True
                    self.__process_series(
                        server_name=server_name,
                        event_data=event_data,
                        library_name=library_name,
                        series=item,
                        season=season,
                    )
                else:
                    if not is_special_library and not self.__movie_name_matches(current_name, item_name):
                        continue
                    found = True
                    self.__process_movie(
                        server_name=server_name,
                        event_data=event_data,
                        library_name=library_name,
                        item=item,
                        special_library=is_special_library,
                    )
                if not is_special_library:
                    break

            if not found:
                suffix = "" if is_special_library else f" {item_name}"
                self.__notify(event_data, f"{server_name} 未找到媒体：{library_name}{suffix}")
        finally:
            tasks = self._library_task.get(task_key, [])
            if task_name in tasks:
                tasks.remove(task_name)
            if not tasks:
                self._library_task.pop(task_key, None)
            if not tasks and self._disabled:
                disabled_options = dict(library_options)
                final_disabled = list(disabled_options.get("DisabledSubtitleFetchers") or [])
                if "Danmu" not in final_disabled:
                    final_disabled.append("Danmu")
                disabled_options["DisabledSubtitleFetchers"] = final_disabled
                if self.__update_library(library_id, disabled_options):
                    logger.info(f"{server_name} 已禁用媒体库：{library_name} Danmu插件")
                else:
                    logger.error(f"{server_name} 禁用媒体库：{library_name} Danmu插件失败")

    @staticmethod
    def __movie_name_matches(name: str, expected: Optional[str]) -> bool:
        """按旧插件约定去掉年份后比较电影名称。"""
        if not expected:
            return False
        match = re.match(r"^(.*?)(?= ?\(\d{4}\)?|$)", name)
        return bool(match and match.group(1).strip() == expected)

    def __process_series(
        self,
        server_name: str,
        event_data: dict,
        library_name: str,
        series: dict,
        season: Optional[int],
    ) -> None:
        """遍历电视剧季，并按命令参数限制处理范围。"""
        series_id = series.get("Id")
        if not series_id:
            return
        series_name = str(series.get("Name") or "")
        seasons = self.__get_items(series_id)
        if not seasons:
            logger.error(f"{server_name} 获取剧集 {series_name} 的季度列表失败")
            return
        for season_item in seasons:
            if not isinstance(season_item, dict):
                continue
            index_number = season_item.get("IndexNumber")
            if season is not None and str(index_number) != str(season):
                continue
            self.__process_season(
                server_name=server_name,
                event_data=event_data,
                library_name=library_name,
                series_name=series_name,
                season_item=season_item,
            )

    def __process_season(
        self,
        server_name: str,
        event_data: dict,
        library_name: str,
        series_name: str,
        season_item: dict,
    ) -> None:
        """检查季度弹幕文件，触发 Emby 下载并等待文件落盘。"""
        season_id = season_item.get("Id")
        if not season_id:
            return
        season_number = season_item.get("IndexNumber")
        current_count, total_count = self.__check_danmu_exists(season_id, only_check=True)
        if total_count > 0 and current_count >= total_count:
            self.__notify(
                event_data,
                f"{server_name} {library_name} {series_name} 第{season_number}季 弹幕文件已全部存在：{current_count}/{total_count}",
            )
            return

        if not self.__download_danmu(season_id):
            self.__notify(
                event_data,
                f"{server_name} 通知Danmu插件获取 {library_name} {series_name} 第{season_number}季 的弹幕失败",
            )
            return

        self.__notify(
            event_data,
            f"{server_name} 开始通知Emby下载 {library_name} {series_name} 第{season_number}季 弹幕，异步执行，请耐心等候执行完成消息",
        )
        current_count, total_count = self.__check_danmu_exists(season_id, only_check=False)
        if current_count >= total_count > 0:
            title = f"{server_name} {library_name} {series_name} 第{season_number}季 弹幕文件已全部下载完成：{current_count}/{total_count}"
        elif current_count > 0:
            title = f"{server_name} {library_name} {series_name} 第{season_number}季 弹幕文件未全部下载完成：{current_count}/{total_count}"
        else:
            title = f"{server_name} {library_name} {series_name} 第{season_number}季 Emby已配置弹幕源全部匹配弹幕失败"
        self.__notify(event_data, title)

    def __process_movie(
        self,
        server_name: str,
        event_data: dict,
        library_name: str,
        item: dict,
        special_library: bool,
    ) -> None:
        """检查电影弹幕文件，触发 Emby 下载并等待文件落盘。"""
        item_id = item.get("Id")
        if not item_id:
            return
        item_info = self.__get_item_info(item_id)
        item_path = item_info.get("Path")
        if not item_path:
            logger.error(f"{server_name} 电影 {item.get('Name')} 缺少媒体路径")
            self.__notify(event_data, f"{server_name} 获取电影：{library_name} {item.get('Name')}详情失败")
            return
        parent_path = Path(
            self.__get_path(str(item_path if special_library else Path(item_path).parent))
        )
        pattern = "*.xml"
        if list(parent_path.glob(pattern)):
            self.__notify(event_data, f"{server_name} {library_name} {item.get('Name')} 弹幕已下载完成")
            return
        if not self.__download_danmu(item_id):
            self.__notify(
                event_data,
                f"{server_name} 通知Danmu插件获取 {library_name} 电影 {item.get('Name')} {item_id} 的弹幕失败",
            )
            return

        self.__notify(
            event_data,
            f"{server_name} 开始通知Emby下载 {library_name} {item.get('Name')} 弹幕，异步执行，请耐心等候执行完成消息",
        )
        if self.__wait_for_movie(parent_path, pattern, item_info):
            title = f"{server_name} {library_name} {item.get('Name')} 下载弹幕文件成功"
        else:
            title = f"{server_name} {library_name} {item.get('Name')} 已配置弹幕源全部匹配弹幕失败"
        self.__notify(event_data, title)

    def __wait_for_movie(self, parent_path: Path, pattern: str, item_info: dict) -> bool:
        """轮询电影目录，最多等待三次并在日志明确失败时提前结束。"""
        for attempt in range(3):
            if list(parent_path.glob(pattern)):
                return True
            if self.__check_all_failed_by_log(
                item_name=item_info.get("Name"),
                item_year=item_info.get("ProductionYear"),
            ):
                return False
            if attempt < 2:
                logger.warning(f"{parent_path} 下未找到弹幕文件：{pattern}，等待5秒后重试 ({2 - attempt}次)")
                time.sleep(5)
        return bool(list(parent_path.glob(pattern)))

    def get_state(self) -> bool:
        """返回插件是否已启用。"""
        return self._enabled

    def __get_librarys(self) -> List[dict]:
        """读取 Emby 虚拟媒体库及其弹幕插件开关配置。"""
        payload = self.__get_json("emby/Library/VirtualFolders/Query")
        if isinstance(payload, dict):
            items = payload.get("Items")
        else:
            items = payload
        return [item for item in items or [] if isinstance(item, dict)]

    def __get_path(self, file_path: str) -> str:
        """按路径边界执行映射，避免 `/media` 错误匹配 `/media2`。"""
        source_path = Path(file_path)
        for library_path, target_path in self._paths.items():
            source = Path(library_path)
            try:
                relative = source_path.relative_to(source)
            except ValueError:
                continue
            return str(Path(target_path) / relative)
        return file_path

    def __update_library(self, library_id: str, library_options: dict) -> bool:
        """更新 Emby 媒体库的 Danmu 插件开关。"""
        return self.__post_json(
            "emby/Library/VirtualFolders/LibraryOptions",
            {"Id": library_id, "LibraryOptions": library_options},
            expected_status=(200, 204),
        )

    def __get_items(
        self,
        parent_id: str,
        name_starts_with: Optional[str] = None,
    ) -> List[dict]:
        """读取用户范围下的媒体项，并兼容 Emby 4.8.8 的接口路径。"""
        if not self._emby_user:
            return []
        params: Dict[str, Any] = {"ParentId": parent_id}
        if name_starts_with:
            params["NameStartsWith"] = name_starts_with
        payload = self.__get_json(f"emby/Users/{self._emby_user}/Items", params=params)
        if payload is None:
            return self.__get_items_488(parent_id, name_starts_with=name_starts_with)
        items = payload.get("Items") if isinstance(payload, dict) else []
        if items and isinstance(items[0], dict) and items[0].get("Type") == "Folder":
            return self.__get_items_488(parent_id, name_starts_with=name_starts_with)
        return [item for item in items or [] if isinstance(item, dict)]

    def __get_items_488(
        self,
        parent_id: str,
        name_starts_with: Optional[str] = None,
    ) -> List[dict]:
        """读取 Emby 4.8.8 使用的无用户前缀媒体项接口。"""
        params: Dict[str, Any] = {"ParentId": parent_id}
        if name_starts_with:
            params["NameStartsWith"] = name_starts_with
        payload = self.__get_json("emby/Items", params=params)
        items = payload.get("Items") if isinstance(payload, dict) else []
        return [item for item in items or [] if isinstance(item, dict)]

    def __download_danmu(self, item_id: str) -> bool:
        """通知 Emby Danmu 插件刷新指定项目的弹幕。"""
        text = self.__get_text(
            f"api/danmu/{item_id}",
            params={"option": "Refresh"},
        )
        return text.strip().lower() == "ok"

    def __get_item_info(self, item_id: str) -> dict:
        """读取 Emby 项目路径及日志匹配所需的基础字段。"""
        if not self._emby_user:
            return {}
        payload = self.__get_json(
            f"emby/Users/{self._emby_user}/Items/{item_id}",
            params={
                "fields": "ShareLevel",
                "ExcludeFields": "Chapters,Overview,People,MediaStreams,Subviews",
            },
        )
        return payload if isinstance(payload, dict) else {}

    def __check_danmu_exists(self, season_id: str, only_check: bool) -> Tuple[int, int]:
        """统计季度目录中的弹幕文件，并在下载后轮询增量。"""
        season_items = self.__get_items(season_id)
        total_count = len(season_items)
        if not season_items:
            return 0, 0
        first_item_id = season_items[0].get("Id")
        item_info = self.__get_item_info(first_item_id)
        item_path = item_info.get("Path")
        if not item_path:
            return 0, total_count
        parent_path = Path(self.__get_path(str(Path(item_path).parent)))
        pattern = "*.xml"
        if only_check:
            return len(list(parent_path.glob(pattern))), total_count

        downloaded: set[str] = set()
        retries = total_count
        no_increment = 0
        while len(downloaded) < total_count and retries > 0 and no_increment <= 3:
            if self.__check_all_failed_by_log(
                item_name=item_info.get("SeriesName"),
                item_year=item_info.get("ProductionYear"),
            ):
                break
            current_files = list(parent_path.glob(pattern))
            before = len(downloaded)
            downloaded.update(file.name for file in current_files)
            if len(downloaded) == total_count:
                break
            no_increment = 0 if len(downloaded) > before else no_increment + 1
            retries -= 1
            if retries > 0 and no_increment <= 3:
                logger.warning(f"{parent_path} 下弹幕文件：{pattern} 未下载完成，等待5秒后重试 ({retries}次)")
                time.sleep(5)
        return len(downloaded), total_count

    def __get_plugins(self) -> List[dict]:
        """读取 Emby 配置页插件列表。"""
        payload = self.__get_json(
            "emby/web/configurationpages",
            params={"PageType": "PluginConfiguration", "EnableInMainMenu": "true"},
        )
        return [item for item in payload or [] if isinstance(item, dict)]

    def __get_plugin_info(self, plugin_id: str) -> dict:
        """读取指定 Emby 插件配置。"""
        payload = self.__get_json(f"emby/Plugins/{plugin_id}/Configuration")
        return payload if isinstance(payload, dict) else {}

    def __get_danmu_source(self) -> List[str]:
        """读取 Emby Danmu 插件中已启用的弹幕源名称。"""
        plugins = self.__get_plugins()
        plugin_id = next(
            (plugin.get("PluginId") for plugin in plugins if plugin.get("Name") == "danmu"),
            None,
        )
        if not plugin_id:
            logger.error("弹幕配置插件未安装")
            return []
        scrapers = self.__get_plugin_info(plugin_id).get("Scrapers") or []
        return [
            str(scraper.get("Name"))
            for scraper in scrapers
            if isinstance(scraper, dict) and scraper.get("Enable") is True and scraper.get("Name")
        ]

    def __get_emby_log(self) -> str:
        """读取 Emby 最新日志片段，用于识别弹幕源全部失败。"""
        text = self.__get_text("System/Logs/embyserver.txt")
        return "\n".join(text.splitlines()[-200:]) if text else ""

    def __check_all_failed_by_log(self, item_name: Any, item_year: Any) -> bool:
        """判断所有已启用弹幕源是否都报告匹配失败或内容过小。"""
        emby_log = self.__get_emby_log()
        if not emby_log or not self._danmu_source:
            return False
        name = re.escape(str(item_name or ""))
        year = re.escape(str(item_year or ""))
        for source in self._danmu_source:
            escaped_source = re.escape(str(source))
            match_failed = rf"\[{escaped_source}\]匹配失败：{name} \({year}\)"
            content_small = rf"\[{escaped_source}\]弹幕内容少于1KB，忽略处理：.{name}"
            if not re.search(match_failed, emby_log) or not re.search(content_small, emby_log):
                return False
        return True

    def __notify(self, event_data: dict, title: str) -> None:
        """按远程命令上下文发送一次结果通知。"""
        self.post_message(
            channel=event_data.get("channel"),
            title=title,
            userid=event_data.get("user"),
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册 Emby 弹幕下载远程命令。"""
        return [
            {
                "cmd": "/danmu",
                "event": EventType.PluginAction,
                "desc": "emby弹幕下载",
                "category": "",
                "data": {"action": "embydanmu"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件没有额外的 MoviePilot JSON API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """本插件只响应远程命令，不注册后台服务。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单和默认值。"""
        helper = self.mediaserver_helper or MediaServerHelper()
        try:
            media_servers = [
                {"title": config.name, "value": config.name}
                for config in helper.get_configs().values()
                if config.type == "emby"
            ]
        except Exception:
            media_servers = []
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
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "disabled", "label": "是否禁用媒体库的Danmu插件"},
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
                                "component": "VTextarea",
                                "props": {
                                    "model": "dirs",
                                    "label": "目录映射关系",
                                    "rows": 2,
                                    "placeholder": "emby目录:mp目录（一行一个）",
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
                                    "text": "支持交互命令运行: /danmu 媒体库名 媒体名 (季) 或 /danmu 华语电影 或 /danmu 国产剧。 季可选，不填则获取全部季度。",
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
                                    "text": "需Emby安装Danmu插件，并启用弹幕功能（https://github.com/fengymi/emby-plugin-danmu）。",
                                },
                            }],
                        }],
                    },
                ],
            }
        ], {
            "enabled": False,
            "disabled": False,
            "dirs": "",
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        """本插件不提供独立详情页。"""
        return []

    def stop_service(self) -> None:
        """清理命令执行状态，避免重载后残留旧媒体库任务标记。"""
        with self._run_lock:
            self._library_task.clear()
