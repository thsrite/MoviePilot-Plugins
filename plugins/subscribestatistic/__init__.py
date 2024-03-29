import json
from datetime import datetime, timedelta

from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from app.db.subscribe_oper import SubscribeOper
from typing import Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.core.config import settings
from app.schemas import NotificationType
from app.schemas.types import SystemConfigKey


class SubscribeStatistic(_PluginBase):
    # 插件名称
    plugin_name = "订阅下载统计"
    # 插件描述
    plugin_desc = "统计指定时间内各站点订阅及下载情况。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/subscribestatistic.png"
    # 插件版本
    plugin_version = "1.5"
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
    _notify = False
    _onlyonce = False
    _movie_subscribe_days = None
    _tv_subscribe_days = None
    _movie_download_days = None
    _tv_download_days = None
    _notify_type = None
    _msgtype = None
    subscribe = None
    downloadhis = None
    siteoper = None
    _cron: str = ""

    def init_plugin(self, config: dict = None):
        self.subscribe = SubscribeOper()
        self.downloadhis = DownloadHistoryOper()
        self.siteoper = SiteOper()
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._movie_subscribe_days = config.get("movie_subscribe_days")
            self._tv_subscribe_days = config.get("tv_subscribe_days")
            self._movie_download_days = config.get("movie_download_days")
            self._tv_download_days = config.get("tv_download_days")
            self._notify_type = config.get("notify_type")
            self._msgtype = config.get("msgtype")

            if self._enabled and (
                    self._cron or self._onlyonce) and self._notify and self._msgtype and self._notify_type:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"订阅下载统计服务启动，立即运行一次")
                    self._scheduler.add_job(self.notify, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="订阅下载统计")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.notify,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="订阅下载统计")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def notify(self):
        """
        发送统计消息
        """
        text = ""
        if 'movie_subscribes' in self._notify_type:
            text += "【电影订阅统计】\n"
            _, movie_subscribe_sites, movie_subscribe_datas = self.__get_movie_subscribes()
            movie_subscribe_dict = dict(zip(movie_subscribe_sites, movie_subscribe_datas))
            movie_subscribe_dict = dict(sorted(movie_subscribe_dict.items(), key=lambda x: x[1], reverse=True))
            for movie_subscribe_site in movie_subscribe_dict.keys():
                text += f"{movie_subscribe_site}: {movie_subscribe_dict[movie_subscribe_site]}\n"
            text += "\n"

        if 'tv_subscribes' in self._notify_type:
            text += "【电视剧订阅统计】\n"
            _, tv_subscribe_sites, tv_subscribe_datas = self.__get_tv_subscribes()
            tv_subscribe_dict = dict(zip(tv_subscribe_sites, tv_subscribe_datas))
            tv_subscribe_dict = dict(sorted(tv_subscribe_dict.items(), key=lambda x: x[1], reverse=True))
            for tv_subscribe_site in tv_subscribe_dict.keys():
                text += f"{tv_subscribe_site}: {tv_subscribe_dict[tv_subscribe_site]}\n"
            text += "\n"

        if 'movie_downloads' in self._notify_type:
            text += "【电影下载统计】\n"
            _, movie_download_sites, movie_download_datas = self.__get_movie_downloads()
            movie_download_dict = dict(zip(movie_download_sites, movie_download_datas))
            movie_download_dict = dict(sorted(movie_download_dict.items(), key=lambda x: x[1], reverse=True))
            for movie_download_site in movie_download_dict.keys():
                text += f"{movie_download_site}: {movie_download_dict[movie_download_site]}\n"
            text += "\n"

        if 'tv_downloads' in self._notify_type:
            text += "【电视剧下载统计】\n"
            _, tv_download_sites, tv_download_datas = self.__get_tv_downloads()
            tv_download_dict = dict(zip(tv_download_sites, tv_download_datas))
            tv_download_dict = dict(sorted(tv_download_dict.items(), key=lambda x: x[1], reverse=True))
            for tv_download_site in tv_download_dict.keys():
                text += f"{tv_download_site}: {tv_download_dict[tv_download_site]}\n"

        # 发送通知
        mtype = NotificationType.Manual
        if self._msgtype:
            mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual

        self.post_message(title="订阅下载统计",
                          mtype=mtype,
                          text=text)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def __get_movie_subscribes(self):
        """
        获取电影订阅统计数据
        """
        # 电影订阅
        movie_subscribes = self.subscribe.list_by_type(mtype='电影', days=self._movie_subscribe_days)
        movie_subscribe_sites = []
        movie_subscribe_datas = []
        if movie_subscribes:
            movie_subscribe_site_ids = []
            for movie_subscribe in movie_subscribes:
                if movie_subscribe.sites:
                    movie_subscribe_site_ids += [site for site in json.loads(movie_subscribe.sites)]
                else:
                    movie_subscribe_site_ids += self.systemconfig.get(SystemConfigKey.RssSites) or []

            for movie_subscribe_site_id in movie_subscribe_site_ids:
                site = self.siteoper.get(movie_subscribe_site_id)
                if site:
                    if not movie_subscribe_sites.__contains__(site.name):
                        movie_subscribe_sites.append(site.name)
                        movie_subscribe_datas.append(movie_subscribe_site_ids.count(movie_subscribe_site_id))

        return movie_subscribes, movie_subscribe_sites, movie_subscribe_datas

    def __get_tv_subscribes(self):
        """
        获取电视剧订阅统计数据
        """
        tv_subscribes = self.subscribe.list_by_type(mtype='电视剧', days=self._tv_subscribe_days)
        tv_subscribe_sites = []
        tv_subscribe_datas = []
        if tv_subscribes:
            tv_subscribe_site_ids = []
            for tv_subscribe in tv_subscribes:
                if tv_subscribe.sites:
                    tv_subscribe_site_ids += [site for site in json.loads(tv_subscribe.sites)]
                else:
                    tv_subscribe_site_ids += self.systemconfig.get(SystemConfigKey.RssSites) or []

            for tv_subscribe_site_id in tv_subscribe_site_ids:
                site = self.siteoper.get(tv_subscribe_site_id)
                if site:
                    if not tv_subscribe_sites.__contains__(site.name):
                        tv_subscribe_sites.append(site.name)
                        tv_subscribe_datas.append(tv_subscribe_site_ids.count(tv_subscribe_site_id))

        return tv_subscribes, tv_subscribe_sites, tv_subscribe_datas

    def __get_movie_downloads(self):
        """
        获取电影下载统计数据
        """
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

        return movie_downloads, movie_download_sites, movie_download_datas

    def __get_tv_downloads(self):
        """
        获取电视剧下载统计数据
        """
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

        return tv_downloads, tv_download_sites, tv_download_datas

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'notify_type',
                                            'label': '推送类型',
                                            'items': [
                                                {'title': '电影订阅', 'value': 'movie_subscribes'},
                                                {'title': '电视剧订阅', 'value': 'tv_subscribes'},
                                                {'title': '电影下载', 'value': 'movie_downloads'},
                                                {'title': '电视剧下载', 'value': 'tv_downloads'},
                                            ]
                                        }
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
                                            'md': 4
                                        },
                                        'content': [
                                            {
                                                'component': 'VSelect',
                                                'props': {
                                                    'multiple': False,
                                                    'chips': True,
                                                    'model': 'msgtype',
                                                    'label': '消息类型',
                                                    'items': MsgTypeOptions
                                                }
                                            }
                                        ]
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
            "notify": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "movie_subscribe_days": 30,
            "tv_subscribe_days": 30,
            "movie_download_days": 7,
            "tv_download_days": 7,
            "notify_type": "",
            "msgtype": ""
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
        movie_subscribes, movie_subscribe_sites, movie_subscribe_datas = self.__get_movie_subscribes()

        # 电视剧订阅
        tv_subscribes, tv_subscribe_sites, tv_subscribe_datas = self.__get_tv_subscribes()

        # 电影下载
        movie_downloads, movie_download_sites, movie_download_datas = self.__get_movie_downloads()

        # 电视剧下载
        tv_downloads, tv_download_sites, tv_download_datas = self.__get_tv_downloads()

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
                                            'text': '订阅未选择站点或站点已删除'
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
                                            'text': '订阅未选择站点或站点已删除'
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
