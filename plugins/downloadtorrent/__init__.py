from app.db.site_oper import SiteOper
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.utils.string import StringUtils


class DownloadTorrent(_PluginBase):
    # 插件名称
    plugin_name = "添加种子下载"
    # 插件描述
    plugin_desc = "选择下载器，添加种子任务。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/download.png"
    # 插件版本
    plugin_version = "1.0"
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
    _downloader = None
    _is_paused = False
    _save_path = None
    _torrent_urls = None
    qb = None
    tr = None
    site = None

    def init_plugin(self, config: dict = None):
        self.qb = Qbittorrent()
        self.tr = Transmission()
        self.site = SiteOper()

        if config:
            self._downloader = config.get("downloader")
            self._is_paused = config.get("is_paused")
            self._save_path = config.get("save_path")
            self._torrent_urls = config.get("torrent_urls")

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

                    # 添加下载
                    if str(self._downloader) == "qb":
                        torrent = self.qb.add_torrent(content=torrent_url,
                                                      is_paused=self._is_paused,
                                                      download_dir=self._save_path,
                                                      cookie=site.cookie)
                    else:
                        torrent = self.tr.add_torrent(content=torrent_url,
                                                      is_paused=self._is_paused,
                                                      download_dir=self._save_path,
                                                      cookie=site.cookie)

                    if torrent:
                        logger.info(f"种子添加下载成功 {torrent_url} 保存位置 {self._save_path}")
                    else:
                        logger.error(f"种子添加下载失败 {torrent_url} 保存位置 {self._save_path}")

            self.update_config({
                "downloader": self._downloader,
                "save_path": self._save_path,
                "is_paused": self._is_paused
            })

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
                                           'md': 4
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
                                           'md': 4
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
                                           'md': 4
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'save_path',
                                                   'label': '保存路径'
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
                   "torrent_urls": ""
               }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
