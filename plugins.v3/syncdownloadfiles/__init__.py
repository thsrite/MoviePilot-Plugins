from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, List, Dict, Tuple, Optional
from zoneinfo import ZoneInfo

from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.transferhistory import TransferHistoryOper
from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.services import DownloaderHelper


class SyncDownloadFiles(_PluginBase):
    """扫描已完成下载器任务并登记外部下载文件。"""

    _supported_downloader_types = {"qbittorrent", "transmission"}

    # 插件名称
    plugin_name = "下载器文件同步"
    # 插件描述
    plugin_desc = "同步下载器的文件信息到数据库，删除文件时联动删除下载任务。"
    # 插件图标
    plugin_icon = "Youtube-dl_A.png"
    # 插件版本
    plugin_version = "2.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "syncdownloadfiles_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _time = None
    _interval_seconds = None
    _onlyonce = False
    _run_once = False
    _history = False
    _clear = False
    _downloaders = []
    _dirs = None

    downloader_helper = None

    def __init__(self):
        """为每个插件实例建立独立的任务与一次性触发门禁。"""
        super().__init__()
        self._sync_lock = threading.Lock()
        self._run_once_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """读取配置并重建下载器服务目录。"""
        config = config or {}

        # 停止现有任务
        self.stop_service()
        self.downloader_helper = DownloaderHelper()

        self._enabled = bool(config.get('enabled'))
        self._time = config.get('time') or 6
        self._interval_seconds = self.__parse_interval_seconds(self._time)
        if self._enabled and self._interval_seconds is None:
            logger.error("同步时间间隔必须是大于 0 的小时数，周期服务不会启动")
        self._history = bool(config.get('history'))
        self._clear = bool(config.get('clear'))
        self._run_once = bool(config.get("onlyonce"))
        self._onlyonce = False
        self._downloaders = config.get('downloaders') or []
        self._dirs = config.get("dirs") or ""

        if self._clear:
            # 清理下载器文件记录
            DownloadHistoryOper().truncate_files()
            # 清理下载器最后处理记录
            for downloader in self._downloaders:
                # 获取最后同步时间
                self.del_data(f"last_sync_time_{downloader}")
            # 关闭clear
            self._clear = False
            self.__update_config()

        if self._run_once:
            # 一次性任务交由宿主服务调度器运行，配置只保存触发开关的关闭状态。
            self.__update_config()

    def sync(self):
        """以非阻塞 single-flight 门禁执行一次文件同步。"""
        self.__run_sync(wait=False)

    def __run_sync(self, *, wait: bool):
        """按触发类型选择等待或跳过正在执行的同步任务。"""
        if not self._sync_lock.acquire(blocking=wait):
            logger.warning("已有下载器文件同步任务正在运行，跳过重复触发")
            return
        try:
            self.__sync()
        finally:
            self._sync_lock.release()

    def __sync(self):
        """
        同步所选下载器种子记录
        """
        start_time = datetime.now()
        logger.info("开始同步下载器任务文件记录")

        if not self._downloaders:
            logger.error("未选择同步下载器，停止运行")
            return

        path_mappings = self.__parse_path_mappings()
        if path_mappings is None:
            logger.error("目录映射配置无效，停止本轮同步")
            return

        download_history = DownloadHistoryOper()
        transfer_history = TransferHistoryOper() if self._history else None
        media_extensions = {
            suffix.lower() for suffix in settings.RMT_MEDIAEXT
        }

        # Oper 只在单次任务内使用，避免跨后台任务复用数据库访问对象。
        for downloader in self._downloaders:
            scan_started_at = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time()),
            )

            logger.info(f"开始扫描下载器 {downloader} ...")
            try:
                service_info = self.__get_downloader_service(downloader)
            except Exception as error:
                logger.warning(f"下载器 {downloader} 服务状态读取失败：{error}")
                continue
            if not service_info:
                logger.warning(f"下载器 {downloader} 不可用，跳过同步")
                continue
            downloader_obj = service_info.instance
            downloader_type = service_info.config.type
            if downloader_type not in self._supported_downloader_types:
                logger.warning(
                    f"下载器 {downloader} 类型 {downloader_type} 暂不支持文件同步"
                )
                continue

            # 获取下载器中已完成的种子
            try:
                torrents = downloader_obj.get_completed_torrents()
            except Exception as error:
                logger.warning(f"下载器 {downloader} 已完成任务读取失败：{error}")
                continue
            if torrents is None:
                logger.warning(f"下载器 {downloader} 已完成任务读取失败，本轮不推进同步游标")
                continue
            if not torrents:
                logger.info(f"下载器 {downloader} 没有已完成种子")
                self.save_data(f"last_sync_time_{downloader}", scan_started_at)
                continue
            logger.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")

            # 把种子按照名称和种子大小分组，获取添加时间最早的一个，认定为是源种子，其余为辅种
            try:
                torrents = self.__get_origin_torrents(torrents, downloader_type)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                logger.warning(
                    f"下载器 {downloader} 任务元数据不完整，本轮不推进同步游标：{error}"
                )
                continue
            logger.info(f"下载器 {downloader} 去除辅种，获取到源种子数：{len(torrents)}")
            scan_reliable = True

            for torrent in torrents:
                try:
                    hash_str = self.__get_hash(torrent, downloader_type)
                    download_dir = self.__get_download_dir(torrent, downloader_type)
                    torrent_name = self.__get_torrent_name(torrent, downloader_type)
                except (AttributeError, KeyError, TypeError, ValueError) as error:
                    scan_reliable = False
                    logger.warning(
                        f"下载器 {downloader} 存在无法识别的任务，本轮不推进同步游标：{error}"
                    )
                    continue

                existing_fullpaths = {
                    record.fullpath
                    for record in download_history.get_files_by_hash(hash_str)
                    if record.fullpath
                }

                download_dir = self.__map_download_dir(
                    download_dir,
                    path_mappings,
                )
                # 获取种子文件
                torrent_files = self.__get_torrent_files(
                    torrent,
                    downloader_type,
                    downloader_obj,
                )
                if torrent_files is None:
                    scan_reliable = False
                    logger.warning(
                        f"种子 {hash_str} 文件清单读取失败，本轮不推进同步游标"
                    )
                    continue
                logger.info(f"开始同步种子 {hash_str}, 文件数 {len(torrent_files)}")

                download_files = []
                transfer_updates = []
                torrent_reliable = True
                for file in torrent_files:
                    downloaded = self.__is_download(file, downloader_type)
                    if downloaded is None:
                        scan_reliable = False
                        torrent_reliable = False
                        logger.warning(
                            f"种子 {hash_str} 存在未知下载状态的文件，本轮不推进同步游标"
                        )
                        continue
                    if not downloaded:
                        continue
                    # 种子文件路径
                    file_path_str = self.__get_file_path(file, downloader_type)
                    if not file_path_str:
                        scan_reliable = False
                        torrent_reliable = False
                        logger.warning(
                            f"种子 {hash_str} 存在无法识别路径的文件，本轮不推进同步游标"
                        )
                        continue
                    file_path = PurePosixPath(file_path_str.replace('\\', '/'))
                    if not file_path.parts or file_path.is_absolute() or ".." in file_path.parts:
                        scan_reliable = False
                        torrent_reliable = False
                        logger.warning(
                            f"种子 {hash_str} 文件路径越出下载目录，本轮不推进同步游标"
                        )
                        continue
                    # 只处理视频格式
                    if not file_path.suffix \
                            or file_path.suffix.lower() not in media_extensions:
                        continue
                    # 种子文件根路程
                    root_path = file_path.parts[0]
                    # 不含种子名称的种子文件相对路径
                    if len(file_path.parts) == 1 and file_path.name == torrent_name:
                        save_path = PurePosixPath(download_dir)
                        rel_path = str(file_path)
                    elif root_path == torrent_name:
                        save_path = PurePosixPath(download_dir).joinpath(torrent_name)
                        rel_path = str(file_path.relative_to(root_path))
                    else:
                        save_path = PurePosixPath(download_dir).joinpath(torrent_name)
                        rel_path = str(file_path)
                    # 完整路径
                    full_path = save_path.joinpath(rel_path)
                    if transfer_history:
                        transferhis = transfer_history.get_by_src(str(full_path))
                        if transferhis and not transferhis.download_hash:
                            transfer_updates.append(transferhis.id)

                    if str(full_path) in existing_fullpaths:
                        continue

                    # 种子文件记录
                    download_files.append(
                        {
                            "download_hash": hash_str,
                            "downloader": downloader,
                            "fullpath": str(full_path),
                            "savepath": str(save_path),
                            "filepath": rel_path,
                            "torrentname": torrent_name,
                            "state": 1
                        }
                    )

                if not torrent_reliable:
                    logger.warning(f"种子 {hash_str} 文件清单未完整验证，等待下轮重试")
                    continue
                if download_files:
                    # 登记下载文件
                    download_history.add_files(download_files)
                for history_id in transfer_updates:
                    logger.info(
                        f"开始补充转移记录：{history_id} download_hash {hash_str}"
                    )
                    transfer_history.update_download_hash(
                        historyid=history_id,
                        download_hash=hash_str,
                    )
                logger.info(f"种子 {hash_str} 同步完成")

            if scan_reliable:
                self.save_data(f"last_sync_time_{downloader}", scan_started_at)
                logger.info("下载器种子文件同步完成")
            else:
                logger.warning(
                    f"下载器 {downloader} 存在未可靠处理的任务，保留原同步游标等待重试"
                )

            # 计算耗时
            end_time = datetime.now()

            logger.info(f"下载器任务文件记录已同步完成。总耗时 {(end_time - start_time).seconds} 秒")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "time": self._time,
            "history": self._history,
            "clear": self._clear,
            "onlyonce": self._onlyonce,
            "downloaders": self._downloaders,
            "dirs": self._dirs
        })

    @staticmethod
    def __get_origin_torrents(torrents: Any, dl_tpe: str):
        # 把种子按照名称和种子大小分组，获取添加时间最早的一个，认定为是源种子，其余为辅种
        grouped_data = {}

        # 排序种子，根据种子添加时间倒序
        if dl_tpe == "qbittorrent":
            torrents = sorted(torrents, key=lambda x: x.get("added_on"), reverse=True)
            # 遍历原始数组，按照size和name进行分组
            for torrent in torrents:
                size = torrent.get('size')
                name = torrent.get('name')
                key = (size, name)  # 使用元组作为字典的键

                # 如果分组键不存在，则将当前元素作为最小元素添加到字典中
                if key not in grouped_data:
                    grouped_data[key] = torrent
                else:
                    # 如果分组键已存在，则比较当前元素的time是否更小，如果更小则更新字典中的元素
                    if torrent.get('added_on') < grouped_data[key].get('added_on'):
                        grouped_data[key] = torrent
        elif dl_tpe == "transmission":
            torrents = sorted(torrents, key=lambda x: x.added_date, reverse=True)
            # 遍历原始数组，按照size和name进行分组
            for torrent in torrents:
                size = torrent.total_size
                name = torrent.name
                key = (size, name)  # 使用元组作为字典的键

                # 如果分组键不存在，则将当前元素作为最小元素添加到字典中
                if key not in grouped_data:
                    grouped_data[key] = torrent
                else:
                    # 如果分组键已存在，则比较当前元素的time是否更小，如果更小则更新字典中的元素
                    if torrent.added_date < grouped_data[key].added_date:
                        grouped_data[key] = torrent

        else:
            raise ValueError(f"不支持的下载器类型：{dl_tpe}")

        return list(grouped_data.values())

    @staticmethod
    def __is_download(file: Any, dl_type: str) -> Optional[bool]:
        """返回文件是否完整下载；无法确认时返回 ``None``。"""
        if dl_type == "qbittorrent":
            if not isinstance(file, dict):
                return None
            priority = file.get("priority")
            progress = file.get("progress")
            if priority is None or progress is None:
                return None
            try:
                return float(priority) > 0 and float(progress) >= 1
            except (TypeError, ValueError):
                return None
        if dl_type == "transmission":
            selected = getattr(file, "selected", None)
            completed = getattr(file, "completed", None)
            size = getattr(file, "size", None)
            if selected is None or completed is None or size is None:
                return None
            try:
                return bool(selected) and int(size) > 0 and int(completed) >= int(size)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def __get_file_path(file: Any, dl_type: str):
        """读取下载器文件路径；不完整的动态对象返回空字符串。"""
        if dl_type == "qbittorrent":
            return str(file.get("name") or "") if isinstance(file, dict) else ""
        if dl_type == "transmission":
            return str(getattr(file, "name", "") or "")
        return ""

    @staticmethod
    def __get_torrent_files(torrent: Any, dl_type: str, downloader_obj):
        """读取任务文件清单；``None`` 表示下载器读取失败。"""
        try:
            torrent_id = torrent.get("hash") if dl_type == "qbittorrent" else torrent.id
            return downloader_obj.get_files(tid=torrent_id)
        except Exception as e:
            logger.warning(f"获取种子文件失败：{str(e)}")
            return None

    @staticmethod
    def __get_torrent_name(torrent: Any, dl_type: str):
        """返回非空任务名称。"""
        name = torrent.get("name") if dl_type == "qbittorrent" else torrent.name
        if not name:
            raise ValueError("任务名称为空")
        return str(name)

    @staticmethod
    def __get_download_dir(torrent: Any, dl_type: str):
        """返回规范化的非空下载目录。"""
        download_dir = (
            torrent.get("save_path")
            if dl_type == "qbittorrent"
            else torrent.download_dir
        )
        if not download_dir:
            raise ValueError("下载目录为空")
        return str(download_dir).replace('\\', '/')

    @staticmethod
    def __get_hash(torrent: Any, dl_type: str):
        """返回非空任务哈希。"""
        hash_str = torrent.get("hash") if dl_type == "qbittorrent" else torrent.hashString
        if not hash_str:
            raise ValueError("任务哈希为空")
        return str(hash_str)

    def __get_downloader_service(self, name: str) -> Optional[Any]:
        """按配置名称读取一个已连接且受支持的下载器服务。"""
        service_info = self.downloader_helper.get_service(name=name)
        if not service_info or not service_info.config:
            return None
        if service_info.instance.is_inactive():
            logger.warning(f"下载器 {name} 未连接，请检查配置")
            return None
        return service_info

    def __parse_path_mappings(self) -> Optional[List[Tuple[str, str]]]:
        """一次性校验目录映射，避免任务处理中途产生部分错误路径。"""
        mappings = []
        for raw_mapping in str(self._dirs or "").splitlines():
            mapping = raw_mapping.strip()
            if not mapping:
                continue
            if "=>" in mapping:
                parts = mapping.split("=>", maxsplit=1)
            else:
                windows_source = re.fullmatch(
                    r"([A-Za-z]:[\\/][^:]*):(.+)",
                    mapping,
                )
                posix_to_windows = re.fullmatch(
                    r"([^:]+):([A-Za-z]:[\\/].+)",
                    mapping,
                )
                if windows_source:
                    parts = [windows_source.group(1), windows_source.group(2)]
                elif posix_to_windows:
                    parts = [posix_to_windows.group(1), posix_to_windows.group(2)]
                elif re.fullmatch(r"[A-Za-z]:[\\/].*", mapping):
                    logger.warning(f"Windows 目录映射缺少目标路径：{raw_mapping}")
                    return None
                elif mapping.count(":") == 1:
                    parts = mapping.split(":", maxsplit=1)
                else:
                    logger.warning(f"目录映射格式不明确：{raw_mapping}")
                    return None
            source = parts[0].strip().replace('\\', '/').rstrip("/")
            target = parts[1].strip().replace('\\', '/').rstrip("/")
            if not source or not target:
                logger.warning(f"目录映射源或目标为空：{raw_mapping}")
                return None
            mappings.append((source, target))
        return mappings

    @staticmethod
    def __map_download_dir(
        download_dir: str,
        mappings: List[Tuple[str, str]],
    ) -> str:
        """按首个匹配的目录段前缀应用已校验映射。"""
        normalized_dir = download_dir.replace('\\', '/')
        for source, target in mappings:
            if normalized_dir == source:
                return target
            if normalized_dir.startswith(f"{source}/"):
                return f"{target}{normalized_dir[len(source):]}"
        return normalized_dir

    @property
    def service_infos(self) -> Optional[Dict[str, Any]]:
        """
        服务信息
        """
        if not self._downloaders:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        services = self.downloader_helper.get_services(name_filters=self._downloaders)
        if not services:
            logger.warning("获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"下载器 {service_name} 未连接，请检查配置")
            elif service_info.config.type not in self._supported_downloader_types:
                logger.warning(
                    f"下载器 {service_name} 类型 {service_info.config.type} 暂不支持文件同步"
                )
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的下载器，请检查配置")
            return None

        return active_services

    def get_state(self) -> bool:
        with self._run_once_lock:
            return bool(
                self._run_once
                or (self._enabled and self._interval_seconds is not None)
            )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        services = []
        if self._run_once:
            services.append({
                "id": f"{self.__class__.__name__}.Once",
                "name": "立即同步下载器文件记录",
                "trigger": "date",
                "func": self.__run_once_sync,
                "kwargs": {
                    "run_date": datetime.now(ZoneInfo(settings.TZ))
                    + timedelta(seconds=3)
                },
            })
        if self._enabled and self._interval_seconds is not None:
            services.append({
                "id": self.__class__.__name__,
                "name": "同步下载器文件记录服务",
                "trigger": "interval",
                "func": self.sync,
                "kwargs": {"seconds": self._interval_seconds}
            })
        return services

    @staticmethod
    def __parse_interval_seconds(value: Any) -> Optional[float]:
        """把正数小时配置转换为宿主 interval 服务所需秒数。"""
        try:
            hours = float(str(value).strip())
        except (TypeError, ValueError):
            return None
        return hours * 3600 if hours > 0 else None

    def __run_once_sync(self):
        """消费一次性触发后复用受 single-flight 保护的同步入口。"""
        with self._run_once_lock:
            if not self._run_once:
                return
            self._run_once = False
        self.__run_sync(wait=True)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '开启插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'history',
                                            'label': '补充整理历史记录',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理数据',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'time',
                                            'label': '同步时间间隔（小时）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'downloaders',
                                            'label': '同步下载器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.downloader_helper.get_configs().values()
                                                      if config.type in self._supported_downloader_types]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'dirs',
                                            'label': '目录映射',
                                            'rows': 5,
                                            'placeholder': '每行一个源目录:目标目录，Windows 路径可使用 => 分隔'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '适用于非MoviePilot下载的任务；下载器种子数据较多时，同步时间将会较长，请耐心等候，可查看实时日志了解同步进度；时间间隔建议最少每6小时执行一次，防止上次任务没处理完。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "history": False,
            "clear": False,
            "time": 6,
            "dirs": "",
            "downloaders": []
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        """插件仅注册宿主公共服务，没有自持后台资源需要释放。"""
        return None
