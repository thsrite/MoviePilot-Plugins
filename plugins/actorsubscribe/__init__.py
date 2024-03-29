from datetime import datetime, timedelta

import pytz

from app.chain.douban import DoubanChain
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


class ActorSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣明星热映订阅"
    # 插件描述
    plugin_desc = "自动订阅豆瓣明星最新电影。"
    # 插件图标
    plugin_icon = "Mdcng_A.png"
    # 插件版本
    plugin_version = "1.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "actorsubscribe_"
    # 加载顺序
    plugin_order = 25
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
    _quality = None
    _resolution = None
    _effect = None
    _clear = False
    # 质量选择框数据
    _qualityOptions = {
        '全部': '',
        '蓝光原盘': 'Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD',
        'Remux': 'Remux',
        'BluRay': 'Blu-?Ray',
        'UHD': 'UHD|UltraHD',
        'WEB-DL': 'WEB-?DL|WEB-?RIP',
        'HDTV': 'HDTV',
        'H265': '[Hx].?265|HEVC',
        'H264': '[Hx].?264|AVC'
    }

    # 分辨率选择框数据
    _resolutionOptions = {
        '全部': '',
        '4k': '4K|2160p|x2160',
        '1080p': '1080[pi]|x1080',
        '720p': '720[pi]|x720'
    }

    # 特效选择框数据
    _effectOptions = {
        '全部': '',
        '杜比视界': 'Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+',
        '杜比全景声': 'Dolby[\\s.]*\\+?Atmos|Atmos',
        'HDR': '[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+',
        'SDR': '[\\s.]+SDR[\\s.]+',
    }

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
            self._quality = config.get("quality")
            self._resolution = config.get("resolution")
            self._effect = config.get("effect")
            self._clear = config.get("clear")

            # 清理插件历史
            if self._clear:
                self.del_data(key="history")
                self.del_data(key="already_handle")

                self._clear = False
                self.__update_config()
                logger.info("历史清理完成")

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"明星热映订阅服务启动，立即运行一次")
                    self._scheduler.add_job(self.__actor_subscribe, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="明星热映订阅")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__actor_subscribe,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="明星热映订阅")
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

        movies = DoubanChain().movie_showing(page=1, count=100)
        if not movies:
            return []
        medias = [MediaInfo(douban_info=movie) for movie in movies]
        logger.info(f"获取到豆瓣正在热映 {len(medias)} 部")

        # 检查订阅
        actors = str(self._actors).split(",")
        for mediainfo in medias:
            if mediainfo.title_year in already_handle:
                logger.info(f"电影 {mediainfo.title_year} 已被处理，跳过")
                continue

            already_handle.append(mediainfo.title_year)
            logger.info(f"开始处理电影 {mediainfo.title_year}")

            # 元数据
            meta = MetaInfo(mediainfo.title)

            # 豆瓣演员中文名
            mediainfo_actiors = mediainfo.actors + mediainfo.directors

            oldmediainfo = mediainfo
            # 主要获取tmdbid
            mediainfo = self.chain.recognize_media(meta=meta, doubanid=mediainfo.douban_id)
            if not mediainfo:
                logger.warn(f'未识别到媒体信息，标题：{oldmediainfo.title}，豆瓣ID：{oldmediainfo.douban_id}')
                continue

            # 查询缺失的媒体信息
            exist_flag, _ = self.downloadchain.get_no_exists_info(meta=meta, mediainfo=mediainfo)
            if exist_flag:
                logger.info(f'{mediainfo.title_year} 媒体库中已存在')
                continue

            # 判断用户是否已经添加订阅
            if self.subscribechain.exists(mediainfo=mediainfo):
                logger.info(f'{mediainfo.title_year} 订阅已存在')
                continue

            if mediainfo_actiors:
                for actor in mediainfo_actiors:
                    # logger.info(f'正在处理 {mediainfo.title_year} 演员 {actor}')
                    if actor and actor in actors:
                        # 开始订阅
                        logger.info(f"电影 {mediainfo.title_year} {mediainfo.tmdb_id} 命中订阅演员 {actor}，开始订阅")

                        # 添加订阅
                        self.subscribechain.add(title=mediainfo.title,
                                                year=mediainfo.year,
                                                mtype=mediainfo.type,
                                                tmdbid=mediainfo.tmdb_id,
                                                doubanid=mediainfo.douban_id,
                                                exist_ok=True,
                                                quality=self._quality,
                                                resolution=self._resolution,
                                                effect=self._effect,
                                                username=settings.SUPERUSER)
                        # 存储历史记录
                        history.append({
                            "title": mediainfo.title,
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "doubanid": mediainfo.douban_id,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })

        # 保存历史记录
        self.save_data('history', history)
        self.save_data('already_handle', already_handle)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "actors": self._actors,
            "quality": self._quality,
            "resolution": self._resolution,
            "effect": self._effect,
            "clear": self._clear,
        })

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
        qualityOptions = [{"title": i, "value": self._qualityOptions.get(i)} for i in self._qualityOptions.keys()]
        resolutionOptions = [{"title": i, "value": self._resolutionOptions.get(i)} for i in
                             self._resolutionOptions.keys()]
        effectOptions = [{"title": i, "value": self._effectOptions.get(i)} for i in self._effectOptions.keys()]

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
                                            'label': '清理历史记录',
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
                                    'md': 8
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
                                            'model': 'quality',
                                            'label': '质量',
                                            'items': qualityOptions
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
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'resolution',
                                            'label': '分辨率',
                                            'items': resolutionOptions
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
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'effect',
                                            'label': '特效',
                                            'items': effectOptions
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "actors": "",
            "quality": "",
            "resolution": "",
            "effect": "",
            "clear": False
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
