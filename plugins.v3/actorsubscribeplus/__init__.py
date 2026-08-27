from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.plugins import _PluginBase
from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MetaInfo, build_media_key, resolve_media_identity


class ActorSubscribePlus(_PluginBase):
    """按演员筛选影视作品并创建订阅。"""

    # 插件名称
    plugin_name = "演员作品订阅"
    # 插件描述
    plugin_desc = "获取TMDB演员作品，并自动添加到订阅。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/actorsubscribeplus.png"
    # 插件版本
    plugin_version = "2.0.0"
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

    _enabled: bool = False
    _onlyonce: bool = False
    _cron: str = ""
    _actors: Optional[str] = None
    _scheduler: Optional[BackgroundScheduler] = None
    _clear: bool = False
    _clear_already_handle: bool = False
    _mtype: List[str] = [MediaType.MOVIE.value, MediaType.TV.value]
    _year: int = 2000
    _last: int = 30
    _vate: float = 0
    mediachain: Optional[MediaChain] = None
    tmdbchain: Optional[TmdbChain] = None
    subscribechain: Optional[SubscribeChain] = None
    downloadchain: Optional[DownloadChain] = None

    def init_plugin(self, config: dict = None):
        """重建链实例、迁移历史并根据配置注册定时任务。"""
        self.stop_service()
        self.mediachain = MediaChain()
        self.tmdbchain = TmdbChain()
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.__migrate_history_identity()

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = config.get("cron") or ""
        self._actors = config.get("actors") or ""
        self._clear = bool(config.get("clear", False))
        self._clear_already_handle = bool(config.get("clear_already_handle", False))
        self._mtype = config.get("mtype") or [MediaType.MOVIE.value, MediaType.TV.value]
        self._year = config.get("year", 2000)
        self._last = config.get("last", 30)
        self._vate = config.get("vate", 0)

        if self._clear:
            self.del_data(key="history")
            self._clear = False
            self.__update_config()
            logger.info("订阅历史清理完成")

        if self._clear_already_handle:
            self.del_data(key="already_handle")
            self._clear_already_handle = False
            self.__update_config()
            logger.info("已处理历史清理完成")

        if not (self._enabled or self._onlyonce):
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if self._onlyonce:
            logger.info("演员作品订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                self.__actor_subscribe,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="演员作品订阅",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self.__actor_subscribe,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="演员作品订阅",
                )
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")
                self.systemmessage.put(f"执行周期配置错误：{err}")

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def __history_unique(title: str, media_source: MediaSource, media_id: str) -> str:
        """构造同时包含来源和来源原生 ID 的稳定历史键。"""
        return f"actorsubscribeplus: {title} ({build_media_key(media_source, media_id)})"

    @staticmethod
    def __detail_link(
        media_source: Optional[MediaSource],
        media_id: Optional[str],
        media_type: Optional[str],
    ) -> str:
        """构造历史详情页的来源链接。"""
        media_source, media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if not media_source or not media_id:
            return ""
        if media_source == MediaSource.TMDB:
            path = "movie" if media_type in {MediaType.MOVIE.value, "movie"} else "tv"
            return f"https://www.themoviedb.org/{path}/{media_id}"
        if media_source == MediaSource.Douban:
            return f"https://movie.douban.com/subject/{media_id}"
        if media_source == MediaSource.Bangumi:
            return f"https://bgm.tv/subject/{media_id}"
        if media_source == MediaSource.AniList:
            return f"https://anilist.co/anime/{media_id}"
        if media_source == MediaSource.IMDb:
            return f"https://www.imdb.com/title/{media_id}"
        if media_source == MediaSource.TVDB:
            return f"https://thetvdb.com/search?query={media_id}"
        return ""

    def __migrate_history_identity(self) -> None:
        """将存量历史中的来源专有 ID 幂等迁移为统一媒体身份。"""
        history = self.get_data("history")
        if not isinstance(history, list):
            return

        changed = False
        for item in history:
            if not isinstance(item, dict):
                continue

            media_source, media_id = resolve_media_identity(
                media_source=item.get("media_source"),
                media_id=item.get("media_id"),
            )
            if not media_source:
                for legacy_source, legacy_key in (
                    (MediaSource.TMDB, "tmdbid"),
                    (MediaSource.Douban, "doubanid"),
                ):
                    media_source, media_id = resolve_media_identity(
                        media_source=legacy_source,
                        media_id=item.get(legacy_key),
                    )
                    if media_source:
                        break
            if not media_source:
                continue

            migrated = {
                key: value
                for key, value in item.items()
                if key not in {"tmdbid", "doubanid"}
            }
            migrated["media_source"] = media_source.value
            migrated["media_id"] = media_id
            migrated["detail_link"] = migrated.get("detail_link") or self.__detail_link(
                media_source=media_source,
                media_id=media_id,
                media_type=migrated.get("type"),
            )
            migrated["unique"] = self.__history_unique(
                title=migrated.get("title") or "",
                media_source=media_source,
                media_id=media_id,
            )
            if migrated != item:
                item.clear()
                item.update(migrated)
                changed = True

        if changed:
            self.save_data("history", history)

    @staticmethod
    def __media_type(mediainfo: Any) -> Optional[MediaType]:
        """把链返回的媒体类型规范化为宿主枚举。"""
        if isinstance(mediainfo.type, MediaType):
            return mediainfo.type
        try:
            return MediaType(mediainfo.type)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __release_date(mediainfo: Any) -> Optional[datetime]:
        """解析电影上映日期或电视剧首播日期，非法日期按缺失处理。"""
        release_date = mediainfo.first_air_date or mediainfo.release_date
        if not release_date:
            return None
        try:
            return datetime.strptime(str(release_date), "%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    def __save_progress(self, history: List[dict], already_handle: List[str]) -> None:
        """持久化已完成候选，避免后续候选失败时丢失本轮进度。"""
        self.save_data("history", history)
        self.save_data("already_handle", already_handle)

    def __actor_subscribe(self):
        """查询配置演员的 TMDB 作品并创建符合筛选条件的订阅。"""
        if not self._actors:
            logger.warning("暂无订阅明星，停止运行")
            return

        history: List[dict] = self.get_data("history") or []
        already_handle: List[str] = self.get_data("already_handle") or []
        subscribe_actors = [actor.strip() for actor in str(self._actors).split(",") if actor.strip()]

        try:
            minimum_year = int(self._year)
        except (TypeError, ValueError):
            minimum_year = 2000
        try:
            recent_days = int(self._last)
        except (TypeError, ValueError):
            recent_days = 30
        try:
            minimum_vote = float(self._vate)
        except (TypeError, ValueError):
            minimum_vote = 0

        for actor in subscribe_actors:
            logger.info(f"开始订阅演员 {actor} 的作品")
            persons = self.mediachain.search_persons(
                name=actor,
                media_source=MediaSource.TMDB,
            )
            if not persons:
                logger.warning(f"未找到TMDB演员 {actor}")
                continue

            person_id = next(
                (
                    person.id
                    for person in persons
                    if person.source == MediaSource.TMDB.value and person.id
                ),
                None,
            )
            if not person_id:
                logger.warning(f"未找到演员 {actor} 的Person ID")
                continue

            logger.info(f"正在获取演员 {actor} Person ID {person_id}")
            actor_medias = []
            for page in range(1, 10):
                medias = self.tmdbchain.person_credits(person_id=person_id, page=page)
                if not medias:
                    break
                actor_medias.extend(medias)

            if not actor_medias:
                logger.warning(f"未找到演员 {actor} 的作品")
                continue

            logger.info(f"获取到演员 {actor} 的作品 {len(actor_medias)} 部")
            for mediainfo in actor_medias:
                media_source, media_id = resolve_media_identity(media=mediainfo)
                if not media_source or not media_id:
                    logger.warning("演员作品缺少有效媒体身份，跳过")
                    continue

                media_type = self.__media_type(mediainfo)
                if not media_type or media_type.value not in self._mtype:
                    logger.warning(f"{mediainfo.title_year} 类型不在订阅列表中，跳过")
                    continue

                if not mediainfo.year:
                    logger.warning(f"{mediainfo.title} 缺少年份，跳过")
                    continue
                try:
                    media_year = int(mediainfo.year)
                except (TypeError, ValueError):
                    logger.warning(f"{mediainfo.title} 年份无法识别，跳过")
                    continue
                if media_year < minimum_year:
                    logger.warning(f"{mediainfo.title_year} 年份不在订阅列表中，跳过")
                    continue

                release_date = self.__release_date(mediainfo)
                if not release_date:
                    logger.warning(f"{mediainfo.title_year} 缺少有效上映时间，跳过")
                    continue
                now = datetime.now()
                if release_date > now and (release_date - now).days > recent_days:
                    logger.warning(f"{mediainfo.title_year} 最近上映时间不在时间范围内，跳过")
                    continue

                try:
                    vote_average = float(mediainfo.vote_average or 0)
                except (TypeError, ValueError):
                    logger.warning(f"{mediainfo.title_year} 评分无法识别，跳过")
                    continue
                if vote_average < minimum_vote:
                    logger.warning(f"{mediainfo.title_year} 评分不足，跳过")
                    continue

                media_key = build_media_key(media_source, media_id)
                if not media_key:
                    logger.warning(f"{mediainfo.title_year} 媒体身份无效，跳过")
                    continue
                title_year = mediainfo.title_year
                if media_key in already_handle or title_year in already_handle:
                    logger.warning(f"{title_year} 已被处理，跳过")
                    continue

                logger.info(f"开始处理 {media_type.value} {title_year}")
                meta = MetaInfo(mediainfo.title)
                try:
                    exist_flag, _ = self.downloadchain.get_no_exists_info(
                        meta=meta,
                        mediainfo=mediainfo,
                    )
                except Exception as err:
                    logger.error(f"{title_year} 查询媒体库失败：{err}")
                    continue
                if exist_flag:
                    logger.warning(f"{title_year} 媒体库中已存在")
                    already_handle.append(media_key)
                    self.__save_progress(history, already_handle)
                    continue

                try:
                    subscribed = self.subscribechain.exists(mediainfo=mediainfo)
                except Exception as err:
                    logger.error(f"{title_year} 查询订阅失败：{err}")
                    continue
                if subscribed:
                    logger.warning(f"{title_year} 订阅已存在")
                    already_handle.append(media_key)
                    self.__save_progress(history, already_handle)
                    continue

                logger.info(f"开始订阅 {actor} {media_type.value} {title_year} {media_key}")
                try:
                    subscribe_id, error = self.subscribechain.add(
                        title=mediainfo.title,
                        year=str(mediainfo.year),
                        mtype=media_type,
                        media_source=media_source,
                        media_id=media_id,
                        exist_ok=True,
                        username="演员作品订阅",
                    )
                except Exception as err:
                    logger.error(f"{title_year} 添加订阅异常：{err}")
                    continue
                if not subscribe_id:
                    logger.error(f"{title_year} 添加订阅失败：{error or '未知错误'}")
                    continue

                already_handle.append(media_key)
                detail_link = mediainfo.detail_link or self.__detail_link(
                    media_source=media_source,
                    media_id=media_id,
                    media_type=media_type.value,
                )
                history.append(
                    {
                        "title": mediainfo.title,
                        "type": media_type.value,
                        "year": mediainfo.year,
                        "poster": mediainfo.get_poster_image(),
                        "overview": mediainfo.overview,
                        "media_source": media_source.value,
                        "media_id": media_id,
                        "detail_link": detail_link,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "unique": self.__history_unique(
                            title=mediainfo.title,
                            media_source=media_source,
                            media_id=media_id,
                        ),
                    }
                )
                self.__save_progress(history, already_handle)
            logger.info(f"演员 {actor} 订阅完成")

        self.__save_progress(history, already_handle)
        logger.info("演员订阅任务完成")

    def __update_config(self):
        """保存当前配置并清除一次性控制开关。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "actors": self._actors,
                "clear": self._clear,
                "clear_already_handle": self._clear_already_handle,
                "mtype": self._mtype,
                "year": self._year,
                "last": self._last,
                "vate": self._vate,
            }
        )

    def delete_history(self, key: str) -> schemas.Response[None]:
        """删除详情页中指定的订阅历史记录。"""
        history = self.get_data("history")
        if not history:
            return schemas.Response(success=False, message="未找到历史记录")
        history = [item for item in history if item.get("unique") != key]
        self.save_data("history", history)
        return schemas.Response(success=True, message="删除成功")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "删除订阅历史记录",
                "response_model": schemas.Response[None],
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面描述和默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "clear", "label": "清理订阅记录"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_already_handle",
                                            "label": "清理已处理记录",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，留空自动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "mtype",
                                            "label": "订阅类型",
                                            "items": [
                                                {"title": "电影", "value": "电影"},
                                                {"title": "电视剧", "value": "电视剧"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "year",
                                            "label": "年份",
                                            "placeholder": "大于该年份才会被订阅",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "last",
                                            "label": "最近多久上映",
                                            "placeholder": "当前日期几天内上映的才会被订阅",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "vate",
                                            "label": "评分",
                                            "placeholder": "大于该评分才会被订阅",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "actors",
                                            "label": "明星",
                                            "placeholder": "多个英文逗号分割",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "actors": "",
            "clear": False,
            "year": 2000,
            "last": 30,
            "vate": 0,
            "clear_already_handle": False,
            "mtype": [MediaType.MOVIE.value, MediaType.TV.value],
        }

    def get_page(self) -> List[dict]:
        """返回订阅历史详情页面描述。"""
        history = self.get_data("history")
        if not history:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {"class": "text-center"},
                }
            ]

        contents = []
        for item in sorted(history, key=lambda value: value.get("time") or "", reverse=True):
            title = item.get("title")
            media_type = item.get("type")
            media_source, media_id = resolve_media_identity(
                media_source=item.get("media_source"),
                media_id=item.get("media_id"),
            )
            unique = item.get("unique") or (
                self.__history_unique(title or "", media_source, media_id)
                if media_source and media_id
                else ""
            )
            detail_link = item.get("detail_link") or self.__detail_link(
                media_source=media_source,
                media_id=media_id,
                media_type=media_type,
            )
            contents.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {"innerClass": "absolute top-0 right-0"},
                            "events": {
                                "click": {
                                    "api": "plugin/ActorSubscribePlus/delete_history",
                                    "method": "get",
                                    "params": {"key": unique},
                                }
                            },
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex justify-space-start flex-nowrap flex-row",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VImg",
                                            "props": {
                                                "src": item.get("poster"),
                                                "height": 120,
                                                "width": 80,
                                                "aspect-ratio": "2/3",
                                                "class": "object-cover shadow ring-gray-500",
                                                "cover": True,
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VCardSubtitle",
                                            "props": {
                                                "class": "pa-2 font-bold break-words whitespace-break-spaces"
                                            },
                                            "content": [
                                                {
                                                    "component": "a",
                                                    "props": {
                                                        "href": detail_link,
                                                        "target": "_blank",
                                                    },
                                                    "text": title,
                                                }
                                            ],
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"类型：{media_type}",
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"时间：{item.get('time')}",
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            )

        return [
            {
                "component": "div",
                "props": {"class": "grid gap-3 grid-info-card"},
                "content": contents,
            }
        ]

    def stop_service(self):
        """停止插件的定时任务并释放调度器。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as err:
            logger.error(f"退出插件失败：{err}")
