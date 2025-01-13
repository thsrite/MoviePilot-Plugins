from datetime import datetime, timedelta

import pytz

from app import schemas
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.modules.themoviedb import TmdbApi
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas import MediaType
from app.schemas.types import EventType
from app.utils.string import StringUtils


class MediaRelease(_PluginBase):
    # 插件名称
    plugin_name = "影视将映订阅"
    # 插件描述
    plugin_desc = "监控未上线影视作品，自动添加订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/mediarelease.png"
    # 插件版本
    plugin_version = "1.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "mediarelease_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    subscribechain = None
    downloadchain = None
    tmdb = None
    _scheduler: Optional[BackgroundScheduler] = None
    _clear = False
    _movies = None
    _tvs = None

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.tmdb = TmdbApi()
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._clear = config.get("clear")
            self._movies = config.get("movies")
            self._tvs = config.get("tvs")

            # 清理插件订阅历史
            if self._clear:
                self.del_data(key="history")

                self._clear = False
                self.__update_config()
                logger.info("订阅历史清理完成")

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"影视将映订阅服务启动，立即运行一次")
                    self._scheduler.add_job(self.__release, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="影视将映订阅")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__release,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="影视将映订阅")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __release(self):
        """
        影视将映订阅
        """
        if not self._movies and not self._tvs:
            logger.warn("暂无作品订阅，停止运行")
            return

        history: List[dict] = self.get_data('history') or []

        # 检查订阅
        if self._movies:
            logger.info("开始检查将映电影")
            noexist_medias, history = self.__subscribe(self._movies, MediaType.MOVIE, history)
            self._movies = ",".join(noexist_medias)
            # 保存配置
            self.__update_config()

        # 检查订阅
        if self._tvs:
            logger.info("开始检查将映电视剧")
            noexist_medias, history = self.__subscribe(self._tvs, MediaType.TV, history)
            self._tvs = ",".join(noexist_medias)
            # 保存配置
            self.__update_config()

        # 保存历史记录
        self.save_data('history', history)
        logger.info(f"影视将映订阅任务完成")

    def __subscribe(self, medias, mtype: MediaType, history):
        noexist_medias = []
        for media_name in medias.split(","):
            if not media_name:
                continue
            # 提取要素
            _, key_word, season_num, episode_num, year, content = StringUtils.get_keyword(media_name)
            # 元数据
            meta = MetaInfo(key_word)
            meta.type = mtype
            if season_num:
                meta.begin_season = season_num
            if episode_num:
                meta.begin_episode = episode_num
            if year:
                meta.year = year
            if mtype == MediaType.MOVIE:
                search_medias = self.tmdb.search_movies(meta.name, meta.year)
            else:
                search_medias = self.tmdb.search_tvs(meta.name, meta.year)

            search_medias = [MediaInfo(tmdb_info=info) for info in search_medias]
            if not search_medias:
                logger.warn(f"{mtype.value} {media_name} 在TMDB中未找到")
                noexist_medias.append(media_name)
                continue

            for mediainfo in search_medias:
                # 查询缺失的媒体信息
                exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.warn(f'{mediainfo.title_year} 媒体库中已存在')
                    continue

                # 判断用户是否已经添加订阅
                if self.subscribechain.exists(mediainfo=mediainfo):
                    logger.warn(f'{mediainfo.title_year} 订阅已存在')
                    continue

                # 开始订阅
                logger.info(
                    f"开始订阅 {mtype.value} {mediainfo.title_year} TMDBID {mediainfo.tmdb_id}")
                # 添加订阅
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        tmdbid=mediainfo.tmdb_id,
                                        doubanid=mediainfo.douban_id,
                                        exist_ok=True,
                                        username="影视将映订阅")

                # 存储历史记录
                history.append({
                    "title": mediainfo.title,
                    "type": mtype.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "tmdbid": mediainfo.tmdb_id,
                    "doubanid": mediainfo.douban_id,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "unique": f"mediarelease: {mediainfo.title} (DB:{mediainfo.tmdb_id})"
                })

        logger.info(f"{mtype.value} 将映订阅任务完成")

        return noexist_medias, history

    @eventmanager.register(EventType.PluginAction)
    def remote_subscribe(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "media_release":
                return
            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return
            args = args.split(" ")
            if len(args) < 2:
                logger.error(f"参数错误：{event_data} 电影/电视剧 名称 年份")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"参数错误！格式：电影/电视剧 名称 年份！",
                                  userid=event.event_data.get("user"))
                return

            content = " ".join(args[1:])
            if str(args[0]) == "电影":
                if not self._movies:
                    self._movies = str(content)
                else:
                    movies = [movie for movie in self._movies.split(",")]
                    if str(content) in movies:
                        logger.error(f"{content} 已在电影列表中")
                        if event.event_data.get("user"):
                            self.post_message(channel=event.event_data.get("channel"),
                                              title=f"{content} 已在电影列表中！",
                                              userid=event.event_data.get("user"))
                        return
                    else:
                        movies.append(str(content))
                    self._movies = ",".join(movies)
                # 保存配置
                self.__update_config()
                if event.event_data.get("user"):
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"{content} 已添加电影将映订阅！",
                                      userid=event.event_data.get("user"))

            elif str(args[0]) == "电视剧":
                if not self._tvs:
                    self._tvs = str(content)
                else:
                    tvs = [tv for tv in self._tvs.split(",")]
                    if str(content) in tvs:
                        logger.error(f"{content} 已在电视剧列表中")
                        if event.event_data.get("user"):
                            self.post_message(channel=event.event_data.get("channel"),
                                              title=f"{content} 已在电视剧列表中！",
                                              userid=event.event_data.get("user"))
                        return
                    else:
                        tvs.append(str(content))
                    self._tvs = ",".join(tvs)
                # 保存配置
                self.__update_config()
                if event.event_data.get("user"):
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"{content} 已添加电视剧将映订阅！",
                                      userid=event.event_data.get("user"))
            else:
                logger.error(f"参数错误：{event_data} 电影/电视剧 名称 年份")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"参数错误！格式：电影/电视剧 名称 年份！",
                                  userid=event.event_data.get("user"))
                return

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "clear": self._clear,
            "movies": self._movies,
            "tvs": self._tvs,
        })

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
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/mrs",
                "event": EventType.PluginAction,
                "desc": "影视将映订阅",
                "category": "",
                "data": {
                    "action": "media_release"
                }
            },
        ]

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
                                            'model': 'clear',
                                            'label': '清理订阅记录',
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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'movies',
                                            'label': '电影',
                                            'rows': 4,
                                            'placeholder': '电影名称(多个英文逗号拼接)'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'tvs',
                                            'label': '电视剧',
                                            'rows': 4,
                                            'placeholder': '电视剧名称(多个英文逗号拼接)'
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
            "onlyonce": False,
            "cron": "5 1 * * *",
            "clear": False,
            "tvs": "",
            "movies": "",
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
            tmdbid = history.get("tmdbid")
            doubanid = history.get("doubanid")
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
                                    'api': 'plugin/MediaRelease/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'key': f"mediarelease: {title} (DB:{tmdbid})",
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
