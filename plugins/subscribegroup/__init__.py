import json

from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.subscribe_oper import SubscribeOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.core.event import eventmanager, Event
from app.schemas.types import EventType


class SubscribeGroup(_PluginBase):
    # 插件名称
    plugin_name = "订阅制作组填充"
    # 插件描述
    plugin_desc = "订阅首次下载自动添加官组和站点到订阅信息，以保证订阅资源的统一性。"
    # 插件图标
    plugin_icon = "teamwork.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribegroup_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _clear = False
    _subscribeoper = None
    _downloadhistoryoper = None

    def init_plugin(self, config: dict = None):
        self._downloadhistoryoper = DownloadHistoryOper()
        self._subscribeoper = SubscribeOper()

        if config:
            self._enabled = config.get("enabled")
            self._clear = config.get("clear")

        # 清理已处理历史
        if self._clear:
            self.del_data(key="history")

            self._clear = False
            self.__update_config()
            logger.info("已处理历史清理完成")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "clear": self._clear,
        })

    @eventmanager.register(EventType.DownloadAdded)
    def download_added(self, event: Event = None):
        """
        下载通知
        """
        if not self._enabled:
            logger.error("插件未开启")
            return

        history: List[str] = self.get_data('history') or []

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("hash") or not event_data.get("context"):
                return
            download_hash = event_data.get("hash")
            # 根据hash查询下载记录
            download_history = self._downloadhistoryoper.get_by_hash(download_hash)
            if not download_history:
                logger.warning(f"种子hash:{download_hash} 对应下载记录不存在")
                return

            if f"{download_history.type}:{download_history.tmdbid}" in history:
                logger.warning(f"下载历史:{download_history.title} 已处理过，不再重复处理")
                return

            # 保存已处理历史
            history.append(f"{download_history.type}:{download_history.tmdbid}")
            self.save_data('history', history)

            if download_history.type != '电视剧':
                logger.warning(f"下载历史:{download_history.title} 不是电视剧，不进行官组填充")
                return

            # 根据下载历史查询订阅记录
            subscribes = self._subscribeoper.list_by_tmdbid(tmdbid=download_history.tmdbid,
                                                            season=int(download_history.seasons.replace('S', ''))
                                                            if download_history.seasons and
                                                               download_history.seasons.count('-') == 0 else None)
            if not subscribes or len(subscribes) == 0:
                logger.warning(f"下载历史:{download_history.title} tmdbid:{download_history.tmdbid} 对应订阅记录不存在")
                return
            for subscribe in subscribes:
                if subscribe.type != '电视剧':
                    logger.warning(f"订阅记录:{subscribe.name} 不是电视剧，不进行官组填充")
                    return
                sites = json.loads(subscribe.sites) or []
                if subscribe.include or len(sites) > 0:
                    logger.warning(f"订阅记录:{subscribe.name} 已有官组或站点信息，不进行官组填充")
                    return

                # 开始填充官组和站点
                context = event_data.get("context")
                _torrent = context.torrent_info
                _meta = context.meta_info

                # 官组
                resource_team = None
                if _meta:
                    resource_team = _meta.resource_team

                # 站点
                sites = None
                if _torrent:
                    site_id = _torrent.site
                    if site_id:
                        sites = json.dumps([site_id])

                # 更新订阅记录
                if resource_team or sites:
                    self._subscribeoper.update(subscribe.id, {
                        'include': resource_team,
                        'sites':  sites
                    })
                    logger.info(
                        f"订阅记录:{subscribe.name} 填充官组 {resource_team} 和站点 {sites} 成功")

    def get_state(self) -> bool:
        return self._enabled

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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理已处理记录',
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '电视剧订阅未配置包含关键词和订阅站点时，订阅下载后，将下载种子的制作组和站点填充到订阅信息中，以保证订阅资源的统一性。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "clear": False,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
