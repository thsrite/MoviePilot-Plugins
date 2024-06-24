from datetime import datetime, timedelta

import pytz
import cn2an

from app import schemas
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
from app.modules.themoviedb.tmdbapi import TmdbApi


class PopularSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "热门媒体订阅"
    # 插件描述
    plugin_desc = "自定添加热门电影、电视剧、动漫到订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/popular.png"
    # 插件版本
    plugin_version = "1.7"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "popularsubscribe_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _movie_enabled: bool = False
    _tv_enabled: bool = False
    _anime_enabled: bool = False
    # 一页多少条数据
    _movie_page_cnt: int = 30
    _tv_page_cnt: int = 30
    _anime_page_cnt: int = 30
    # 流行度最低多少
    _movie_popular_cnt: int = 0
    _tv_popular_cnt: int = 0
    _anime_popular_cnt: int = 0
    _movie_cron: str = ""
    _tv_cron: str = ""
    _anime_cron: str = ""
    _onlyonce: bool = False
    _clear = False
    _clear_already_handle = False
    _username = None

    downloadchain = None
    subscribechain = None
    tmdb = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.tmdb = TmdbApi()
        # 停止现有任务
        self.stop_service()

        if config:
            self._movie_enabled = config.get("movie_enabled")
            self._tv_enabled = config.get("tv_enabled")
            self._anime_enabled = config.get("anime_enabled")
            self._movie_cron = config.get("movie_cron")
            self._tv_cron = config.get("tv_cron")
            self._anime_cron = config.get("anime_cron")
            self._movie_page_cnt = config.get("movie_page_cnt")
            self._tv_page_cnt = config.get("tv_page_cnt")
            self._anime_page_cnt = config.get("anime_page_cnt")
            self._movie_popular_cnt = config.get("movie_popular_cnt")
            self._tv_popular_cnt = config.get("tv_popular_cnt")
            self._anime_popular_cnt = config.get("anime_popular_cnt")
            self._clear = config.get("clear")
            self._clear_already_handle = config.get("clear_already_handle")
            self._username = config.get("username") or '热门订阅'
            _onlyonce2 = config.get("onlyonce")

            # 清理插件订阅历史
            if self._clear:
                self.del_data(key="history")

                self._clear = False
                self.__update_config()
                logger.info("订阅历史清理完成")

            # 清理已处理历史
            if self._clear_already_handle:
                self.del_data(key="already_handle")

                self._clear_already_handle = False
                self.__update_config()
                logger.info("已处理历史清理完成")

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._movie_enabled and (self._movie_cron or _onlyonce2):
                if self._movie_cron:
                    try:
                        self._scheduler.add_job(func=self.__popular_subscribe,
                                                trigger=CronTrigger.from_crontab(self._movie_cron),
                                                name="电影热门订阅",
                                                args=['电影', self._movie_page_cnt, self._movie_popular_cnt])
                    except Exception as err:
                        logger.error(f"电影热门订阅定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"电影热门订阅执行周期配置错误：{err}")

                if _onlyonce2:
                    logger.info(f"电影热门订阅服务启动，立即运行一次")
                    self._scheduler.add_job(self.__popular_subscribe, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="电影热门订阅",
                                            args=['电影', self._movie_page_cnt, self._movie_popular_cnt])
                    self._onlyonce = False
                    self.__update_config()

            if self._tv_enabled and (self._tv_cron or _onlyonce2):
                if self._tv_cron:
                    try:
                        self._scheduler.add_job(func=self.__popular_subscribe,
                                                trigger=CronTrigger.from_crontab(self._tv_cron),
                                                name="电视剧热门订阅",
                                                args=['电视剧', self._tv_page_cnt, self._tv_popular_cnt])
                    except Exception as err:
                        logger.error(f"电视剧热门订阅定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"电视剧热门订阅执行周期配置错误：{err}")

                    if _onlyonce2:
                        logger.info(f"电视剧热门订阅服务启动，立即运行一次")
                        self._scheduler.add_job(self.__popular_subscribe, 'date',
                                                run_date=datetime.now(
                                                    tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                                name="电视剧热门订阅",
                                                args=['电视剧', self._tv_page_cnt, self._tv_popular_cnt])
                        self._onlyonce = False
                        self.__update_config()

            if self._anime_enabled and (self._anime_cron or _onlyonce2):
                if self._anime_cron:
                    try:
                        self._scheduler.add_job(func=self.__popular_subscribe,
                                                trigger=CronTrigger.from_crontab(self._anime_cron),
                                                name="动漫热门订阅",
                                                args=['动漫', self._anime_page_cnt, self._anime_popular_cnt])
                    except Exception as err:
                        logger.error(f"动漫热门订阅定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"动漫热门订阅执行周期配置错误：{err}")

                if _onlyonce2:
                    logger.info(f"动漫热门订阅服务启动，立即运行一次")
                    self._scheduler.add_job(self.__popular_subscribe, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="动漫热门订阅",
                                            args=['动漫', self._anime_page_cnt, self._anime_popular_cnt])
                    self._onlyonce = False
                    self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "movie_enabled": self._movie_enabled,
            "tv_enabled": self._tv_enabled,
            "anime_enabled": self._anime_enabled,
            "movie_cron": self._movie_cron,
            "tv_cron": self._tv_cron,
            "anime_cron": self._anime_cron,
            "movie_page_cnt": self._movie_page_cnt,
            "tv_page_cnt": self._tv_page_cnt,
            "anime_page_cnt": self._anime_page_cnt,
            "movie_popular_cnt": self._movie_popular_cnt,
            "tv_popular_cnt": self._tv_popular_cnt,
            "anime_popular_cnt": self._anime_popular_cnt,
            "clear": self._clear,
            "clear_already_handle": self._clear_already_handle,
            "onlyonce": self._onlyonce,
            "username": self._username
        })

    def __popular_subscribe(self, stype, page_cnt, popular_cnt):
        """
        热门订阅
        """
        true_type = stype
        true_cnt = page_cnt
        if str(stype) == '动漫':
            stype = "电视剧"
            # 动漫|电视剧 公用一组数据，取所需数据的20倍应该ok吧
            page_cnt = int(page_cnt) * 20

        subscribes = SubscribeHelper().get_statistic(stype=stype, page=1, count=page_cnt)
        if not subscribes:
            logger.error(f"没有获取到{true_type}热门订阅")
            return

        history: List[dict] = self.get_data('history') or []
        already_handle: List[dict] = self.get_data('already_handle') or []

        # 遍历热门订阅检查流行度是否达到要求
        tv_anime_cnt = 0
        for sub in subscribes:
            if popular_cnt and sub.get("count") and int(popular_cnt) > int(sub.get("count")):
                logger.info(
                    f"{sub.get('name')} 订阅人数：{sub.get('count')} 小于 设定人数：{popular_cnt}，跳过")
                continue

            media = MediaInfo()
            media.tmdb_id = sub.get("tmdbid")
            media.type = MediaType(sub.get("type"))
            media.title = sub.get("name")
            media.year = sub.get("year")
            media.douban_id = sub.get("doubanid")
            media.bangumi_id = sub.get("bangumiid")
            media.tvdb_id = sub.get("tvdbid")
            media.imdb_id = sub.get("imdbid")
            media.season = sub.get("season")
            media.poster_path = sub.get("poster")

            # 元数据
            meta = MetaInfo(media.title)

            # 电视剧特殊处理：动漫|电视剧
            if str(stype) == "电视剧":
                # 动漫|电视剧所需请求数量以达到
                if int(tv_anime_cnt) >= int(true_cnt):
                    break

                # 根据tmdbid获取媒体信息
                tmdb_info = self.tmdb.get_info(mtype=media.type, tmdbid=media.tmdb_id)
                if not tmdb_info:
                    logger.warn(f'未识别到媒体信息，标题：{media.title}，tmdbid：{media.tmdb_id}')
                    continue

                # 获取媒体类型
                genre_ids = tmdb_info.get("genre_ids") or []
                if genre_ids:
                    # 如果当前是动漫订阅，则判断是否在动漫分类中，如果不在则跳过
                    if str(true_type) == '动漫' and not set(genre_ids).intersection(set(settings.ANIME_GENREIDS)):
                        logger.debug(f'{media.title_year} 不在动漫分类中，跳过')
                        continue
                    # 如果当前是电视剧订阅，则判断是否在动漫分类中，如果在则跳过
                    if str(true_type) == '电视剧' and set(genre_ids).intersection(set(settings.ANIME_GENREIDS)):
                        logger.debug(f'{media.title_year} 在动漫分类中，跳过')
                        continue

                # 电视剧|动漫分类都通过，则计数
                tv_anime_cnt += 1

            if media.title_year in already_handle:
                logger.info(f"{media.type.value} {media.title_year} 已被处理，跳过")
                continue
            already_handle.append(media.title_year)

            title = media.title_year
            season_str = None
            if media.season and int(media.season) > 1:
                # 小写数据转大写
                season_str = f"第{cn2an.an2cn(media.season, 'low')}季"
                title = f"{media.title_year} {season_str}"
            logger.info(f"{title} 订阅人数：{sub.get('count')} 满足 设定人数：{popular_cnt}")

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
                                    season=media.season,
                                    doubanid=media.douban_id,
                                    exist_ok=True,
                                    username=self._username)
            logger.info(f'{media.title_year} 订阅人数：{sub.get("count")} 添加订阅')

            # 存储历史记录
            history.append({
                "title": media.title,
                "type": media.type.value,
                "year": media.year,
                "season": season_str,
                "poster": media.get_poster_image(),
                "overview": media.overview,
                "tmdbid": media.tmdb_id,
                "doubanid": media.douban_id,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "unique": f"{media.title}:{media.tmdb_id}:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
            })

        # 保存历史记录
        self.save_data('history', history)
        self.save_data('already_handle', already_handle)
        logger.info(f"{true_type}热门订阅检查完成")

    def delete_history(self, key: str, apikey: str):
        """
        删除同步历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def get_state(self) -> bool:
        return self._movie_enabled or self._tv_enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除订阅历史记录"
            }
        ]

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
                                            'label': '电影订阅人次',
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
                                            'label': '电视剧订阅人次',
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
                                            'model': 'anime_enabled',
                                            'label': '动漫热门订阅',
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
                                            'model': 'anime_cron',
                                            'label': '动漫订阅周期',
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
                                            'model': 'anime_page_cnt',
                                            'label': '动漫获取条数',
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
                                            'model': 'anime_popular_cnt',
                                            'label': '动漫订阅人次',
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '获取指定条数的热门媒体，自定义最低订阅人数要求进行订阅。'
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
                                            'type': 'error',
                                            'variant': 'tonal',
                                            'text': '立即运行一次：立即运行一次已开启的电影/电视剧/动漫订阅。'
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清理订阅记录',
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
                                            'model': 'clear_already_handle',
                                            'label': '清理已处理记录',
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
                                            'model': 'username',
                                            'label': '订阅用户',
                                            'placeholder': '默认为`热门订阅`'
                                        }
                                    }
                                ]
                            },
                        ]
                    }
                ]
            }
        ], {
            "movie_enabled": False,
            "tv_enabled": False,
            "anime_enabled": False,
            "movie_cron": "5 1 * * *",
            "tv_cron": "5 1 * * *",
            "anime_cron": "5 1 * * *",
            "movie_page_cnt": "",
            "tv_page_cnt": "",
            "anime_page_cnt": "",
            "movie_popular_cnt": "",
            "tv_popular_cnt": "",
            "anime_popular_cnt": "",
            "onlyonce": False,
            "clear": False,
            "clear_already_handle": False,
            "username": "热门订阅"
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
            year = history.get("year")
            season = history.get("season")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            tmdbid = history.get("tmdbid")
            doubanid = history.get("doubanid")
            unique = history.get("unique")

            if season:
                contents.append(
                    {
                        'component': 'VCard',
                        'content': [
                            {
                                "component": "VDialogCloseBtn",
                                "props": {
                                    'innerClass': 'absolute top-0 right-0',
                                },
                                'events': {
                                    'click': {
                                        'api': 'plugin/PopularSubscribe/delete_history',
                                        'method': 'get',
                                        'params': {
                                            'key': unique,
                                            'apikey': settings.API_TOKEN
                                        }
                                    }
                                },
                            },
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
                                                'text': f'年份：{year}'
                                            },
                                            {
                                                'component': 'VCardText',
                                                'props': {
                                                    'class': 'pa-0 px-2'
                                                },
                                                'text': f'季度：{season}'
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
            else:
                contents.append(
                    {
                        'component': 'VCard',
                        'content': [
                            {
                                "component": "VDialogCloseBtn",
                                "props": {
                                    'innerClass': 'absolute top-0 right-0',
                                },
                                'events': {
                                    'click': {
                                        'api': 'plugin/PopularSubscribe/delete_history',
                                        'method': 'get',
                                        'params': {
                                            'key': f"popularsubscribe: {title} (DB:{tmdbid})",
                                            'apikey': settings.API_TOKEN
                                        }
                                    }
                                },
                            },
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
                                                'text': f'年份：{year}'
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
