import base64
import copy
import json
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dateutil.parser import isoparse
from requests import RequestException
from zhconv import zhconv

from app import schemas
from app.chain.tmdb import TmdbChain
from app.core.event import eventmanager, Event
from app.db.transferhistory_oper import TransferHistoryOper
from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.modules.emby import Emby
from app.schemas.types import EventType, MediaType
from app.utils.common import retry
from app.utils.http import RequestUtils
from app.utils.string import StringUtils


class EmbyMetaRefresh(_PluginBase):
    # 插件名称
    plugin_name = "Emby元数据刷新"
    # 插件描述
    plugin_desc = "定时刷新Emby媒体库元数据，演职人员中文。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/emby-icon.png"
    # 插件版本
    plugin_version = "1.7.4"
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

    # 退出事件
    _event = threading.Event()
    # 私有属性
    _enabled = False
    tmdbchain = None
    _onlyonce = False
    _cron = None
    _actor_chi = False
    _num = None
    _refresh_type = None
    _ReplaceAllMetadata = "true"
    _ReplaceAllImages = "true"
    _actor_path = None
    mediaserver_helper = None
    _EMBY_HOST = None
    _EMBY_USER = None
    _EMBY_APIKEY = None
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()
        self.tmdbchain = TmdbChain()
        self.mediaserver_helper = MediaServerHelper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._actor_chi = config.get("actor_chi")
            self._num = config.get("num") or 5
            self._actor_path = config.get("actor_path")
            self._refresh_type = config.get("refresh_type") or "历史记录"
            self._ReplaceAllMetadata = config.get("ReplaceAllMetadata") or "true"
            self._ReplaceAllImages = config.get("ReplaceAllImages") or "true"

            emby_server = self.mediaserver_helper.get_service(name="Emby")
            if not emby_server:
                logger.error("未配置Emby媒体服务器")
                return

            self._EMBY_USER = emby_server.instance.get_user()
            self._EMBY_HOST = emby_server.config.get("host")
            self._EMBY_APIKEY = emby_server.config.get("apikey")
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
                "actor_chi": self._actor_chi,
                "num": self._num,
                "refresh_type": self._refresh_type,
                "ReplaceAllMetadata": self._ReplaceAllMetadata,
                "ReplaceAllImages": self._ReplaceAllImages,
                "actor_path": self._actor_path,
            }
        )

    def refresh(self):
        """
        刷新媒体库元数据
        """
        if "emby" not in settings.MEDIASERVER:
            logger.error("未配置Emby媒体服务器")
            return

        if str(self._refresh_type) == "历史记录":
            # 获取days内入库的媒体
            current_date = datetime.now()
            # 计算几天前的日期
            target_date = current_date - timedelta(days=int(self._num))
            transferhistorys = TransferHistoryOper().list_by_date(target_date.strftime('%Y-%m-%d'))
            if not transferhistorys:
                logger.error(f"{self._num}天内没有媒体库入库记录")
                return

            logger.info(f"开始刷新媒体库元数据，最近 {self._num} 天内入库媒体：{len(transferhistorys)}个")
            # 刷新媒体库
            for transferinfo in transferhistorys:
                self.__refresh_emby(transferinfo)
        else:
            latest = self.__get_latest_media()
            if not latest:
                logger.error(f"Emby中没有最新媒体")
                return

            logger.info(f"开始刷新媒体库元数据，{self._num} 天内最新媒体：{len(latest)} 个")

            # 已处理的媒体
            handle_items = {}

            # 刷新媒体库
            for item in latest:
                logger.info(
                    f"开始刷新媒体库元数据，最新媒体：{'电视剧' if str(item.get('Type')) == 'Episode' else '电影'} {'%s S%02dE%02d %s' % (item.get('SeriesName'), item.get('ParentIndexNumber'), item.get('IndexNumber'), item.get('Name')) if str(item.get('Type')) == 'Episode' else item.get('Name')} {item.get('Id')}")
                self.__refresh_emby_library_by_id(item.get("Id"))

                # 刮演员中文
                if self._actor_chi:
                    key = f"{item.get('Type')}-{item.get('SeriesName') if str(item.get('Type')) == 'Episode' else item.get('Name')}"
                    peoples = None
                    if key not in handle_items.keys():
                        peoples = self.__update_people_chi(
                            item_id=item.get("SeriesId") if str(item.get('Type')) == 'Episode' else item.get("Id"),
                            title=item.get('SeriesName') if str(item.get('Type')) == 'Episode' else item.get('Name'),
                            type=MediaType('电视剧' if str(item.get('Type')) == 'Episode' else '电影'),
                            season=item.get("ParentIndexNumber") if str(item.get('Type')) == 'Episode' else None
                        )

                    # 是否有演员信息
                    if str(item.get('Type')) == 'Episode':
                        item_dicts = handle_items.get(key, {})
                        item_ids = item_dicts.get('itemIds', [])
                        item_actors = item_dicts.get('actors', [])
                        item_ids.append(item.get("Id"))
                        handle_items[key] = {
                            'itemIds': item_ids,
                            'actors': peoples or item_actors
                        }
            # 处理剧集
            for key, value in handle_items.items():
                if value:
                    item_ids = value.get('itemIds', [])
                    item_actors = value.get('actors', [])
                    for item_id in item_ids:
                        item_info = self.__get_item_info(item_id)
                        if item_actors == item_info.get("People"):
                            logger.warn(
                                f"最新媒体：{'电视剧' if str(item_info.get('Type')) == 'Episode' else '电影'} {'%s S%02dE%02d %s' % (item_info.get('SeriesName'), item_info.get('ParentIndexNumber'), item_info.get('IndexNumber'), item_info.get('Name')) if str(item_info.get('Type')) == 'Episode' else item_info.get('Name')} {item_info.get('Id')} 演员信息已更新，跳过")
                            continue
                        item_info["People"] = item_actors
                        item_info["LockedFields"].append("Cast")
                        flag = self.set_iteminfo(itemid=item_info.get("Id"), iteminfo=item_info)
                        logger.info(
                            f"最新媒体：{'电视剧' if str(item_info.get('Type')) == 'Episode' else '电影'} {'%s S%02dE%02d %s' % (item_info.get('SeriesName'), item_info.get('ParentIndexNumber'), item_info.get('IndexNumber'), item_info.get('Name')) if str(item_info.get('Type')) == 'Episode' else item_info.get('Name')} {item_info.get('Id')} 演员信息完成 {flag}")

        logger.info(f"刷新媒体库元数据完成")

    def __get_latest_media(self) -> List[dict]:
        """
        获取Emby中最新媒体
        """
        refresh_date = datetime.utcnow() - timedelta(days=int(self._num))
        refresh_date = refresh_date.replace(tzinfo=pytz.utc)  # 添加UTC时区信息
        try:
            latest_medias = self.__get_latest(limit=1000)
            if not latest_medias:
                return []

            _latest_medias = []
            for media in latest_medias:
                media_date = media.get("DateCreated")
                # 截断微秒部分，使其长度为六位数
                media_date = isoparse(media_date)
                if media_date > refresh_date:
                    _latest_medias.append(media)
                else:
                    break
            return _latest_medias
        except Exception as err:
            logger.error(f"获取Emby中最新媒体失败：{str(err)}")
            return []

    def __update_people_chi(self, item_id, title, type, season=None):
        """
        刮削演员中文名
        """
        # 刮演员中文
        item_info = self.__get_item_info(item_id)
        if item_info:
            if self._actor_path and not any(
                    str(actor_path) in item_info.get("Path") for actor_path in self._actor_path.split(",")):
                return None

            imdb_id = item_info.get("ProviderIds", {}).get("Imdb")
            if self.__need_trans_actor(item_info):
                logger.info(f"开始获取 {title} ({item_info.get('ProductionYear')}) 的豆瓣演员信息 ...")
                douban_actors = self.__get_douban_actors(title=title,
                                                         imdb_id=imdb_id,
                                                         type=type,
                                                         year=item_info.get("ProductionYear"),
                                                         season=season)
                if not douban_actors:
                    logger.info(f"未找到 {title} ({item_info.get('ProductionYear')}) 的豆瓣演员信息")
                    return None

                logger.debug(
                    f"获取 {title} ({item_info.get('ProductionYear')}) 的豆瓣演员信息 完成，演员：{douban_actors}")
                peoples = self.__update_peoples(itemid=item_id, iteminfo=item_info,
                                                douban_actors=douban_actors)

                return peoples
            else:
                logger.info(f"媒体 {title} ({item_info.get('ProductionYear')}) 演员信息无需更新")
        return item_info.get("People")

    def __update_peoples(self, itemid: str, iteminfo: dict, douban_actors):
        # 处理媒体项中的人物信息
        """
        "People": [
            {
              "Name": "丹尼尔·克雷格",
              "Id": "33625",
              "Role": "James Bond",
              "Type": "Actor",
              "PrimaryImageTag": "bef4f764540f10577f804201d8d27918"
            }
        ]
        """
        peoples = []
        need_update_people = False
        # 更新当前媒体项人物
        for people in iteminfo["People"] or []:
            if self._event.is_set():
                logger.info(f"演职人员刮削服务停止")
                return
            if not people.get("Name"):
                continue
            if StringUtils.is_chinese(people.get("Name")) \
                    and StringUtils.is_chinese(people.get("Role")):
                peoples.append(people)
                continue
            info = self.__update_people(people=people,
                                        douban_actors=douban_actors)
            if info:
                logger.info(
                    f"更新演职人员 {people.get('Name')} ({people.get('Role')}) 信息：{info.get('Name')} ({info.get('Role')})")
                need_update_people = True
                peoples.append(info)
            else:
                peoples.append(people)

        item_name = f"{iteminfo.get('Name')} ({iteminfo.get('ProductionYear')})" if iteminfo.get(
            'Type') == 'Series' or iteminfo.get(
            'Type') == 'Movie' else f"{iteminfo.get('SeriesName')} ({iteminfo.get('ProductionYear')}) {iteminfo.get('SeasonName')} {iteminfo.get('Name')}"
        # 保存媒体项信息
        if peoples and need_update_people:
            iteminfo["People"] = peoples
            iteminfo["LockedFields"].append("Cast")
            flag = self.set_iteminfo(itemid=itemid, iteminfo=iteminfo)
            logger.info(
                f"更新媒体 {item_name} 演员信息完成 {flag}")
        else:
            logger.info(f"媒体 {item_name} 演员信息无需更新")

        return iteminfo["People"]

    def __update_people(self, people: dict, douban_actors: list = None) -> Optional[dict]:
        """
        更新人物信息，返回替换后的人物信息
        """

        def __get_emby_iteminfo() -> dict:
            """
            获得Emby媒体项详情
            """
            try:
                url = f'[HOST]emby/Users/[USER]/Items/{people.get("Id")}?' \
                      f'Fields=ChannelMappingInfo&api_key=[APIKEY]'
                res = Emby().get_data(url=url)
                if res:
                    return res.json()
            except Exception as err:
                logger.error(f"获取Emby媒体项详情失败：{str(err)}")
            return {}

        def __get_peopleid(p: dict) -> Tuple[Optional[str], Optional[str]]:
            """
            获取人物的TMDBID、IMDBID
            """
            if not p.get("ProviderIds"):
                return None, None
            peopletmdbid, peopleimdbid = None, None
            if "Tmdb" in p["ProviderIds"]:
                peopletmdbid = p["ProviderIds"]["Tmdb"]
            if "tmdb" in p["ProviderIds"]:
                peopletmdbid = p["ProviderIds"]["tmdb"]
            if "Imdb" in p["ProviderIds"]:
                peopleimdbid = p["ProviderIds"]["Imdb"]
            if "imdb" in p["ProviderIds"]:
                peopleimdbid = p["ProviderIds"]["imdb"]
            return peopletmdbid, peopleimdbid

        # 返回的人物信息
        ret_people = copy.deepcopy(people)

        try:
            # 查询媒体库人物详情
            personinfo = __get_emby_iteminfo()
            if not personinfo:
                logger.warn(f"未找到人物 {people.get('Name')} 的信息")
                return None

            # 是否更新标志
            updated_name = False
            updated_overview = False
            update_character = False
            profile_path = None

            # 从TMDB信息中更新人物信息
            person_tmdbid, person_imdbid = __get_peopleid(personinfo)
            if person_tmdbid:
                person_detail = self.tmdbchain.person_detail(int(person_tmdbid))
                if person_detail:
                    cn_name = self.__get_chinese_name(person_detail)
                    # 图片优先从TMDB获取
                    profile_path = person_detail.profile_path
                    if profile_path:
                        logger.debug(f"{people.get('Name')} 从TMDB获取到图片：{profile_path}")
                        profile_path = f"https://{settings.TMDB_IMAGE_DOMAIN}/t/p/original{profile_path}"
                    if cn_name:
                        # 更新中文名
                        logger.debug(f"{people.get('Name')} 从TMDB获取到中文名：{cn_name}")
                        personinfo["Name"] = cn_name
                        ret_people["Name"] = cn_name
                        updated_name = True
                        # 更新中文描述
                        biography = person_detail.biography
                        if biography and StringUtils.is_chinese(biography):
                            logger.debug(f"{people.get('Name')} 从TMDB获取到中文描述")
                            personinfo["Overview"] = biography
                            updated_overview = True

            # 从豆瓣信息中更新人物信息
            """
            {
              "name": "丹尼尔·克雷格",
              "roles": [
                "演员",
                "制片人",
                "配音"
              ],
              "title": "丹尼尔·克雷格（同名）英国,英格兰,柴郡,切斯特影视演员",
              "url": "https://movie.douban.com/celebrity/1025175/",
              "user": null,
              "character": "饰 詹姆斯·邦德 James Bond 007",
              "uri": "douban://douban.com/celebrity/1025175?subject_id=27230907",
              "avatar": {
                "large": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p42588.jpg?imageView2/2/q/80/w/600/h/3000/format/webp",
                "normal": "https://qnmob3.doubanio.com/view/celebrity/raw/public/p42588.jpg?imageView2/2/q/80/w/200/h/300/format/webp"
              },
              "sharing_url": "https://www.douban.com/doubanapp/dispatch?uri=/celebrity/1025175/",
              "type": "celebrity",
              "id": "1025175",
              "latin_name": "Daniel Craig"
            }
            """
            if douban_actors and (not updated_name
                                  or not updated_overview
                                  or not update_character):
                # 从豆瓣演员中匹配中文名称、角色和简介
                for douban_actor in douban_actors:
                    if douban_actor.get("latin_name") == people.get("Name") \
                            or douban_actor.get("name") == people.get("Name"):
                        # 名称
                        if not updated_name:
                            logger.info(f"{people.get('Name')} 从豆瓣中获取到中文名：{douban_actor.get('name')}")
                            personinfo["Name"] = douban_actor.get("name")
                            ret_people["Name"] = douban_actor.get("name")
                            updated_name = True
                        # 描述
                        if not updated_overview:
                            if douban_actor.get("title"):
                                logger.info(f"{people.get('Name')} 从豆瓣中获取到中文描述：{douban_actor.get('title')}")
                                personinfo["Overview"] = douban_actor.get("title")
                                updated_overview = True
                        # 饰演角色
                        if not update_character:
                            if douban_actor.get("character"):
                                # "饰 詹姆斯·邦德 James Bond 007"
                                character = re.sub(r"饰\s+", "",
                                                   douban_actor.get("character"))
                                character = re.sub("演员", "",
                                                   character)
                                character = re.sub("voice", "配音",
                                                   character)
                                character = re.sub("Director", "导演",
                                                   character)
                                if character:
                                    logger.debug(f"{people.get('Name')} 从豆瓣中获取到饰演角色：{character}")
                                    ret_people["Role"] = character
                                    update_character = True
                        # 图片
                        if not profile_path:
                            avatar = douban_actor.get("avatar") or {}
                            if avatar.get("large"):
                                logger.info(f"{people.get('Name')} 从豆瓣中获取到图片：{avatar.get('large')}")
                                profile_path = avatar.get("large")
                        break

            # 更新人物图片
            if profile_path:
                logger.debug(f"更新人物 {people.get('Name')} 的图片：{profile_path}")
                self.set_item_image(itemid=people.get("Id"), imageurl=profile_path)

            # 锁定人物信息
            if updated_name:
                if "Name" not in personinfo["LockedFields"]:
                    personinfo["LockedFields"].append("Name")
            if updated_overview:
                if "Overview" not in personinfo["LockedFields"]:
                    personinfo["LockedFields"].append("Overview")

            # 更新人物信息
            if updated_name or updated_overview or update_character:
                logger.debug(f"更新人物 {people.get('Name')} 的信息：{personinfo}")
                ret = self.set_iteminfo(itemid=people.get("Id"), iteminfo=personinfo)
                if ret:
                    return ret_people
            else:
                logger.debug(f"人物 {people.get('Name')} 未找到中文数据")
                return None
        except Exception as err:
            logger.error(f"更新人物信息失败：{str(err)}")
            return None

    @staticmethod
    def set_iteminfo(itemid: str, iteminfo: dict):
        """
        更新媒体项详情
        """

        def __set_emby_iteminfo():
            """
            更新Emby媒体项详情
            """
            try:
                res = Emby().post_data(
                    url=f'[HOST]emby/Items/{itemid}?api_key=[APIKEY]&reqformat=json',
                    data=json.dumps(iteminfo),
                    headers={
                        "Content-Type": "application/json"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Emby媒体项详情失败，错误码：{res.status_code}")
                    return False
            except Exception as err:
                logger.error(f"更新Emby媒体项详情失败：{str(err)}")
            return False

        return __set_emby_iteminfo()

    @staticmethod
    @retry(RequestException, logger=logger)
    def set_item_image(itemid: str, imageurl: str):
        """
        更新媒体项图片
        """

        def __download_image():
            """
            下载图片
            """
            try:
                if "doubanio.com" in imageurl:
                    r = RequestUtils(headers={
                        'Referer': "https://movie.douban.com/"
                    }, ua=settings.USER_AGENT).get_res(url=imageurl, raise_exception=True)
                else:
                    r = RequestUtils().get_res(url=imageurl, raise_exception=True)
                if r:
                    return base64.b64encode(r.content).decode()
                else:
                    logger.warn(f"{imageurl} 图片下载失败，请检查网络连通性")
            except Exception as err:
                logger.error(f"下载图片失败：{str(err)}")
            return None

        def __set_emby_item_image(_base64: str):
            """
            更新Emby媒体项图片
            """
            try:
                url = f'[HOST]emby/Items/{itemid}/Images/Primary?api_key=[APIKEY]'
                res = Emby().post_data(
                    url=url,
                    data=_base64,
                    headers={
                        "Content-Type": "image/png"
                    }
                )
                if res and res.status_code in [200, 204]:
                    return True
                else:
                    logger.error(f"更新Emby媒体项图片失败，错误码：{res.status_code}")
                    return False
            except Exception as result:
                logger.error(f"更新Emby媒体项图片失败：{result}")
            return False

        # 下载图片获取base64
        image_base64 = __download_image()
        if image_base64:
            return __set_emby_item_image(image_base64)

        return None

    @staticmethod
    def __get_chinese_name(personinfo: schemas.MediaPerson) -> str:
        """
        获取TMDB别名中的中文名
        """
        try:
            also_known_as = personinfo.also_known_as or []
            if also_known_as:
                for name in also_known_as:
                    if name and StringUtils.is_chinese(name):
                        # 使用cn2an将繁体转化为简体
                        return zhconv.convert(name, "zh-hans")
        except Exception as err:
            logger.error(f"获取人物中文名失败：{err}")
        return ""

    def __get_douban_actors(self, title, imdb_id, type, year, season: int = None) -> List[dict]:
        """
        获取豆瓣演员信息
        """
        # 随机休眠 3-10 秒
        sleep_time = 3 + int(time.time()) % 7
        logger.debug(f"随机休眠 {sleep_time}秒 ...")
        time.sleep(sleep_time)
        # 匹配豆瓣信息
        doubaninfo = self.chain.match_doubaninfo(name=title,
                                                 imdbid=imdb_id,
                                                 mtype=type,
                                                 year=year,
                                                 season=season)
        # 豆瓣演员
        if doubaninfo:
            doubanitem = self.chain.douban_info(doubaninfo.get("id")) or {}
            return (doubanitem.get("actors") or []) + (doubanitem.get("directors") or [])
        else:
            logger.info(f"未找到豆瓣信息：{title} {year}")
        return []

    @staticmethod
    def __need_trans_actor(item):
        """
        是否需要处理人物信息
        """
        _peoples = [x for x in item.get("People", []) if
                    (x.get("Name") and not StringUtils.is_chinese(x.get("Name")))
                    or (x.get("Role") and not StringUtils.is_chinese(x.get("Role")))]
        if _peoples:
            return True
        return False

    def __get_item_info(self, item_id):
        res = RequestUtils().get_res(
            f"{self._EMBY_HOST}/emby/Users/{self._EMBY_USER}/Items/{item_id}?api_key={self._EMBY_APIKEY}")
        if res and res.status_code == 200:
            return res.json()
        return {}

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程刷新媒体库
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "emby_meta_refresh":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始刷新Emby元数据 ...",
                              userid=event.event_data.get("user"))
        self.refresh()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="刷新Emby元数据完成！", userid=event.event_data.get("user"))

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
                if self._actor_chi:
                    self.__update_people_chi(item_id=movie.item_id, title=movie.title, type=MediaType.MOVIE)
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
            if self._actor_chi:
                self.__update_people_chi(item_id=item_id, title=transferinfo.title, type=MediaType.TV, season=season)

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
        req_url = "%semby/Items/%s/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh" \
                  "&ImageRefreshMode=FullRefresh&ReplaceAllMetadata=%s&ReplaceAllImages=%s&api_key=%s" % (
                      self._EMBY_HOST, item_id, self._ReplaceAllMetadata, self._ReplaceAllImages, self._EMBY_APIKEY)
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

    def __get_latest(self, limit) -> list:
        """
        获取最新入库项目
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = "%semby/Users/%s/Items?Limit=%s&api_key=%s&SortBy=DateCreated,SortName&SortOrder=Descending&IncludeItemTypes=Episode,Movie&Recursive=true&Fields=DateCreated" % (
            self._EMBY_HOST, self._EMBY_USER, limit, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.json().get("Items")
                else:
                    logger.info(f"获取最新入库项目失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接Items出错：" + str(e))
            return []

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
        return [{
            "cmd": "/emby_meta_refresh",
            "event": EventType.PluginAction,
            "desc": "Emby媒体库刷新",
            "category": "",
            "data": {
                "action": "emby_meta_refresh"
            }
        }]

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
                                            'model': 'actor_chi',
                                            'label': '刮削演员中文',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'refresh_type',
                                            'label': '刷新方式',
                                            'items': [
                                                {'title': '历史记录', 'value': '历史记录'},
                                                {'title': '最新入库', 'value': '最新入库'},
                                            ]
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
                                            'model': 'num',
                                            'label': '最新入库天数/历史记录天数'
                                        }
                                    }
                                ]
                            }
                        ],
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'ReplaceAllImages',
                                            'label': '覆盖图片',
                                            'items': [
                                                {'title': 'true', 'value': "true"},
                                                {'title': 'false', 'value': "false"},
                                            ]
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
                                            'model': 'ReplaceAllMetadata',
                                            'label': '覆盖元数据',
                                            'items': [
                                                {'title': 'true', 'value': "true"},
                                                {'title': 'false', 'value': "false"},
                                            ]
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
                                            'model': 'actor_path',
                                            'label': '演员刮削生效路径关键词',
                                            'placeholder': '留空则全部处理，否则只处理相应路径关键词的媒体(多个英文逗号分割)'
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
                                            'text': '周期请求媒体服务器元数据刷新接口。注：只支持Emby。'
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
            "actor_chi": False,
            "ReplaceAllMetadata": "true",
            "ReplaceAllImages": "true",
            "cron": "5 1 * * *",
            "refresh_type": "历史记录",
            "actor_path": "",
            "num": 5
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
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
