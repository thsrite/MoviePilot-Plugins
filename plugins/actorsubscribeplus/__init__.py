import time
from datetime import datetime, timedelta

import pytz

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class ActorSubscribePlus(_PluginBase):
    # 插件名称
    plugin_name = "演员作品订阅"
    # 插件描述
    plugin_desc = "获取TMDB演员作品，并自动添加到订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/actorsubscribeplus.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "actorsubscribeplus_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _actors = None
    subscribechain = None
    downloadchain = None
    _scheduler: Optional[BackgroundScheduler] = None
    _clear = False
    _clear_already_handle = False
    _mtype = False

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._actors = config.get("actors")
            self._clear = config.get("clear")
            self._clear_already_handle = config.get("clear_already_handle")
            self._mtype = config.get("mtype") or ['电影', '电视剧']

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

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"演员作品订阅服务启动，立即运行一次")
                    self._scheduler.add_job(self.__actor_subscribe, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="演员作品订阅")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__actor_subscribe,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="演员作品订阅")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __actor_subscribe(self):
        """
        明星热映订阅
        """
        if not self._actors:
            logger.warn("暂无订阅明星，停止运行")
            return

        history: List[dict] = self.get_data('history') or []
        already_handle: List[dict] = self.get_data('already_handle') or []

        # 检查订阅
        subscribe_actors = str(self._actors).split(",")

        # 订阅演员作品
        for actor in subscribe_actors:
            logger.info(f"开始订阅演员 {actor} 的作品")
            result = MediaChain().search_persons(name=actor)
            if not result:
                logger.warn(f"未找到TMDB演员 {actor}")
                continue

            person_id = None
            for person in result:
                if person.source == "themoviedb":
                    person_id = person.id
                    break

            if not person_id:
                logger.warn(f"未找到演员 {actor} 的Person ID")
                continue

            logger.info(f"正在获取演员 {actor} Person ID {person_id}")

            actor_medias = []
            for i in range(1, 10):
                medias = TmdbChain().person_credits(person_id=person_id, page=i)
                if medias:
                    actor_medias += medias
                else:
                    break

            if not actor_medias:
                logger.warn(f"未找到演员 {actor} 的作品")
                continue

            logger.info(f"获取到演员 {actor} 的作品 {len(actor_medias)} 部")

            for mediainfo in actor_medias:
                if mediainfo.type.value not in self._mtype:
                    continue
                if mediainfo.title_year in already_handle:
                    logger.info(f"{mediainfo.type.value} {mediainfo.title_year} 已被处理，跳过")
                    continue

                already_handle.append(mediainfo.title_year)
                logger.info(f"开始处理电影 {mediainfo.title_year}")

                # 元数据
                meta = MetaInfo(mediainfo.title)

                # 查询缺失的媒体信息
                exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
                if exist_flag:
                    logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                    continue

                # 判断用户是否已经添加订阅
                if self.subscribechain.exists(mediainfo=mediainfo):
                    logger.info(f'{mediainfo.title_year} 订阅已存在')
                    continue

                # 开始订阅
                logger.info(
                    f"开始订阅 {actor} {mediainfo.type.value} {mediainfo.title_year} TMDBID {mediainfo.tmdb_id}")
                # 添加订阅
                self.subscribechain.add(title=mediainfo.title,
                                        year=mediainfo.year,
                                        mtype=mediainfo.type,
                                        tmdbid=mediainfo.tmdb_id,
                                        doubanid=mediainfo.douban_id,
                                        exist_ok=True,
                                        username="演员作品订阅")
                # 存储历史记录
                history.append({
                    "title": mediainfo.title,
                    "type": mediainfo.type.value,
                    "year": mediainfo.year,
                    "poster": mediainfo.get_poster_image(),
                    "overview": mediainfo.overview,
                    "tmdbid": mediainfo.tmdb_id,
                    "doubanid": mediainfo.douban_id,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "unique": f"actorsubscribeplus: {mediainfo.title} (DB:{mediainfo.tmdb_id})"
                })
            logger.info(f"演员 {actor} 订阅完成")

        # 保存历史记录
        self.save_data('history', history)
        self.save_data('already_handle', already_handle)
        logger.info(f"演员订阅任务完成")

    def __get_douban_actors(self, mediainfo: MediaInfo, season: int = None) -> List[dict]:
        """
        获取豆瓣演员信息
        """
        sleep_time = 3 + int(time.time()) % 7
        logger.debug(f"随机休眠 {sleep_time}秒 ...")
        time.sleep(sleep_time)
        if mediainfo.douban_id:
            doubanitem = DoubanChain().douban_info(mediainfo.douban_id) or {}
        else:
            # 匹配豆瓣信息
            doubaninfo = DoubanChain().match_doubaninfo(name=mediainfo.title,
                                                        imdbid=mediainfo.imdb_id,
                                                        mtype=mediainfo.type,
                                                        year=mediainfo.year,
                                                        season=season)
            # 豆瓣演员
            if doubaninfo:
                mediainfo.douban_id = doubaninfo.get("id")
                doubanitem = DoubanChain().douban_info(doubaninfo.get("id")) or {}
            else:
                doubanitem = None

        if doubanitem:
            actors = (doubanitem.get("actors") or []) + (doubanitem.get("directors") or [])
            return [actor.get("name") for actor in actors]
        else:
            logger.debug(f"未找到豆瓣信息：{mediainfo.title_year}")
            return []

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "actors": self._actors,
            "clear": self._clear,
            "clear_already_handle": self._clear_already_handle,
            "mtype": self._mtype,
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'mtype',
                                            'label': '订阅类型',
                                            'items': [
                                                {'title': '电影', 'value': '电影'},
                                                {'title': '电视剧', 'value': '电视剧'},
                                            ]
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'actors',
                                            'label': '明星',
                                            'placeholder': '多个英文逗号分割'
                                        }
                                    }
                                ]
                            },
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "actors": "",
            "clear": False,
            "clear_already_handle": False,
            "mtype": ['电影', '电视剧'],
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
                                    'api': 'plugin/ActorSubscribePlus/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'key': f"actorsubscribeplus: {title} (DB:{tmdbid})",
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
