import json
import re
import time

from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.subscribe_oper import SubscribeOper
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger
from app.core.event import eventmanager, Event
from app.schemas.types import EventType


class SubscribeGroup(_PluginBase):
    # 插件名称
    plugin_name = "订阅规则自动填充"
    # 插件描述
    plugin_desc = "电视剧下载后自动添加官组等信息到订阅；添加订阅后根据二级分类名称自定义订阅规则。"
    # 插件图标
    plugin_icon = "teamwork.png"
    # 插件版本
    plugin_version = "1.5"
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
    _clear_handle = False
    _update_details = []
    _update_confs = None
    _subscribe_confs = {}
    _subscribeoper = None
    _downloadhistoryoper = None
    _siteoper = None

    def init_plugin(self, config: dict = None):
        self._downloadhistoryoper = DownloadHistoryOper()
        self._subscribeoper = SubscribeOper()
        self._siteoper = SiteOper()

        if config:
            self._enabled = config.get("enabled")
            self._clear = config.get("clear")
            self._clear_handle = config.get("clear_handle")
            self._update_details = config.get("update_details") or []
            self._update_confs = config.get("update_confs")

            if self._update_confs:
                active_sites = self._siteoper.list_active()
                for confs in str(self._update_confs).split("\n"):
                    category = None
                    resolution = None
                    quality = None
                    effect = None
                    include = None
                    exclude = None
                    sites = []
                    for conf in str(confs).split("#"):
                        if ":" in conf:
                            k, v = conf.split(":")
                            if k == "category":
                                category = v
                            if k == "resolution":
                                resolution = v
                            if k == "quality":
                                quality = v
                            if k == "effect":
                                effect = v
                            if k == "include":
                                include = v
                            if k == "exclude":
                                exclude = v
                            if k == "sites":
                                for site_name in str(v).split(","):
                                    for active_site in active_sites:
                                        if str(site_name) == str(active_site.name):
                                            sites.append(active_site.id)
                                            break
                    if category:
                        self._subscribe_confs[category] = {
                            'resolution': resolution,
                            'quality': quality,
                            'effect': effect,
                            'include': include,
                            'exclude': exclude,
                            'sites': sites
                        }
                logger.info(f"获取到二级分类自定义配置 {len(self._subscribe_confs.keys())} 个")
            else:
                self._subscribe_confs = {}

            # 清理已处理历史
            if self._clear_handle:
                self.del_data(key="history_handle")

                self._clear_handle = False
                self.__update_config()
                logger.info("已处理历史清理完成")

            # 清理历史记录
            if self._clear:
                self.del_data(key="history")

                self._clear = False
                self.__update_config()
                logger.info("历史记录清理完成")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "clear": self._clear,
            "clear_handle": self._clear_handle,
            "update_details": self._update_details,
            "update_confs": self._update_confs,
        })

    @eventmanager.register(EventType.SubscribeAdded)
    def subscribe_notice(self, event: Event = None):
        """
        添加订阅根据二级分类填充订阅
        """
        if not self._enabled:
            logger.error("插件未开启")
            return

        if len(self._subscribe_confs.keys()) == 0:
            logger.error("插件未开启二级分类自定义填充")
            return

        if event:
            event_data = event.event_data
            if not event_data or not event_data.get("subscribe_id") or not event_data.get("mediainfo"):
                return

            sid = event_data.get("subscribe_id")
            category = event_data.get("mediainfo").get("category")
            if not category:
                logger.error(f"订阅ID:{sid} 未获取到二级分类")
                return

            if category not in self._subscribe_confs.keys():
                logger.error(f"订阅ID:{sid} 二级分类:{category} 未配置自定义规则")
                return

            # 查询订阅
            subscribe = self._subscribeoper.get(sid)

            # 二级分类自定义配置
            category_conf = self._subscribe_confs.get(category)

            # 更新订阅自定义配置
            self._subscribeoper.update(sid, {
                'include': category_conf.get('include') if category_conf.get('include') else subscribe.include,
                'exclude': category_conf.get('exclude') if category_conf.get('exclude') else subscribe.exclude,
                'sites': json.dumps(category_conf.get('sites')) if category_conf.get('sites') else subscribe.sites,
                'resolution': self.__parse_pix(category_conf.get('resolution')) if category_conf.get(
                    'resolution') else subscribe.resolution,
                'quality': self.__parse_type(category_conf.get('quality')) if category_conf.get(
                    'quality') else subscribe.quality,
                'effect': self.__parse_effect(category_conf.get('effect')) if category_conf.get(
                    'effect') else subscribe.effect,
            })
            logger.info(f"订阅记录:{subscribe.name} 填充成功\n"
                        f"包含关键词 {category_conf.get('include') if category_conf.get('include') else subscribe.include} \n"
                        f"排除关键词 {category_conf.get('exclude') if category_conf.get('exclude') else subscribe.exclude} \n"
                        f"站点 {json.dumps(category_conf.get('sites')) if category_conf.get('sites') else subscribe.sites} \n"
                        f"分辨率 {self.__parse_pix(category_conf.get('resolution')) if category_conf.get('resolution') else subscribe.resolution} \n"
                        f"质量 {self.__parse_type(category_conf.get('quality')) if category_conf.get('quality') else subscribe.quality} \n"
                        f"特效 {self.__parse_effect(category_conf.get('effect')) if category_conf.get('effect') else subscribe.effect}")

            # 读取历史记录
            history = self.get_data('history') or []

            history.append({
                'name': subscribe.name,
                'type': f'{category}订阅自定义配置',
                'content': json.dumps(category_conf),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
            })
            # 保存历史
            self.save_data(key="history", value=history)

    @eventmanager.register(EventType.DownloadAdded)
    def download_notice(self, event: Event = None):
        """
        添加下载填充订阅制作组等信息
        """
        if not self._enabled:
            logger.error("插件未开启")
            return

        if len(self._update_details) == 0:
            logger.error("插件未开启更新填充内容")
            return

        history_handle: List[str] = self.get_data('history_handle') or []

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

            if f"{download_history.type}:{download_history.tmdbid}" in history_handle:
                logger.warning(f"下载历史:{download_history.title} 已处理过，不再重复处理")
                return

            # 保存已处理历史
            history_handle.append(f"{download_history.type}:{download_history.tmdbid}")
            self.save_data('history_handle', history_handle)

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
                # 开始填充官组和站点
                context = event_data.get("context")
                _torrent = context.torrent_info
                _meta = context.meta_info

                # 分辨率
                resource_pix = None
                if "resource_pix" in self._update_details and not subscribe.resolution:
                    resource_pix = _meta.resource_pix if _meta else None
                    if resource_pix:
                        resource_pix = self.__parse_pix(resource_pix)

                # 资源类型
                resource_type = None
                if "resource_type" in self._update_details and not subscribe.quality:
                    resource_type = _meta.resource_type if _meta else None
                    if resource_type:
                        resource_type = self.__parse_type(resource_type)
                # 特效
                resource_effect = None
                if "resource_effect" in self._update_details and not subscribe.effect:
                    resource_effect = _meta.resource_effect if _meta else None
                    if resource_effect:
                        resource_effect = self.__parse_effect(resource_effect)

                resource_team = None
                sites = None
                if ("group" in self._update_details and not subscribe.include
                        and (not subscribe.sites or len(json.loads(subscribe.sites)) == 0)):
                    # 官组
                    resource_team = _meta.resource_team if _meta else None
                    # 站点
                    sites = json.dumps([_torrent.site]) if _torrent and _torrent.site else None

                # 更新订阅记录
                if resource_pix or resource_type or resource_effect or resource_team or sites:
                    self._subscribeoper.update(subscribe.id, {
                        'include': resource_team,
                        'sites': sites,
                        'quality': resource_type,
                        'resolution': resource_pix,
                        'effect': resource_effect,
                    })
                    logger.info(f"订阅记录:{subscribe.name} 填充成功\n"
                                f"官组 {resource_team} \n"
                                f"站点 {sites} \n"
                                f"分辨率 {resource_pix} \n"
                                f"质量 {resource_type} \n"
                                f"特效 {resource_effect}")

                    # 读取历史记录
                    history = self.get_data('history') or []
                    history.append({
                        'name': subscribe.name,
                        'type': '种子下载自定义配置',
                        'content': f'官组 {resource_team} 站点 {sites} 分辨率 {resource_pix} 质量 {resource_type} 特效 {resource_effect}',
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                    })
                    # 保存历史
                    self.save_data(key="history", value=history)
                else:
                    logger.warning(f"订阅记录:{subscribe.name} 已配置相关参数，无需自动填充")

    def __parse_pix(self, resource_pix):
        # 识别1080或者4k或720
        if re.match(r"1080[pi]|x1080", resource_pix):
            resource_pix = "1080[pi]|x1080"
        if re.match(r"4K|2160p|x2160", resource_pix):
            resource_pix = "4K|2160p|x2160"
        if re.match(r"720[pi]|x720", resource_pix):
            resource_pix = "720[pi]|x720"
        return resource_pix

    def __parse_type(self, resource_type):
        if re.match(r"Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD", resource_type):
            resource_type = "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD"
        if re.match(r"Remux", resource_type):
            resource_type = "Remux"
        if re.match(r"Blu-?Ray", resource_type):
            resource_type = "Blu-?Ray"
        if re.match(r"UHD|UltraHD", resource_type):
            resource_type = "UHD|UltraHD"
        if re.match(r"WEB-?DL|WEB-?RIP", resource_type):
            resource_type = "WEB-?DL|WEB-?RIP"
        if re.match(r"HDTV", resource_type):
            resource_type = "HDTV"
        if re.match(r"[Hx].?265|HEVC", resource_type):
            resource_type = "[Hx].?265|HEVC"
        if re.match(r"[Hx].?264|AVC", resource_type):
            resource_type = "[Hx].?264|AVC"
        return resource_type

    def __parse_effect(self, resource_effect):
        if re.match(r"Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+", resource_effect):
            resource_effect = "Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+"
        if re.match(r"Dolby[\\s.]*\\+?Atmos|Atmos", resource_effect):
            resource_effect = "Dolby[\\s.]*\\+?Atmos|Atmos"
        if re.match(r"[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+", resource_effect):
            resource_effect = "[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+"
        if re.match(r"[\\s.]+SDR[\\s.]+", resource_effect):
            resource_effect = "[\\s.]+SDR[\\s.]+"
        return resource_effect

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
                                            'label': '清理历史记录',
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
                                            'model': 'clear_handle',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'update_details',
                                            'label': '填充内容',
                                            'items': [
                                                {
                                                    "title": "资源质量",
                                                    "vale": "resource_type"
                                                },
                                                {
                                                    "title": "分辨率",
                                                    "vale": "resource_pix"
                                                },
                                                {
                                                    "title": "特效",
                                                    "vale": "resource_effect"
                                                },
                                                {
                                                    "title": "制作组",
                                                    "vale": "group"
                                                }
                                            ]
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'update_confs',
                                            'label': '二级分类自定义填充',
                                            'rows': 3,
                                            'placeholder': 'category:日番#include:.*(CR.*简繁|简繁英).RLWeb|ADWeb.#sites:观众,红叶PT\n'
                                                           'category:港台剧#include:国粤'
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
                                            'text': '电视剧订阅未配置包含关键词和订阅站点等配置时，订阅或搜索下载后，'
                                                    '将下载种子的制作组和站点等信息填充到订阅信息中，以保证后续订阅资源的统一性。'
                                                    '（适用于订阅新出的电视剧。）'
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
                                            'text': '二级分类自定义填充用于根据二级分类自定义订阅规则，具体属性明细请查看电视剧订阅设置页面。'
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
                                            'text': 'category:二级分类名称,resolution:分辨率,quality:质量,effect:特效,include:包含关键词,'
                                                    'exclude:排除关键词,sites:站点名称（多个站点用逗号拼接）。'
                                                    'category必填，多组属性用#分割。例如category:动漫#resolution:1080p'
                                                    '（添加的动漫订阅，指定分辨率为1080p）。'
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
            "clear_handle": False,
            "update_details": [],
            "update_confs": "",
        }

    def get_page(self) -> List[dict]:
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照时间倒序
        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        contens = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': history.get("name")
                    },
                    {
                        'component': 'td',
                        'text': history.get("type")
                    },
                    {
                        'component': 'td',
                        'text': history.get("content")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '执行时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '订阅名称'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '更新类型'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '更新内容'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': contens
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        pass
