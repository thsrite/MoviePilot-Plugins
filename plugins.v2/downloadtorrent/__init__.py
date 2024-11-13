from typing import Any, List, Dict, Tuple, Optional
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from app.log import logger
from app.utils.string import StringUtils
from app.schemas import ServiceInfo
from app.helper.downloader import DownloaderHelper
from app.helper.directory import DirectoryHelper



class DownloadTorrent(_PluginBase):
    # 插件名称
    plugin_name = "添加种子下载"
    # 插件描述
    plugin_desc = "选择下载器，添加种子任务。"
    # 插件图标
    plugin_icon = "download.png"
    # 插件版本
    plugin_version = "2.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "downloadtorrent_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _is_paused = False
    _save_path = None
    _mp_path = None
    _downloader = None
    site = None
    torrent_helper = None
    downloader_helper = None
    directory_helper = None

    def init_plugin(self, config: dict = None):
        self.downloader_helper = DownloaderHelper()
        self.directory_helper = DirectoryHelper()
        self.site = SiteOper()

        if config:
            self._is_paused = config.get("is_paused")
            self._save_path = config.get("save_path")
            self._mp_path = config.get("mp_path")
            self._torrent_urls = config.get("torrent_urls")
            self._downloader = config.get("downloader")

            # 下载种子
            if self._torrent_urls:
                for torrent_url in str(self._torrent_urls).split("\n"):
                    # 获取种子对应站点cookie
                    domain = StringUtils.get_url_domain(torrent_url)
                    if not domain:
                        logger.error(f"种子 {torrent_url} 获取站点域名失败，跳过处理")
                        continue

                    # 查询站点
                    site = self.site.get_by_domain(domain)
                    if not site or not site.cookie:
                        logger.error(f"种子 {torrent_url} 获取站点cookie失败，跳过处理")
                        continue

                    service = self.service_info(self._downloader)
                    download_id = self.__download(service=service,
                                              content=torrent_url,
                                              save_path=self._save_path or self._mp_path,
                                              cookie=site.cookie)

                    if download_id:
                        logger.info(f"种子添加下载成功 {torrent_url} 保存位置 {self._save_path or self._mp_path}")
                    else:
                        logger.error(f"种子添加下载失败 {torrent_url} 保存位置 {self._save_path or self._mp_path}")

            self.update_config({
                "downloader": self._downloader,
                "save_path": self._save_path,
                "mp_path": self._mp_path,
                "is_paused": self._is_paused
            })

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """
        服务信息
        """
        if not name:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = self.downloader_helper.get_service(name)
        if not service or not service.instance:
            logger.warning(f"获取下载器 {name} 实例失败，请检查配置")
            return None

        if service.instance.is_inactive():
            logger.warning(f"下载器 {name} 未连接，请检查配置")
            return None
        return service
    
    def __download(self, service: ServiceInfo, content: bytes,
                   save_path: str, cookie: str) -> Optional[str]:
        """
        添加下载任务
        """
        if not service or not service.instance:
            return
        downloader = service.instance
        if self.downloader_helper.is_downloader("qbittorrent", service=service):
            torrent = downloader.add_torrent(content=content,
                                           download_dir=save_path,
                                           is_paused=self._is_paused,
                                           cookie=cookie)
            if not torrent:
                return None
            else:
              return torrent
        elif self.downloader_helper.is_downloader("transmission", service=service):
            # 添加任务
            torrent = downloader.add_torrent(content=content,
                                             download_dir=save_path,
                                             is_paused=self._is_paused,
                                             cookie=cookie)
            if not torrent:
                return None
            else:
                return torrent.hashString

        logger.error(f"不支持的下载器类型")
        return None


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
        dir_conf = [{'title': d.name, 'value': d.download_path} for d in self.directory_helper.get_local_download_dirs()]
        downloader_options = [{"title": config.name, "value": config.name} for config in self.downloader_helper.get_configs().values()]
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
                                            'items':  downloader_options
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
                                            'model': 'is_paused',
                                            'label': '暂停种子',
                                            'items': [
                                                {'title': '开启', 'value': True},
                                                {'title': '不开启', 'value': False}
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
                                            'model': 'mp_path',
                                            'label': 'MoviePilot保存路径',
                                            'items': dir_conf
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'save_path',
                                            'label': '自定义保存路径'
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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'torrent_urls',
                                            'rows': '3',
                                            'label': '种子链接',
                                            'placeholder': '种子链接，一行一个'
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
                                            'text': '自定义保存路径优先级高于MoviePilot保存路径。'
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
                                            'text': '保存路径为下载器保存路径，种子链接一行一个。'
                                                    '添加的种子链接需站点已在站点管理维护或公共站点。'
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
            "is_paused": False,
            "save_path": "",
            "mp_path": "",
            "torrent_urls": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass