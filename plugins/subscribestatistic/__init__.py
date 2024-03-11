import json

from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from app.db.subscribe_oper import SubscribeOper
from typing import Any, List, Dict, Tuple


class SubscribeStatistic(_PluginBase):
    # 插件名称
    plugin_name = "订阅下载统计"
    # 插件描述
    plugin_desc = "统计指定时间内各站点订阅及下载情况。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/subscribestatistic.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribestatistic_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 任务执行间隔
    _enabled = False
    _movie_subscribe_days = None
    _tv_subscribe_days = None
    _movie_download_days = None
    _tv_download_days = None
    subscribe = None
    downloadhis = None
    siteoper = None

    def init_plugin(self, config: dict = None):
        self.subscribe = SubscribeOper()
        self.downloadhis = DownloadHistoryOper()
        self.siteoper = SiteOper()
        if config:
            self._enabled = config.get("enabled")
            self._movie_subscribe_days = config.get("movie_subscribe_days")
            self._tv_subscribe_days = config.get("tv_subscribe_days")
            self._movie_download_days = config.get("movie_download_days")
            self._tv_download_days = config.get("tv_download_days")

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
                                    'md': 4
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
                            # {
                            #     'component': 'VCol',
                            #     'props': {
                            #         'cols': 12,
                            #         'md': 4
                            #     },
                            #     'content': [
                            #         {
                            #             'component': 'VSwitch',
                            #             'props': {
                            #                 'model': 'notify',
                            #                 'label': '发送通知',
                            #             }
                            #         }
                            #     ]
                            # },
                            # {
                            #     'component': 'VCol',
                            #     'props': {
                            #         'cols': 12,
                            #         'md': 4
                            #     },
                            #     'content': [
                            #         {
                            #             'component': 'VSwitch',
                            #             'props': {
                            #                 'model': 'onlyonce',
                            #                 'label': '立即运行一次',
                            #             }
                            #         }
                            #     ]
                            # }
                        ]
                    },
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'movie_subscribe_days',
                                            'label': '电影订阅天数',
                                            'placeholder': '30'
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
                                            'model': 'tv_subscribe_days',
                                            'label': '电视剧订阅天数',
                                            'placeholder': '30'
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
                                            'model': 'movie_download_days',
                                            'label': '电影下载天数',
                                            'placeholder': '7'
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
                                            'model': 'tv_download_days',
                                            'label': '电视剧下载天数',
                                            'placeholder': '7'
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
                                            'text': '订阅数量：MoviePilot指定天数内正在订阅的数量。'
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
                                            'text': '下载数量：通过MoviePilot下载的数量，包括订阅下载、手动下载以及其他下载等场景。'
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
            "movie_subscribe_days": 30,
            "tv_subscribe_days": 30,
            "movie_download_days": 7,
            "tv_download_days": 7
        }

    def get_page(self) -> List[dict]:
        if not self._enabled:
            return [
                {
                    'component': 'div',
                    'text': '暂未开启插件',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        # 电影订阅
        movie_subscribes = self.subscribe.list_by_type(mtype='电影', days=self._movie_subscribe_days)
        movie_subscribe_sites = []
        movie_subscribe_datas = []
        if movie_subscribes:
            movie_subscribe_site_ids = []
            for movie_subscribe in movie_subscribes:
                if movie_subscribe.sites:
                    movie_subscribe_site_ids += [site for site in json.loads(movie_subscribe.sites)]

            for movie_subscribe_site_id in movie_subscribe_site_ids:
                site = self.siteoper.get(movie_subscribe_site_id)
                if site:
                    if not movie_subscribe_sites.__contains__(site.name):
                        movie_subscribe_sites.append(site.name)
                        movie_subscribe_datas.append(movie_subscribe_site_ids.count(movie_subscribe_site_id))

        # 电视剧订阅
        tv_subscribes = self.subscribe.list_by_type(mtype='电视剧', days=self._movie_subscribe_days)
        tv_subscribe_sites = []
        tv_subscribe_datas = []
        if tv_subscribes:
            tv_subscribe_site_ids = []
            for tv_subscribe in tv_subscribes:
                if tv_subscribe.sites:
                    tv_subscribe_site_ids += [site for site in json.loads(tv_subscribe.sites)]

            for tv_subscribe_site_id in tv_subscribe_site_ids:
                site = self.siteoper.get(tv_subscribe_site_id)
                if site:
                    if not tv_subscribe_sites.__contains__(site.name):
                        tv_subscribe_sites.append(site.name)
                        tv_subscribe_datas.append(tv_subscribe_site_ids.count(tv_subscribe_site_id))

        # 电影下载
        movie_downloads = self.downloadhis.list_by_type(mtype="电影", days=self._movie_download_days)
        movie_download_sites = []
        movie_download_datas = []
        if movie_downloads:
            movie_download_sites2 = []
            for movie_download in movie_downloads:
                if movie_download.torrent_site:
                    movie_download_sites2.append(movie_download.torrent_site)

            for movie_download_site in movie_download_sites2:
                if not movie_download_sites.__contains__(movie_download_site):
                    movie_download_sites.append(movie_download_site)
                if not movie_download_datas.__contains__(movie_download_site):
                    movie_download_datas.append(movie_download_sites2.count(movie_download_site))

        # 电视剧下载
        tv_downloads = self.downloadhis.list_by_type(mtype="电视剧", days=self._tv_download_days)
        tv_download_sites = []
        tv_download_datas = []
        if tv_downloads:
            tv_download_sites2 = []
            for tv_download in tv_downloads:
                if tv_download.torrent_site:
                    tv_download_sites2.append(tv_download.torrent_site)

            for tv_download_site in tv_download_sites2:
                if not tv_download_sites.__contains__(tv_download_site):
                    tv_download_sites.append(tv_download_site)
                if not tv_download_datas.__contains__(tv_download_site):
                    tv_download_datas.append(tv_download_sites2.count(tv_download_site))

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    # 电影订阅图表
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VApexChart',
                                'props': {
                                    'height': 300,
                                    'options': {
                                        'chart': {
                                            'type': 'pie',
                                        },
                                        'labels': movie_subscribe_sites,
                                        'title': {
                                            'text': f'电影近 {self._movie_subscribe_days} 天订阅 {len(movie_subscribes)} 部'
                                        },
                                        'legend': {
                                            'show': True
                                        },
                                        'plotOptions': {
                                            'pie': {
                                                'expandOnClick': False
                                            }
                                        },
                                        'noData': {
                                            'text': '暂无数据'
                                        }
                                    },
                                    'series': movie_subscribe_datas
                                }
                            }
                        ]
                    },
                    # 电视剧订阅图表
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VApexChart',
                                'props': {
                                    'height': 300,
                                    'options': {
                                        'chart': {
                                            'type': 'pie',
                                        },
                                        'labels': tv_subscribe_sites,
                                        'title': {
                                            'text': f'电视剧近 {self._tv_subscribe_days} 天订阅 {len(tv_subscribes)} 部'
                                        },
                                        'legend': {
                                            'show': True
                                        },
                                        'plotOptions': {
                                            'pie': {
                                                'expandOnClick': False
                                            }
                                        },
                                        'noData': {
                                            'text': '暂无数据'
                                        }
                                    },
                                    'series': tv_subscribe_datas
                                }
                            }
                        ]
                    },
                    # 电影下载图表
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VApexChart',
                                'props': {
                                    'height': 300,
                                    'options': {
                                        'chart': {
                                            'type': 'pie',
                                        },
                                        'labels': movie_download_sites,
                                        'title': {
                                            'text': f'电影近 {self._movie_download_days} 天下载 {len(movie_downloads)} 个种子'
                                        },
                                        'legend': {
                                            'show': True
                                        },
                                        'plotOptions': {
                                            'pie': {
                                                'expandOnClick': False
                                            }
                                        },
                                        'noData': {
                                            'text': '暂无数据'
                                        }
                                    },
                                    'series': movie_download_datas
                                }
                            }
                        ]
                    },
                    # 电视剧下载图表
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VApexChart',
                                'props': {
                                    'height': 300,
                                    'options': {
                                        'chart': {
                                            'type': 'pie',
                                        },
                                        'labels': tv_download_sites,
                                        'title': {
                                            'text': f'电视剧近 {self._tv_download_days} 天下载 {len(tv_downloads)} 个种子'
                                        },
                                        'legend': {
                                            'show': True
                                        },
                                        'plotOptions': {
                                            'pie': {
                                                'expandOnClick': False
                                            }
                                        },
                                        'noData': {
                                            'text': '暂无数据'
                                        }
                                    },
                                    'series': tv_download_datas
                                }
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
