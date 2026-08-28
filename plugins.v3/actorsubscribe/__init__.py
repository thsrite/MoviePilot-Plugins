import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.douban import DoubanChain
from app.chain.download import DownloadChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.plugins import _PluginBase
from app.schemas.types import MediaSource, MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaInfo, build_media_key, resolve_media_identity


class ActorSubscribe(_PluginBase):
    """从热门榜单中筛选指定演员并创建订阅。"""

    plugin_name = "演员订阅"
    plugin_desc = "自动订阅指定演员热映电影、电视剧。"
    plugin_icon = "Mdcng_A.png"
    plugin_version = "3.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "actorsubscribe_"
    plugin_order = 25
    auth_level = 2

    # 质量选择框数据
    _quality_options = {
        "全部": "",
        "蓝光原盘": "Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD",
        "Remux": "Remux",
        "BluRay": "Blu-?Ray",
        "UHD": "UHD|UltraHD",
        "WEB-DL": "WEB-?DL|WEB-?RIP",
        "HDTV": "HDTV",
        "H265": "[Hx].?265|HEVC",
        "H264": "[Hx].?264|AVC",
    }

    # 分辨率选择框数据
    _resolution_options = {
        "全部": "",
        "4k": "4K|2160p|x2160",
        "1080p": "1080[pi]|x1080",
        "720p": "720[pi]|x720",
    }

    # 特效选择框数据
    _effect_options = {
        "全部": "",
        "杜比视界": "Dolby[\\s.]+Vision|DOVI|[\\s.]+DV[\\s.]+",
        "杜比全景声": "Dolby[\\s.]*\\+?Atmos|Atmos",
        "HDR": "[\\s.]+HDR[\\s.]+|HDR10|HDR10\\+",
        "SDR": "[\\s.]+SDR[\\s.]+",
    }

    def __init__(self):
        """初始化热重载隔离的配置、调度器和业务链状态。"""
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._cron = ""
        self._actors = ""
        self._quality = ""
        self._resolution = ""
        self._effect = ""
        self._username = "演员订阅"
        self._clear = False
        self._clear_already_handle = False
        self._source: List[str] = ["douban_showing"]
        self._scheduler: Optional[BackgroundScheduler] = None
        self.doubanchain: Optional[DoubanChain] = None
        self.tmdbchain: Optional[TmdbChain] = None
        self.subscribechain: Optional[SubscribeChain] = None
        self.downloadchain: Optional[DownloadChain] = None

    def init_plugin(self, config: dict = None):
        """重建链实例、迁移历史并按配置注册定时任务。"""
        self.stop_service()
        self.doubanchain = DoubanChain()
        self.tmdbchain = TmdbChain()
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.__migrate_history_identity()

        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = config.get("cron") or ""
        self._actors = config.get("actors") or ""
        self._quality = config.get("quality") or ""
        self._resolution = config.get("resolution") or ""
        self._effect = config.get("effect") or ""
        self._clear = bool(config.get("clear", False))
        self._clear_already_handle = bool(config.get("clear_already_handle", False))
        source = config.get("source")
        if isinstance(source, (list, tuple)):
            self._source = [str(item) for item in source if str(item).strip()]
        elif source:
            self._source = [str(source)]
        else:
            self._source = ["douban_showing"]
        self._username = config.get("username") or "演员订阅"

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
            logger.info("明星热映订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                self.__actor_subscribe,
                "date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="明星热映订阅",
            )
            self._onlyonce = False
            self.__update_config()

        if self._cron:
            try:
                self._scheduler.add_job(
                    func=self.__actor_subscribe,
                    trigger=CronTrigger.from_crontab(self._cron),
                    name="明星热映订阅",
                )
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")
                self.systemmessage.put(f"执行周期配置错误：{err}")

        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    @staticmethod
    def __history_unique(title: str, media_source: MediaSource, media_id: str) -> str:
        """为订阅历史构造包含来源和原生 ID 的稳定键。"""
        return f"actorsubscribe: {title} ({build_media_key(media_source, media_id)})"

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
    def __media_type(mediainfo: MediaInfo) -> Optional[MediaType]:
        """将链返回的媒体类型规范化为宿主枚举。"""
        if isinstance(mediainfo.type, MediaType):
            return mediainfo.type
        try:
            return MediaType(mediainfo.type)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def __people_names(people: Any) -> List[str]:
        """提取标准演职员字典或兼容字符串中的姓名。"""
        names = []
        for person in people or []:
            if isinstance(person, str):
                name = person.strip()
            elif isinstance(person, dict):
                name = str(person.get("name") or person.get("original_name") or "").strip()
            else:
                name = str(getattr(person, "name", "") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def __get_douban_actors(self, mediainfo: MediaInfo, season: int = None) -> List[str]:
        """当榜单缺少演职员时，从豆瓣详情补充中文姓名。"""
        sleep_time = 3 + int(time.time()) % 7
        logger.debug(f"随机休眠 {sleep_time}秒 ...")
        time.sleep(sleep_time)

        if self.doubanchain is None:
            self.doubanchain = DoubanChain()
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if media_source == MediaSource.Douban and media_id:
            doubanitem = self.doubanchain.douban_info(media_id, mtype=mediainfo.type) or {}
        else:
            doubaninfo = self.doubanchain.match_doubaninfo(
                name=mediainfo.title,
                imdbid=mediainfo.imdb_id,
                mtype=mediainfo.type,
                year=mediainfo.year,
                season=season,
            )
            doubanitem = (
                self.doubanchain.douban_info(doubaninfo.get("id"), mtype=mediainfo.type) or {}
                if doubaninfo and doubaninfo.get("id")
                else {}
            )

        if not doubanitem:
            logger.debug(f"未找到豆瓣信息：{mediainfo.title_year}")
            return []
        return self.__people_names(
            (doubanitem.get("actors") or []) + (doubanitem.get("directors") or [])
        )

    @staticmethod
    def __source_result(result: Any, label: str) -> List[MediaInfo]:
        """将来源链返回值规范化为媒体列表并记录数量。"""
        medias = result if isinstance(result, list) else []
        if medias:
            logger.info(f"获取到{label} {len(medias)} 部")
        return medias

    def __douban_movie_showing(self) -> List[MediaInfo]:
        return self.__source_result(self.doubanchain.movie_showing(page=1, count=30), "豆瓣正在热映")

    def __douban_movies(self) -> List[MediaInfo]:
        return self.__source_result(
            self.doubanchain.douban_discover(
                mtype=MediaType.MOVIE, sort="R", tags="", page=1, count=30
            ),
            "豆瓣电影",
        )

    def __douban_tvs(self) -> List[MediaInfo]:
        return self.__source_result(
            self.doubanchain.douban_discover(
                mtype=MediaType.TV, sort="R", tags="", page=1, count=30
            ),
            "豆瓣剧集",
        )

    def __douban_movie_top250(self) -> List[MediaInfo]:
        return self.__source_result(self.doubanchain.movie_top250(page=1, count=30), "豆瓣电影TOP250")

    def __douban_tv_weekly_chinese(self) -> List[MediaInfo]:
        return self.__source_result(
            self.doubanchain.tv_weekly_chinese(page=1, count=30), "豆瓣国产剧集周榜"
        )

    def __douban_tv_weekly_global(self) -> List[MediaInfo]:
        return self.__source_result(
            self.doubanchain.tv_weekly_global(page=1, count=30), "豆瓣全球剧集周榜"
        )

    def __douban_tv_animation(self) -> List[MediaInfo]:
        return self.__source_result(self.doubanchain.tv_animation(page=1, count=30), "豆瓣动画剧集")

    def __douban_movie_hot(self) -> List[MediaInfo]:
        return self.__source_result(self.doubanchain.movie_hot(page=1, count=30), "豆瓣热门电影")

    def __douban_tv_hot(self) -> List[MediaInfo]:
        return self.__source_result(self.doubanchain.tv_hot(page=1, count=30), "豆瓣热门电视剧")

    def __tmdb_movies(self) -> List[MediaInfo]:
        return self.__source_result(
            self.tmdbchain.tmdb_discover(
                mtype=MediaType.MOVIE,
                sort_by="popularity.desc",
                with_genres="",
                with_original_language="",
                with_keywords="",
                with_watch_providers="",
                vote_average=0,
                vote_count=0,
                release_date="",
                page=1,
            ),
            "TMDB电影",
        )

    def __tmdb_tvs(self) -> List[MediaInfo]:
        return self.__source_result(
            self.tmdbchain.tmdb_discover(
                mtype=MediaType.TV,
                sort_by="popularity.desc",
                with_genres="",
                with_original_language="",
                with_keywords="",
                with_watch_providers="",
                vote_average=0,
                vote_count=0,
                release_date="",
                page=1,
            ),
            "TMDB剧集",
        )

    def __tmdb_trending(self) -> List[MediaInfo]:
        return self.__source_result(self.tmdbchain.tmdb_trending(page=1), "TMDB流行趋势")

    def __source_medias(self, source: str) -> List[MediaInfo]:
        """按配置调用来源查询，单一来源失败不影响本轮其它来源。"""
        handlers = {
            "douban_showing": self.__douban_movie_showing,
            "douban_movies": self.__douban_movies,
            "douban_tvs": self.__douban_tvs,
            "douban_movie_top250": self.__douban_movie_top250,
            "douban_tv_weekly_chinese": self.__douban_tv_weekly_chinese,
            "douban_tv_weekly_global": self.__douban_tv_weekly_global,
            "douban_tv_animation": self.__douban_tv_animation,
            "douban_movie_hot": self.__douban_movie_hot,
            "douban_tv_hot": self.__douban_tv_hot,
            "tmdb_movies": self.__tmdb_movies,
            "tmdb_tvs": self.__tmdb_tvs,
            "tmdb_trending": self.__tmdb_trending,
        }
        handler = handlers.get(source)
        if not handler:
            logger.warning(f"未知的订阅源：{source}")
            return []
        try:
            return handler()
        except Exception as err:
            logger.error(f"订阅源 {source} 查询失败：{err}")
            return []

    def __save_progress(self, history: List[dict], already_handle: List[str]) -> None:
        """持久化本轮进度，避免后续候选失败时丢失成功结果。"""
        self.save_data("history", history)
        self.save_data("already_handle", already_handle)

    def __actor_subscribe(self):
        """查询配置来源并为命中演员的媒体创建订阅。"""
        if not self._actors:
            logger.warning("暂无订阅明星，停止运行")
            return

        history = self.get_data("history") or []
        already_handle = self.get_data("already_handle") or []
        if not isinstance(history, list):
            history = []
        if not isinstance(already_handle, list):
            already_handle = []
        subscribe_actors = {
            actor.strip() for actor in str(self._actors).split(",") if actor.strip()
        }
        seen_keys = set()

        try:
            for configured_source in self._source or []:
                source = str(configured_source).strip()
                if not source:
                    continue
                for mediainfo in self.__source_medias(source):
                    media_source, media_id = resolve_media_identity(media=mediainfo)
                    media_key = build_media_key(media_source, media_id)
                    if not media_key:
                        logger.warning("媒体候选缺少有效 media_source/media_id，跳过")
                        continue
                    if media_key in seen_keys:
                        continue
                    seen_keys.add(media_key)

                    media_type = self.__media_type(mediainfo)
                    if media_type not in {MediaType.MOVIE, MediaType.TV}:
                        logger.warning(f"{mediainfo.title or media_key} 媒体类型无效，跳过")
                        continue
                    if not mediainfo.title:
                        logger.warning(f"{media_key} 缺少媒体标题，跳过")
                        continue
                    title_year = mediainfo.title_year
                    if media_key in already_handle:
                        logger.warning(f"{media_type.value} {title_year} 已被处理，跳过")
                        continue
                    if title_year in already_handle:
                        normalized_handle = []
                        for handled_key in already_handle:
                            handled_key = media_key if handled_key == title_year else handled_key
                            if handled_key not in normalized_handle:
                                normalized_handle.append(handled_key)
                        already_handle[:] = normalized_handle
                        self.__save_progress(history, already_handle)
                        logger.warning(f"{media_type.value} {title_year} 已被处理，跳过")
                        continue

                    try:
                        media_actors = self.__people_names(mediainfo.actors)
                        media_actors.extend(
                            actor for actor in self.__people_names(mediainfo.directors)
                            if actor not in media_actors
                        )
                        if not media_actors:
                            media_actors = self.__get_douban_actors(mediainfo)
                    except Exception as err:
                        logger.error(f"{title_year} 获取演员信息失败：{err}")
                        continue

                    if not media_actors:
                        logger.warning(f"未识别到演员信息：{title_year}")
                        continue
                    logger.info(f"获取到 {mediainfo.title} 演员：{media_actors}")
                    matched_actor = next(
                        (actor for actor in media_actors if actor in subscribe_actors),
                        None,
                    )
                    if not matched_actor:
                        already_handle.append(media_key)
                        self.__save_progress(history, already_handle)
                        logger.info(f"{media_type.value} {title_year} 未命中订阅演员，跳过")
                        continue

                    meta = MetaInfo(mediainfo.title)
                    meta.type = media_type
                    try:
                        exist_flag, _ = self.downloadchain.get_no_exists_info(
                            meta=meta,
                            mediainfo=mediainfo,
                        )
                    except Exception as err:
                        logger.error(f"{title_year} 查询媒体库失败：{err}")
                        continue
                    if exist_flag:
                        already_handle.append(media_key)
                        self.__save_progress(history, already_handle)
                        logger.warning(f"{title_year} 媒体库中已存在")
                        continue

                    try:
                        subscribed = self.subscribechain.exists(mediainfo=mediainfo)
                    except Exception as err:
                        logger.error(f"{title_year} 查询订阅失败：{err}")
                        continue
                    if subscribed:
                        already_handle.append(media_key)
                        self.__save_progress(history, already_handle)
                        logger.warning(f"{title_year} 订阅已存在")
                        continue

                    logger.info(
                        f"{media_type.value} {title_year} {media_key} 命中订阅演员 {matched_actor}，"
                        f"开始订阅。订阅规则：{self._quality} {self._resolution} "
                        f"{self._effect} {self._username}"
                    )
                    try:
                        subscribe_id, error = self.subscribechain.add(
                            title=mediainfo.title,
                            year=str(mediainfo.year or ""),
                            mtype=media_type,
                            media_source=media_source,
                            media_id=media_id,
                            exist_ok=True,
                            quality=self._quality,
                            resolution=self._resolution,
                            effect=self._effect,
                            username=self._username,
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
        finally:
            self.__save_progress(history, already_handle)
        logger.info("演员订阅任务完成")

    def __update_config(self) -> None:
        """保存当前配置并清除一次性控制开关。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "actors": self._actors,
                "quality": self._quality,
                "resolution": self._resolution,
                "effect": self._effect,
                "clear": self._clear,
                "clear_already_handle": self._clear_already_handle,
                "source": self._source,
                "username": self._username,
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
        quality_options = [
            {"title": title, "value": value}
            for title, value in self._quality_options.items()
        ]
        resolution_options = [
            {"title": title, "value": value}
            for title, value in self._resolution_options.items()
        ]
        effect_options = [
            {"title": title, "value": value}
            for title, value in self._effect_options.items()
        ]
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
                                        "component": "VCronField",
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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "chips": True,
                                            "model": "quality",
                                            "label": "质量",
                                            "items": quality_options,
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
                                            "multiple": False,
                                            "chips": True,
                                            "model": "resolution",
                                            "label": "分辨率",
                                            "items": resolution_options,
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
                                            "multiple": False,
                                            "chips": True,
                                            "model": "effect",
                                            "label": "特效",
                                            "items": effect_options,
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
                                            "model": "username",
                                            "label": "订阅用户",
                                            "placeholder": "默认为`演员订阅`",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "source",
                                            "label": "订阅来源",
                                            "items": [
                                                {"title": "豆瓣正在热映", "value": "douban_showing"},
                                                {"title": "豆瓣电影", "value": "douban_movies"},
                                                {"title": "豆瓣剧集", "value": "douban_tvs"},
                                                {"title": "豆瓣电影TOP250", "value": "douban_movie_top250"},
                                                {"title": "豆瓣国产剧集周榜", "value": "douban_tv_weekly_chinese"},
                                                {"title": "豆瓣全球剧集周榜", "value": "douban_tv_weekly_global"},
                                                {"title": "豆瓣动画剧集", "value": "douban_tv_animation"},
                                                {"title": "豆瓣热门电影", "value": "douban_movie_hot"},
                                                {"title": "豆瓣热门电视剧", "value": "douban_tv_hot"},
                                                {"title": "TMDB电影", "value": "tmdb_movies"},
                                                {"title": "TMDB剧集", "value": "tmdb_tvs"},
                                                {"title": "TMDB流行趋势", "value": "tmdb_trending"},
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "actors": "",
            "quality": "",
            "resolution": "",
            "effect": "",
            "username": "演员订阅",
            "clear": False,
            "clear_already_handle": False,
            "source": ["douban_showing"],
        }

    def get_page(self) -> List[dict]:
        """构造插件详情页面，展示已创建订阅及其来源详情。"""
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
                                    "api": "plugin/ActorSubscribe/delete_history",
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
