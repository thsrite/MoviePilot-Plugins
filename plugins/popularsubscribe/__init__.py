import time
from datetime import datetime, timedelta

import pytz

from app.chain.douban import DoubanChain
from app.chain.tmdb import TmdbChain
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.helper.subscribe import SubscribeHelper
from app.schemas import MediaType
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class PopularSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "热门媒体订阅"
    # 插件描述
    plugin_desc = "自定添加热门订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/popular.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "popularsubscribe_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _movie_enabled: bool = False
    _tv_enabled: bool = False
    # 一页多少条数据
    _movie_page_cnt: int = 30
    _tv_page_cnt: int = 30
    # 流行度最低多少
    _movie_popular_cnt: int = 0
    _tv_popula_cnt: int = 0
    _movie_cron: str = ""
    _tv_cron: str = ""

    subscribechain = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        # 停止现有任务
        self.stop_service()

        if config:
            self._movie_enabled = config.get("movie_enabled")
            self._tv_enabled = config.get("tv_enabled")
            self._movie_cron = config.get("movie_cron")
            self._tv_cron = config.get("tv_cron")
            self._movie_page_cnt = config.get("movie_page_cnt")
            self._tv_page_cnt = config.get("tv_page_cnt")
            self._movie_popular_cnt = config.get("movie_popular_cnt")
            self._tv_popula_cnt = config.get("tv_popula_cnt")

            if self._movie_enabled or self._tv_enabled:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                if self._movie_cron:
                    try:
                        self._scheduler.add_job(func=self.__popular_subscribe,
                                                trigger=CronTrigger.from_crontab(self._movie_cron),
                                                name="电影热门订阅",
                                                args=['电影', self._movie_page_cnt, self._movie_popula_cnt])
                    except Exception as err:
                        logger.error(f"电影热门订阅定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"电影热门订阅执行周期配置错误：{err}")

                if self._tv_cron:
                    try:
                        self._scheduler.add_job(func=self.__popular_subscribe,
                                                trigger=CronTrigger.from_crontab(self._tv_cron),
                                                name="电视剧热门订阅",
                                                args=['电视剧', self._tv_page_cnt, self._tv_popula_cnt])
                    except Exception as err:
                        logger.error(f"电视剧热门订阅定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"电视剧热门订阅执行周期配置错误：{err}")
                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __popular_subscribe(self, stype, page_cnt, popular_cnt):
        """
        热门订阅
        """
        subscribes = SubscribeHelper().get_statistic(stype=stype, page=1, count=page_cnt)
        if not subscribes:
            logger.error(f"没有获取到{stype}热门订阅")
            return

        history: List[dict] = self.get_data('history') or []

        # 遍历热门订阅检查流行度是否达到要求
        for sub in subscribes:
            media = MediaInfo()
            media.type = MediaType(sub.get("type"))
            media.title = sub.get("name")
            media.year = sub.get("year")
            media.tmdb_id = sub.get("tmdbid")
            media.douban_id = sub.get("doubanid")
            media.bangumi_id = sub.get("bangumiid")
            media.tvdb_id = sub.get("tvdbid")
            media.imdb_id = sub.get("imdbid")
            media.season = sub.get("season")
            media.poster_path = sub.get("poster")

            # 元数据
            meta = MetaInfo(media.title)

            # 查询缺失的媒体信息
            exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=media)
            if exist_flag:
                logger.info(f'{media.title_year} 媒体库中已存在')
                continue

            # 判断用户是否已经添加订阅
            if self.subscribechain.exists(mediainfo=media):
                logger.info(f'{media.title_year} 订阅已存在')
                continue

            # 添加订阅
            self.subscribechain.add(title=media.title,
                                    year=media.year,
                                    mtype=media.type,
                                    tmdbid=media.tmdb_id,
                                    doubanid=media.douban_id,
                                    exist_ok=True,
                                    username=settings.SUPERUSER)
            logger.info(f'{media.title_year} 添加订阅')

            # 存储历史记录
            history.append({
                "title": media.title,
                "type": media.type.value,
                "year": media.year,
                "poster": media.get_poster_image(),
                "overview": media.overview,
                "tmdbid": media.tmdb_id,
                "doubanid": media.douban_id,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        # 保存历史记录
        self.save_data('history', history)

    def get_state(self) -> bool:
        return self._movie_enabled or self._tv_enabled

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
                                            'model': 'movie_enabled',
                                            'label': '电影热门订阅',
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
                                            'model': 'movie_cron',
                                            'label': '电影订阅周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'model': 'movie_page_cnt',
                                            'label': '电影获取条数',
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
                                            'model': 'movie_popular_cnt',
                                            'label': '电影流行指数',
                                            'placeholder': '0'
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'tv_enabled',
                                            'label': '电视剧热门订阅',
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
                                            'model': 'tv_cron',
                                            'label': '电视剧订阅周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                            'model': 'tv_page_cnt',
                                            'label': '电视剧获取条数',
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
                                            'model': 'tv_popular_cnt',
                                            'label': '电视剧流行指数',
                                            'placeholder': '0'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                ]
            }
        ], {
            "movie_enabled": False,
            "tv_enabled": False,
            "movie_cron": "5 1 * * *",
            "tv_cron": "5 1 * * *",
            "movie_page_cnt": "",
            "tv_page_cnt": "",
            "movie_popular_cnt": "",
            "tv_popular_cnt": "",
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
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
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            doubanid = history.get("doubanid")
            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': poster,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VCardSubtitle',
                                            'props': {
                                                'class': 'pa-2 font-bold break-words whitespace-break-spaces'
                                            },
                                            'content': [
                                                {
                                                    'component': 'a',
                                                    'props': {
                                                        'href': f"https://movie.douban.com/subject/{doubanid}",
                                                        'target': '_blank'
                                                    },
                                                    'text': title
                                                }
                                            ]
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'类型：{mtype}'
                                        },
                                        {
                                            'component': 'VCardText',
                                            'props': {
                                                'class': 'pa-0 px-2'
                                            },
                                            'text': f'时间：{time_str}'
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
