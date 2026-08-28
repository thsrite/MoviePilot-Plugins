from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.media import MediaChain
from app.chain.tmdb import TmdbChain
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import MediaSource, MediaType, MessageType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import resolve_media_identity


class SubscribeReminder(_PluginBase):
    """按媒体来源和日期推送订阅更新提醒。"""

    plugin_name = "订阅提醒"
    plugin_desc = "推送当天订阅更新内容。"
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/subscribe_reminder.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "subscribereminder_"
    plugin_order = 33
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._onlyonce = False
        self._time: Any = 9
        self._subtype: Any = ["movie", "tv"]
        self._msgtype: Any = "Plugin"
        self._scheduler: Optional[BackgroundScheduler] = None
        self._subscribe_oper: Optional[SubscribeOper] = None
        self._media_chain: Optional[MediaChain] = None
        self._tmdb_chain: Optional[TmdbChain] = None

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置，停止旧的一次性任务并按需安排新任务。"""
        self.stop_service()
        config = dict(config or {})
        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._time = config.get("time", 9)
        self._subtype = config.get("subtype") or ["movie", "tv"]
        self._msgtype = config.get("msgtype") or "Plugin"
        self._subscribe_oper = SubscribeOper()
        self._media_chain = MediaChain()
        self._tmdb_chain = TmdbChain()

        if not self._onlyonce:
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("订阅提醒服务启动，立即运行一次")
        self._scheduler.add_job(
            func=self.__run_once,
            trigger="date",
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            name="订阅提醒",
        )
        self._onlyonce = False
        self.__update_config()
        if self._scheduler.get_jobs():
            self._scheduler.start()

    def __update_config(self) -> None:
        """保存一次性执行完成后的配置状态。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": self._onlyonce,
                "time": self._time,
                "subtype": self._subtype,
                "msgtype": self._msgtype,
            }
        )

    def __run_once(self) -> None:
        """把 APScheduler 的同步回调转交给异步通知任务。"""
        asyncio.run(self.__send_notify())

    @staticmethod
    def __date_matches(value: Any, current_date: str) -> bool:
        """判断日期字段是否代表当前本地日期。"""
        return str(value or "").split("T", 1)[0] == current_date

    def __selected_types(self) -> set[str]:
        """归一化配置中的订阅类型，并丢弃未知值。"""
        values = self._subtype.split(",") if isinstance(self._subtype, str) else self._subtype
        if not isinstance(values, (list, tuple, set)):
            return set()
        return {
            str(value).strip().casefold()
            for value in values
            if str(value).strip().casefold() in {"movie", "tv"}
        }

    def __message_type(self) -> MessageType:
        """将配置中的消息类型名称或显示值映射为当前消息枚举。"""
        if isinstance(self._msgtype, MessageType):
            return self._msgtype
        value = str(self._msgtype or "").strip()
        if value in MessageType.__members__:
            return MessageType[value]
        for item in MessageType:
            if item.value == value:
                return item
        return MessageType.Manual

    @staticmethod
    def __display_name(subscribe: Any) -> str:
        """构造订阅提醒中的媒体标题。"""
        return (
            f"{subscribe.name} ({subscribe.year})"
            if subscribe.year
            else str(subscribe.name)
        )

    async def __tmdb_id(self, subscribe: Any) -> Optional[int]:
        """将订阅的完整媒体身份转换为 TMDB ID，供季集接口使用。"""
        media_source, media_id = resolve_media_identity(media=subscribe)
        if not media_source or not media_id:
            logger.warning(f"订阅 {subscribe.name} 缺少有效媒体身份，跳过电视剧提醒")
            return None

        tmdb_id: Any = media_id
        if media_source != MediaSource.TMDB:
            if self._media_chain is None:
                return None
            converted = await self._media_chain.async_convert_media_identity(
                target_source=MediaSource.TMDB,
                media_source=media_source,
                media_id=media_id,
                mtype=MediaType.TV,
                season=subscribe.season,
            )
            tmdb_id = converted.get("id") if isinstance(converted, dict) else None

        if not str(tmdb_id).isdigit():
            logger.warning(f"订阅 {subscribe.name} 无法转换为有效 TMDB 身份，跳过电视剧提醒")
            return None
        return int(tmdb_id)

    async def __tv_reminder(self, subscribe: Any, current_date: str) -> Optional[dict]:
        """查询电视剧订阅季集信息并返回当天播出的提醒项。"""
        if subscribe.season is None or self._tmdb_chain is None:
            return None
        tmdb_id = await self.__tmdb_id(subscribe)
        if tmdb_id is None:
            return None

        episodes = await self._tmdb_chain.async_tmdb_episodes(
            tmdbid=tmdb_id,
            season=subscribe.season,
            episode_group=subscribe.episode_group,
        )
        episode_numbers = sorted(
            {
                episode.episode_number
                for episode in episodes or []
                if episode
                and episode.episode_number is not None
                and self.__date_matches(episode.air_date, current_date)
            }
        )
        if not episode_numbers:
            return None

        episode_text = (
            f"E{episode_numbers[0]:02d}-E{episode_numbers[-1]:02d}"
            if len(episode_numbers) > 1
            else f"E{episode_numbers[0]:02d}"
        )
        return {
            "name": self.__display_name(subscribe),
            "season": f"S{subscribe.season:02d}",
            "episode": episode_text,
            "image": subscribe.backdrop or subscribe.poster,
        }

    async def __movie_reminder(self, subscribe: Any, current_date: str) -> Optional[dict]:
        """按订阅的完整媒体身份异步读取电影上映日期。"""
        if self._media_chain is None:
            return None
        media_source, media_id = resolve_media_identity(media=subscribe)
        if not media_source or not media_id:
            logger.warning(f"订阅 {subscribe.name} 缺少有效媒体身份，跳过电影提醒")
            return None

        mediainfo = await self._media_chain.async_recognize_media(
            mtype=MediaType.MOVIE,
            media_source=media_source,
            media_id=media_id,
        )
        if not mediainfo or not self.__date_matches(mediainfo.release_date, current_date):
            return None
        return {
            "name": self.__display_name(subscribe),
            "image": subscribe.backdrop or subscribe.poster,
        }

    async def __send_batches(
        self,
        items: list[dict],
        title: str,
        prefix: str,
        message_type: MessageType,
        include_episodes: bool = False,
    ) -> None:
        """按每条消息八项的上限异步投递提醒。"""
        for offset in range(0, len(items), 8):
            batch = items[offset : offset + 8]
            if include_episodes:
                text = "".join(
                    f"{prefix}{item['name']} {item['season']}{item['episode']}\n"
                    for item in batch
                )
            else:
                text = "".join(f"{prefix}{item['name']}\n" for item in batch)
            images = [item["image"] for item in batch if item.get("image")]
            await asyncio.to_thread(
                self.post_message,
                mtype=message_type,
                title=title,
                text=text,
                image=random.choice(images) if images else None,
            )
            logger.info(f"推送{title}：{text}")

    async def __send_notify(self) -> None:
        """异步读取订阅并推送当天更新。"""
        if self._subscribe_oper is None:
            return
        subscribes = await self._subscribe_oper.async_list()
        if not subscribes:
            logger.info("当前没有订阅，跳过处理")
            return

        selected_types = self.__selected_types()
        if not selected_types:
            logger.warning("订阅类型不能为空")
            return

        current_date = datetime.now(tz=pytz.timezone(settings.TZ)).date().isoformat()
        message_type = self.__message_type()
        tv_items: list[dict] = []
        movie_items: list[dict] = []
        for subscribe in subscribes:
            try:
                if subscribe.type == MediaType.TV.value and "tv" in selected_types:
                    item = await self.__tv_reminder(subscribe, current_date)
                    if item:
                        tv_items.append(item)
                elif subscribe.type == MediaType.MOVIE.value and "movie" in selected_types:
                    item = await self.__movie_reminder(subscribe, current_date)
                    if item:
                        movie_items.append(item)
            except Exception as error:
                logger.error(f"订阅 {subscribe.name} 更新查询失败：{error}")

        if tv_items:
            await self.__send_batches(
                tv_items,
                title="电视剧更新",
                prefix="📺︎",
                message_type=message_type,
                include_episodes=True,
            )
        if movie_items:
            await self.__send_batches(
                movie_items,
                title="电影更新",
                prefix="📽︎",
                message_type=message_type,
            )

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """本插件不注册动态 HTTP API。"""
        return []

    @staticmethod
    def __parse_hour(value: Any) -> Optional[int]:
        """将配置的小时值限制在合法的每日时钟范围内。"""
        if isinstance(value, bool):
            return None
        try:
            hour = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return hour if 0 <= hour <= 23 else None

    def get_service(self) -> List[Dict[str, Any]]:
        """注册由主程序调度器执行的每日异步提醒服务。"""
        if not self._enabled:
            return []
        hour = self.__parse_hour(self._time)
        if hour is None:
            logger.error(f"订阅提醒时间配置无效：{self._time}")
            return []
        return [
            {
                "id": "SubscribeReminder",
                "name": "订阅提醒定时服务",
                "trigger": CronTrigger.from_crontab(f"0 {hour} * * *"),
                "func": self.__send_notify,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回订阅提醒的配置页面和默认值。"""
        message_type_options = [
            {"title": item.value, "value": item.name} for item in MessageType
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "time",
                                            "label": "时间",
                                            "placeholder": "默认9点",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "subtype",
                                            "label": "订阅类型",
                                            "items": [
                                                {"title": "电影", "value": "movie"},
                                                {"title": "电视剧", "value": "tv"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": False,
                                            "chips": True,
                                            "model": "msgtype",
                                            "label": "消息类型",
                                            "items": message_type_options,
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "默认每天9点推送，需开启（订阅）通知类型。",
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
            "subtype": ["movie", "tv"],
            "msgtype": "Plugin",
            "time": 9,
        }

    def get_page(self) -> List[dict]:
        """本插件没有详情页。"""
        return []

    def stop_service(self) -> None:
        """停止本插件保留的一次性调度器。"""
        scheduler = self._scheduler
        if not scheduler:
            return
        try:
            scheduler.remove_all_jobs()
            if scheduler.running:
                scheduler.shutdown()
        except Exception as error:
            logger.error(f"退出订阅提醒服务失败：{error}")
            return
        self._scheduler = None
