from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class RemoveTorrent(_PluginBase):
    # 插件名称
    plugin_name = "删除站点种子"
    # 插件描述
    plugin_desc = "删除下载器中某站点种子。"
    # 插件图标
    plugin_icon = "delete.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "removetorrent_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _downloader = None
    _delete_type = False
    _delete_torrent = False
    _delete_file = False
    _trackers = None

    def init_plugin(self, config: dict = None):
        self.qb = Qbittorrent()
        self.tr = Transmission()

        if config:
            self._downloader = config.get("downloader")
            self._delete_type = config.get("delete_type")
            self._delete_torrent = config.get("delete_torrent")
            self._delete_file = config.get("delete_file")
            self._trackers = config.get("trackers")

            if self._trackers:
                for tracker in str(self._trackers).split("\n"):
                    logger.info(f"开始处理站点tracker {tracker}")
                    self.__check_feed(tracker)

            self.update_config({
                "downloader": self._downloader,
                "delete_type": self._delete_type,
                "delete_torrent": self._delete_torrent,
                "delete_file": self._delete_file,
                "trackers": ""
            })

    def __check_feed(self, tracker: str):
        """
        检查tracker辅种情况
        """
        downloader_obj = self.__get_downloader(self._downloader)
        # 获取下载器中已完成的种子
        torrents = downloader_obj.get_completed_torrents()
        if not torrents:
            logger.info(f"下载器 {self._downloader} 未获取到已完成种子")
            return

        all_torrents = []
        tracker_torrents = []
        key_torrents = {}
        # 遍历种子，以种子名称和种子大小为key，查询辅种数量
        for torrent in torrents:
            torrent_size = self.__get_torrent_size(torrent, self._downloader)
            torrent_name = self.__get_torrent_name(torrent, self._downloader)
            torrent_key = "%s-%s" % (torrent_name, torrent_size)
            all_torrents.append(torrent_key)
            key_torrents[torrent_key] = torrent

            torrent_trackers = self.__get_torrent_trackers(torrent, self._downloader)
            if str(self._downloader) == "qb":
                # 命中tracker的种子
                if str(tracker) in torrent_trackers:
                    tracker_torrents.append(torrent_key)
            else:
                for torrent_tracker in torrent_trackers:
                    # 命中tracker的种子
                    if str(tracker) in torrent_tracker.get('announce'):
                        tracker_torrents.append(torrent_key)

        if not tracker_torrents:
            logger.error(f"下载器 {self._downloader} 未获取到命中tracker {tracker} 的种子")
            return

        # 查询tracker种子是否有其他辅种
        for tracker_torrent in tracker_torrents:
            torrent = key_torrents.get(tracker_torrent)
            torrent_name = self.__get_torrent_name(torrent, self._downloader)
            torrent_hash = self.__get_torrent_hash(torrent, self._downloader)

            if self._delete_type:
                # 有辅种
                if all_torrents.count(tracker_torrent) > 1:
                    # 删除逻辑
                    if self._delete_torrent:
                        downloader_obj.delete_torrents(delete_file=self._delete_file,
                                                       ids=torrent_hash)
                        logger.info(f"种子 {torrent_name} {torrent_hash} 有其他辅种，已删除")
                    else:
                        logger.info(f"种子 {torrent_name} {torrent_hash} 有其他辅种，可删除")
                else:
                    # 无辅种
                    logger.warn(f"种子 {torrent_name} {torrent_hash} 在其他站无辅种，如需删除请手动处理")
            else:
                # 无辅种
                if all_torrents.count(tracker_torrent) == 1:
                    # 删除逻辑
                    if self._delete_torrent:
                        downloader_obj.delete_torrents(delete_file=self._delete_file,
                                                       ids=torrent_hash)
                        logger.info(f"种子 {torrent_name} {torrent_hash} 无其他辅种，已删除")
                    else:
                        logger.info(f"种子 {torrent_name} {torrent_hash} 无其他辅种，可删除")
                else:
                    logger.warn(f"种子 {torrent_name} {torrent_hash} 在其他站有辅种，如需删除请手动处理")

    def __get_downloader(self, dtype: str):
        """
        根据类型返回下载器实例
        """
        if dtype == "qb":
            return self.qb
        elif dtype == "tr":
            return self.tr
        else:
            return None

    @staticmethod
    def __get_torrent_trackers(torrent: Any, dl_type: str):
        """
        获取种子trackers
        """
        try:
            return torrent.get("tracker") if dl_type == "qb" else torrent.trackers
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_torrent_name(torrent: Any, dl_type: str):
        """
        获取种子name
        """
        try:
            return torrent.get("name") if dl_type == "qb" else torrent.name
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_torrent_size(torrent: Any, dl_type: str):
        """
        获取种子大小
        """
        try:
            return torrent.get("size") if dl_type == "qb" else torrent.total_size
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_torrent_hash(torrent: Any, dl_type: str):
        """
        获取种子hash
        """
        try:
            return torrent.get("hash") if dl_type == "qb" else torrent.hashString
        except Exception as e:
            print(str(e))
            return ""

    def get_state(self) -> bool:
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'downloader',
                                            'label': '下载器',
                                            'items': [
                                                {'title': 'qb', 'value': 'qb'},
                                                {'title': 'tr', 'value': 'tr'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'delete_type',
                                            'label': '是否有辅种',
                                            'items': [
                                                {'title': '是', 'value': True},
                                                {'title': '否', 'value': False}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'delete_torrent',
                                            'label': '删除种子',
                                            'items': [
                                                {'title': '是', 'value': True},
                                                {'title': '否', 'value': False}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'delete_file',
                                            'label': '删除文件',
                                            'items': [
                                                {'title': '是', 'value': True},
                                                {'title': '否', 'value': False}
                                            ]
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'trackers',
                                            'rows': '3',
                                            'label': '站点tracker域名',
                                            'placeholder': '站点tracker域名，一行一个'
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
                                            'text': '输入要删除辅种的站点tracker域名。'
                                                    '保留站点没有辅种的种子，其余在其他站有辅种的种子均删除。'
                                                    '（适用于某个站点不想保种了，但是可能有孤种没法直接全部删除的情况）'
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
                                            'text': '场景一：某个站不想保种了，但是有些种子没有辅种，需要保留。'
                                                    '是否有辅种=是，删除种子=是，删除文件=否。'
                                                    '（保留站点没有辅种的种子，其余在其他站有辅种的种子均删除（保留文件）。）'
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
                                            'text': '场景二：想删除某个站没有辅种的种子。'
                                                    '是否有辅种=否，删除种子=是，删除文件=是。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "downloader": "qb",
            "delete_type": True,
            "delete_torrent": False,
            "delete_file": False,
            "trackers": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
