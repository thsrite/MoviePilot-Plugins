from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from app.db.transferhistory_oper import TransferHistoryOper
from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.modules.emby import Emby
from app.modules.jellyfin import Jellyfin
from app.modules.plex import Plex
from app.schemas import RefreshMediaItem


class MetaRefresh(_PluginBase):
    # 插件名称
    plugin_name = "媒体库元数据刷新"
    # 插件描述
    plugin_desc = "定时刷新媒体库元数据，获取TMDB最新元数据信息。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/media.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "metarefresh_"
    # 加载顺序
    plugin_order = 32
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _days = None
    _servers = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._days = config.get("days") or 5
            self._servers = config.get("servers") or []

            # 加载模块
        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 立即运行一次
            if self._onlyonce:
                logger.info(f"媒体库元数据刷新服务启动，立即运行一次")
                self._scheduler.add_job(self.refresh, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="媒体库元数据")

                # 关闭一次性开关
                self._onlyonce = False

                # 保存配置
                self.__update_config()

            # 周期运行
            if self._cron:
                try:
                    self._scheduler.add_job(func=self.refresh,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="媒体库元数据")
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")
                    # 推送实时消息
                    self.systemmessage.put(f"执行周期配置错误：{err}")

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config(
            {
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "enabled": self._enabled,
                "days": self._days,
                "servers": self._servers,
            }
        )

    def refresh(self):
        """
        刷新媒体库元数据
        """
        if not self._servers:
            logger.error("没有选择刷新媒体服务器")
            return

        # 获取days内入库的媒体
        current_date = datetime.now()
        # 计算几天前的日期
        target_date = current_date - timedelta(days=int(self._days))
        transferhistorys = TransferHistoryOper().list_by_date(target_date)
        if not transferhistorys:
            logger.error(f"{self._days}天内没有媒体库入库记录")
            return

        logger.info(f"开始刷新媒体库元数据，最近{self._days}天内入库媒体：{len(transferhistorys)}个")
        # 刷新媒体库
        items = [
            RefreshMediaItem(
                title=transferinfo.title,
                year=transferinfo.year,
                type=transferinfo.type,
                category=transferinfo.category,
                target_path=transferinfo.dest
            )
            for transferinfo in transferhistorys
        ]

        if "emby" in self._servers and "emby" in settings.MEDIASERVER:
            Emby().refresh_library_by_items(items)
        if "jellyfin" in self._servers and "jellyfin" in settings.MEDIASERVER:
            # FIXME Jellyfin未找到刷新单个项目的API
            Jellyfin().refresh_root_library()
        if "plex" in self._servers and "plex" in settings.MEDIASERVER:
            Plex().refresh_library_by_items(items)

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
                "component": "VForm",
                "content": [
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
                                    'md': 6
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
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'days',
                                            'label': '最新入库天数'
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
                                            'model': 'servers',
                                            'label': '媒体服务器',
                                            'items': [
                                                {
                                                    "title": "emby",
                                                    "vale": "emby"
                                                },
                                                {
                                                    "title": "jellyfin",
                                                    "vale": "jellyfin"
                                                },
                                                {
                                                    "title": "plex",
                                                    "vale": "plex"
                                                }
                                            ]
                                        }
                                    }
                                ]
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "days": 5,
            "servers": []
        }

    def get_page(self) -> List[dict]:
        pass

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
