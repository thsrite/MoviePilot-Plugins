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
from app.utils.http import RequestUtils


class EmbyMetaRefresh(_PluginBase):
    # 插件名称
    plugin_name = "Emby媒体库元数据刷新"
    # 插件描述
    plugin_desc = "定时刷新Emby媒体库元数据。"
    # 插件图标
    plugin_icon = "Emby_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embymetarefresh_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _onlyonce = False
    _cron = None
    _days = None
    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._days = config.get("days") or 5

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

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
                "days": self._days
            }
        )

    def refresh(self):
        """
        刷新媒体库元数据
        """
        if "emby" not in settings.MEDIASERVER:
            logger.error("未配置Emby媒体服务器")
            return

        # 获取days内入库的媒体
        current_date = datetime.now()
        # 计算几天前的日期
        target_date = current_date - timedelta(days=int(self._days))
        transferhistorys = TransferHistoryOper().list_by_date(target_date.strftime('%Y-%m-%d'))
        if not transferhistorys:
            logger.error(f"{self._days}天内没有媒体库入库记录")
            return

        logger.info(f"开始刷新媒体库元数据，最近{self._days}天内入库媒体：{len(transferhistorys)}个")
        # 刷新媒体库
        for transferinfo in transferhistorys:
            self.__refresh_emby(transferinfo)
        logger.info(f"刷新媒体库元数据完成")

    def __refresh_emby(self, transferinfo):
        """
        刷新emby
        """
        if transferinfo.type == "电影":
            movies = Emby().get_movies(title=transferinfo.title, year=transferinfo.year)
            if not movies:
                logger.error(f"Emby中没有找到{transferinfo.title} ({transferinfo.year})")
                return
            for movie in movies:
                self.__refresh_emby_library_by_id(item_id=movie.item_id)
                logger.info(f"已通知刷新Emby电影：{movie.title} ({movie.year}) item_id:{movie.item_id}")
        else:
            item_id = self.__get_emby_series_id_by_name(name=transferinfo.title, year=transferinfo.year)
            if not item_id or item_id is None:
                logger.error(f"Emby中没有找到{transferinfo.title} ({transferinfo.year})")
                return

            # 验证tmdbid是否相同
            item_info = Emby().get_iteminfo(item_id)
            if item_info:
                if transferinfo.tmdbid and item_info.tmdbid:
                    if str(transferinfo.tmdbid) != str(item_info.tmdbid):
                        logger.error(f"Emby中{transferinfo.title} ({transferinfo.year})的tmdbId与入库记录不一致")
                        return

            # 查询集的item_id
            season = int(transferinfo.seasons.replace("S", ""))
            episode = int(transferinfo.episodes.replace("E", ""))
            episode_item_id = self.__get_emby_episode_item_id(item_id=item_id, season=season, episode=episode)
            if not episode_item_id or episode_item_id is None:
                logger.error(
                    f"Emby中没有找到{transferinfo.title} ({transferinfo.year}) {transferinfo.seasons}{transferinfo.episodes}")
                return

            self.__refresh_emby_library_by_id(item_id=episode_item_id)
            logger.info(
                f"已通知刷新Emby电视剧：{transferinfo.title} ({transferinfo.year}) {transferinfo.seasons}{transferinfo.episodes} item_id:{episode_item_id}")

    def __get_emby_episode_item_id(self, item_id: str, season: int, episode: int) -> Optional[str]:
        """
        根据剧集信息查询Emby中集的item_id
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = "%semby/Shows/%s/Episodes?Season=%s&IsMissing=false&api_key=%s" % (
            self._EMBY_HOST, item_id, season, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res_json:
                if res_json:
                    tv_item = res_json.json()
                    res_items = tv_item.get("Items")
                    for res_item in res_items:
                        season_index = res_item.get("ParentIndexNumber")
                        if not season_index:
                            continue
                        if season and season != season_index:
                            continue
                        episode_index = res_item.get("IndexNumber")
                        if not episode_index:
                            continue
                        if episode and episode != episode_index:
                            continue
                        episode_item_id = res_item.get("Id")
                        return episode_item_id
        except Exception as e:
            logger.error(f"连接Shows/Id/Episodes出错：" + str(e))
            return None
        return None

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        req_url = "%semby/Items/%s/Refresh?MetadataRefreshMode=FullRefresh" \
                  "&ImageRefreshMode=FullRefresh&ReplaceAllMetadata=true&ReplaceAllImages=true&api_key=%s" % (
                      self._EMBY_HOST, item_id, self._EMBY_APIKEY)
        try:
            with RequestUtils().post_res(req_url) as res:
                if res:
                    return True
                else:
                    logger.info(f"刷新媒体库对象 {item_id} 失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Items/Id/Refresh出错：" + str(e))
            return False
        return False

    def __get_emby_series_id_by_name(self, name: str, year: str) -> Optional[str]:
        """
        根据名称查询Emby中剧集的SeriesId
        :param name: 标题
        :param year: 年份
        :return: None 表示连不通，""表示未找到，找到返回ID
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return None
        req_url = ("%semby/Items?"
                   "IncludeItemTypes=Series"
                   "&Fields=ProductionYear"
                   "&StartIndex=0"
                   "&Recursive=true"
                   "&SearchTerm=%s"
                   "&Limit=10"
                   "&IncludeSearchTypes=false"
                   "&api_key=%s") % (
                      self._EMBY_HOST, name, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    res_items = res.json().get("Items")
                    if res_items:
                        for res_item in res_items:
                            if res_item.get('Name') == name and (
                                    not year or str(res_item.get('ProductionYear')) == str(year)):
                                return res_item.get('Id')
        except Exception as e:
            logger.error(f"连接Items出错：" + str(e))
            return None
        return ""

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
                                    'md': 6
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
                                    'md': 6
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
                            }
                        ],
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
                                            'text': '查询入库记录，周期请求媒体服务器元数据刷新接口。注：只支持Emby。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "days": 5
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
