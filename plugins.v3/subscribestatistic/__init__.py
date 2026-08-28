"""统计近期订阅与下载记录，并通过宿主调度发送汇总通知。"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.subscribe import SubscribeOper
from app.plugins import _PluginBase
from app.schemas.types import MessageType, SystemConfigKey
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import SitesHelper


# pylint: disable=too-many-instance-attributes
class SubscribeStatistic(_PluginBase):
    """统计各站点订阅和下载数量，并投影宿主调度服务。"""
    # 插件名称
    plugin_name = "订阅下载统计"
    # 插件描述
    plugin_desc = "统计指定时间内各站点订阅及下载情况。"
    # 插件图标
    plugin_icon = (
        "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/"
        "main/icons/subscribestatistic.png"
    )
    # 插件版本
    plugin_version = "2.0.0"
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
    _movie_subscribe_days = 30
    _tv_subscribe_days = 30
    _movie_download_days = 7
    _tv_download_days = 7
    _notify_type: List[str] = []
    _msgtype = ""
    _cron: str = ""
    _run_once = False

    def __init__(self) -> None:
        """初始化稳定数据访问端口和热重载状态。"""
        super().__init__()
        self.subscribe = SubscribeOper()
        self.downloadhis = DownloadHistoryOper()
        self._sites_helper = SitesHelper()
        self._reset_state()

    def _reset_state(self) -> None:
        """重置全部配置投影，避免热重载沿用上一次实例状态。"""
        self._enabled = False
        self._notify = False
        self._onlyonce = False
        self._run_once = False
        self._cron = ""
        self._movie_subscribe_days = 30
        self._tv_subscribe_days = 30
        self._movie_download_days = 7
        self._tv_download_days = 7
        self._notify_type = []
        self._msgtype = ""

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """将表单文本归一化为正整数天数。"""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    def init_plugin(self, config: dict = None) -> None:
        """读取配置，并在投影一次性服务前持久化消费开关。"""
        self._reset_state()
        self.subscribe = SubscribeOper()
        self.downloadhis = DownloadHistoryOper()
        self._sites_helper = SitesHelper()
        config = config or {}
        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = str(config.get("cron") or "").strip()
        self._movie_subscribe_days = self._positive_int(
            config.get("movie_subscribe_days"), 30
        )
        self._tv_subscribe_days = self._positive_int(
            config.get("tv_subscribe_days"), 30
        )
        self._movie_download_days = self._positive_int(
            config.get("movie_download_days"), 7
        )
        self._tv_download_days = self._positive_int(
            config.get("tv_download_days"), 7
        )
        notify_type = config.get("notify_type")
        if isinstance(notify_type, str):
            self._notify_type = [notify_type] if notify_type else []
        elif isinstance(notify_type, list):
            self._notify_type = [str(item) for item in notify_type if item]
        self._msgtype = str(config.get("msgtype") or "").strip()

        ready = bool(
            self._enabled
            and self._notify
            and self._msgtype
            and self._notify_type
        )
        if ready and self._onlyonce:
            self._onlyonce = False
            try:
                saved = self.__update_config()
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error("保存订阅下载统计一次性状态失败：%s", type(error).__name__)
                return
            if saved is False:
                logger.error("保存订阅下载统计一次性状态失败，已停止执行")
                return
            self._run_once = True

    def __update_config(self) -> bool:
        """保存归一化配置，确保一次性任务不会被热重载重复投影。"""
        return self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "notify": self._notify,
            "movie_subscribe_days": self._movie_subscribe_days,
            "tv_subscribe_days": self._tv_subscribe_days,
            "movie_download_days": self._movie_download_days,
            "tv_download_days": self._tv_download_days,
            "notify_type": self._notify_type,
            "msgtype": self._msgtype,
        })

    @staticmethod
    def _section(title: str, labels: Iterable[str], values: Iterable[int]) -> str:
        """生成一个按站点降序排列的通知段落。"""
        rows = sorted(zip(labels, values), key=lambda item: item[1], reverse=True)
        total = sum(value for _, value in rows)
        lines = [f"【{title} 共{total}】", *(label for label, _ in rows), ""]
        return "\n".join(lines)

    def notify(self) -> None:
        """生成并发送已选择的订阅与下载统计。"""
        sections = []
        if "movie_subscribes" in self._notify_type:
            _, labels, values = self.__get_movie_subscribes()
            sections.append(self._section(
                f"电影{self._movie_subscribe_days}天内订阅", labels, values
            ))
        if "tv_subscribes" in self._notify_type:
            _, labels, values = self.__get_tv_subscribes()
            sections.append(self._section(
                f"电视剧{self._tv_subscribe_days}天内订阅", labels, values
            ))
        if "movie_downloads" in self._notify_type:
            _, labels, values = self.__get_movie_downloads()
            sections.append(self._section(
                f"电影{self._movie_download_days}天内下载", labels, values
            ))
        if "tv_downloads" in self._notify_type:
            _, labels, values = self.__get_tv_downloads()
            sections.append(self._section(
                f"电视剧{self._tv_download_days}天内下载", labels, values
            ))

        mtype = MessageType.Manual
        if self._msgtype:
            try:
                mtype = MessageType[str(self._msgtype)]
            except (KeyError, TypeError):
                logger.warning("订阅下载统计消息类型无效，使用手动处理")

        self.post_message(
            title="【订阅下载统计】",
            mtype=mtype,
            text="\n".join(sections),
        )

    def get_state(self) -> bool:
        return bool(self._enabled or self._run_once)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """把一次性和周期统计投影为宿主统一调度服务。"""
        services: List[Dict[str, Any]] = []
        ready = bool(
            self._enabled
            and self._notify
            and self._msgtype
            and self._notify_type
        )
        timezone = ZoneInfo(str(settings.TZ))
        if self._run_once:
            services.append({
                "id": "SubscribeStatistic.Once",
                "name": "订阅下载统计（立即运行）",
                "trigger": "date",
                "func": self.notify,
                "kwargs": {
                    "run_date": datetime.now(timezone) + timedelta(seconds=3)
                },
            })
        if ready and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron, timezone=timezone)
            except (TypeError, ValueError) as error:
                logger.error("订阅下载统计周期配置错误：%s", error)
                self.systemmessage.put(f"执行周期配置错误：{error}")
            else:
                services.append({
                    "id": "SubscribeStatistic.Cron",
                    "name": "订阅下载统计",
                    "trigger": trigger,
                    "func": self.notify,
                    "kwargs": {},
                })
        return services

    @staticmethod
    def __site_ids(sites: Any) -> List[Any]:
        """将 V3 JSON 列或历史 JSON 字符串统一为站点 ID 列表。"""
        if not sites:
            return []
        if isinstance(sites, str):
            try:
                sites = json.loads(sites)
            except json.JSONDecodeError:
                logger.warning("订阅下载统计遇到无效的订阅站点配置，已忽略")
                return []
        return sites if isinstance(sites, list) else []

    def __site_name_map(self) -> Dict[str, str]:
        """从公开站点 SDK 构建站点 ID 到名称的只读投影。"""
        try:
            indexers = self._sites_helper.get_indexers() or []
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("读取站点目录失败：%s", type(error).__name__)
            return {}
        return {
            str(indexer.get("id")): str(indexer.get("name"))
            for indexer in indexers
            if isinstance(indexer, dict)
            and indexer.get("id") is not None
            and indexer.get("name")
        }

    def __subscribe_statistics(self, mtype: str, days: int):
        """汇总指定媒体类型订阅中的站点选择次数。"""
        records = self.subscribe.list_by_type(mtype=mtype, days=days) or []
        fallback_sites = self.systemconfig.get(SystemConfigKey.RssSites) or []
        site_ids: List[Any] = []
        for record in records:
            configured = self.__site_ids(record.sites)
            site_ids.extend(configured or fallback_sites)
        counts = Counter(str(site_id) for site_id in site_ids)
        names = self.__site_name_map()
        rows = [
            (f"{names[site_id]}：{count}", count)
            for site_id, count in counts.items()
            if site_id in names
        ]
        return records, [label for label, _ in rows], [value for _, value in rows]

    def __download_statistics(self, mtype: str, days: int):
        """汇总指定媒体类型下载历史中的站点次数。"""
        records = self.downloadhis.list_by_type(mtype=mtype, days=days) or []
        counts = Counter(
            str(record.torrent_site)
            for record in records
            if record.torrent_site
        )
        rows = [(f"{site}：{count}", count) for site, count in counts.items()]
        return records, [label for label, _ in rows], [value for _, value in rows]

    def __get_movie_subscribes(self):
        """
        获取电影订阅统计数据
        """
        return self.__subscribe_statistics("电影", self._movie_subscribe_days)

    def __get_tv_subscribes(self):
        """
        获取电视剧订阅统计数据
        """
        return self.__subscribe_statistics("电视剧", self._tv_subscribe_days)

    def __get_movie_downloads(self):
        """
        获取电影下载统计数据
        """
        return self.__download_statistics("电影", self._movie_download_days)

    def __get_tv_downloads(self):
        """
        获取电视剧下载统计数据
        """
        return self.__download_statistics("电视剧", self._tv_download_days)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 遍历 MessageType 枚举，生成消息类型选项
        msg_type_options = []
        for item in MessageType:
            msg_type_options.append({
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
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'msgtype',
                                            'label': '消息类型',
                                            'items': msg_type_options
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
            "notify_type": ["movie_downloads"],
            "msgtype": []
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

        form_page = []
        if "movie_subscribes" in self._notify_type:
            # 电影订阅
            (
                movie_subscribes,
                movie_subscribe_sites,
                movie_subscribe_datas,
            ) = self.__get_movie_subscribes()
            form_page.append(
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
                                        'text': (
                                            f'电影近 {self._movie_subscribe_days} 天订阅 '
                                            f'{len(movie_subscribes)} 部'
                                        )
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
                }
            )

        if "tv_subscribes" in self._notify_type:
            # 电视剧订阅
            tv_subscribes, tv_subscribe_sites, tv_subscribe_datas = self.__get_tv_subscribes()
            form_page.append(
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
                                        'text': (
                                            f'电视剧近 {self._tv_subscribe_days} 天订阅 '
                                            f'{len(tv_subscribes)} 部'
                                        )
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
                }
            )

        if "movie_downloads" in self._notify_type:
            # 电影下载
            (
                movie_downloads,
                movie_download_sites,
                movie_download_datas,
            ) = self.__get_movie_downloads()
            form_page.append(
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
                                        'text': (
                                            f'电影近 {self._movie_download_days} 天下载 '
                                            f'{len(movie_downloads)} 个种子'
                                        )
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
                }
            )

        if "tv_downloads" in self._notify_type:
            # 电视剧下载
            tv_downloads, tv_download_sites, tv_download_datas = self.__get_tv_downloads()
            form_page.append(
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
                                        'text': (
                                            f'电视剧近 {self._tv_download_days} 天下载 '
                                            f'{len(tv_downloads)} 个种子'
                                        )
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
            )

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': form_page
            }
        ]

    def stop_service(self) -> None:
        """宿主统一管理本插件投影的调度服务，停止时无本地资源需要释放。"""
