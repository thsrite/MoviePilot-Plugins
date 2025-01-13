import datetime
import json
import re
import threading
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyAudioBook(_PluginBase):
    # 插件名称
    plugin_name = "Emby有声书整理"
    # 插件描述
    plugin_desc = "还在为Emby有声书整理烦恼吗？入库存在很多单集？"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/audiobook.png"
    # 插件版本
    plugin_version = "1.4.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyaudiobook_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _enabled = False
    _notify = False
    _rename = False
    _onlyonce = False
    _cron = None
    _library_id = None
    _msgtype = None
    _mediaservers = None

    mediaserver_helper = None
    _EMBY_HOST = None
    _EMBY_USER = None
    _EMBY_APIKEY = None

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.mediaserver_helper = MediaServerHelper()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._library_id = config.get("library_id")
            self._notify = config.get("notify")
            self._rename = config.get("rename")
            self._msgtype = config.get("msgtype")
            self._mediaservers = config.get("mediaservers") or []

            # 停止现有任务
            self.stop_service()

            if self._enabled or self._onlyonce:
                # 定时服务管理器
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 启用目录监控
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.check,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby有声书整理")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{str(err)}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 运行一次定时服务
                if self._onlyonce:
                    logger.info("Emby有声书整理服务启动，立即运行一次")
                    self._scheduler.add_job(name="Emby有声书整理", func=self.check, trigger='date',
                                            run_date=datetime.datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                            )
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()

                # 启动定时服务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "library_id": self._library_id,
            "rename": self._rename,
            "cron": self._cron,
            "notify": self._notify,
            "msgtype": self._msgtype,
            "mediaservers": self._mediaservers,
        })

    def check(self):
        if not self._library_id:
            logger.error("请设置有声书文件夹ID！")
            return

        emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
        if not emby_servers:
            logger.error("未配置Emby媒体服务器")
            return

        for emby_name, emby_server in emby_servers.items():
            logger.info(f"开始处理媒体服务器 {emby_name}")
            self._EMBY_USER = emby_server.instance.get_user()
            self._EMBY_APIKEY = emby_server.config.config.get("apikey")
            self._EMBY_HOST = emby_server.config.config.get("host")
            if not self._EMBY_HOST.endswith("/"):
                self._EMBY_HOST += "/"
            if not self._EMBY_HOST.startswith("http"):
                self._EMBY_HOST = "http://" + self._EMBY_HOST

            # 获取所有有声书
            items = self.__get_items(parent_id=int(self._library_id))
            if not items:
                logger.error(f"获取媒体库 {self._library_id} 有声书列表失败！")
                return

            # 检查有声书是否需要整理
            for item in items:
                book_items = self.__get_items(item.get("Id"))
                if not book_items:
                    logger.error(f"获取 {item.get('Name')} {item.get('Id')} 有声书失败！")
                    return

                # 检查有声书是否需要整理
                __need_zl = False
                for book_item in book_items:
                    if not book_item.get("AlbumId"):
                        __need_zl = True
                        break

                # 需要整理的提示需要整理
                if __need_zl:
                    logger.info(f"有声书 {item.get('Name')} 需要整理，共 {len(book_items)} 集")
                    # self.__zl(items, -1)
                    # 发送通知
                    if self._notify:
                        mtype = NotificationType.Manual
                        if self._msgtype:
                            mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
                        self.post_message(title="Emby有声书整理",
                                          mtype=mtype,
                                          text=f"有声书 {item.get('Name')} 需要整理，共 {len(book_items)} 集")
                else:
                    # 不需要整理的锁定
                    other_book_info = self.__get_item_info(item.get("Id"))
                    other_book_info.update({
                        "LockData": True,
                    })
                    self.__update_item_info(item.get("Id"), other_book_info)
                    logger.info(f"有声书 {item.get('Name')} 不需要整理，已锁定")

            logger.info(f"{emby_name} 有声书整理服务执行完毕")

    @eventmanager.register(EventType.PluginAction)
    def audiobook(self, event: Event = None):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "audiobook_artist":
                return
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            if not self._library_id:
                logger.error("请设置有声书文件夹ID！")
                self.post_message(channel=event.event_data.get("channel"),
                                  mtype=mtype,
                                  title="请设置有声书文件夹ID！",
                                  userid=event.event_data.get("user"))
                return

            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            args_list = args.split(" ")
            if len(args_list) != 3:
                logger.error(f"参数错误：{args_list}")
                self.post_message(channel=event.event_data.get("channel"),
                                  mtype=mtype,
                                  title=f"参数错误！ /aba 媒体库 书名 正确的演播作者名称",
                                  userid=event.event_data.get("user"))
                return

            library_name = args_list[0]
            book_name = args_list[1]
            book_art = args_list[2]
            logger.info(f"有声书整理：{library_name}:{book_name} - 正确演播作者 {book_art}")

            emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
            if not emby_servers:
                logger.error("未配置Emby媒体服务器")
                return

            for emby_name, emby_server in emby_servers.items():
                if str(library_name).lower() != str(emby_name).lower():
                    continue
                logger.info(f"开始处理媒体服务器 {emby_name}")
                self._EMBY_USER = emby_server.instance.get_user()
                self._EMBY_APIKEY = emby_server.config.config.get("apikey")
                self._EMBY_HOST = emby_server.config.config.get("host")
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

                # 获取所有有声书
                items = self.__get_items(self._library_id)
                if not items:
                    logger.error(f"获取媒体库 {self._library_id} 有声书列表失败！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"获取 {self._library_id} 有声书失败！",
                                      userid=event.event_data.get("user"))
                    return
                # 获取指定有声书
                book_id = None
                book_info = None
                for item in items:
                    if book_name in item.get("Name"):
                        book_id = item.get("Id")
                        book_info = self.__get_item_info(book_id)
                        break

                if not book_id or not book_info:
                    logger.error(f"未找到 {book_name} 有声书！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"未找到 {book_name} 有声书！",
                                      userid=event.event_data.get("user"))
                    return

                art_id = None
                artists = self.__get_artists()
                if not artists:
                    logger.error("获取有声书作者列表失败！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"获取有声书作者列表失败！",
                                      userid=event.event_data.get("user"))
                    return

                for artist in artists:
                    if artist["Name"] == book_art:
                        art_id = artist["Id"]
                        break

                if not art_id:
                    logger.error(f"未找到 {book_art} 作者！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"未找到 {book_art} 作者！",
                                      userid=event.event_data.get("user"))
                    return

                # 原作者信息
                ori_art = book_info.get("ArtistItems", []) or book_info.get("Composers", []) or book_info.get(
                    "AlbumArtists", [])
                ori_art = [art for art in ori_art if art["Name"] != book_art]

                # 新作者信息
                book_info["Artists"] = [book_art]
                book_info["AlbumArtist"] = book_art
                book_info["ArtistItems"] = {'Id': art_id, 'Name': book_art}
                book_info["Composers"] = {'Id': art_id, 'Name': book_art}
                book_info["AlbumArtists"] = {'Id': art_id, 'Name': book_art}
                book_info["LockData"] = True
                update_flag = self.__update_item_info(item_id=book_info["Id"], data=book_info)
                if update_flag:
                    logger.info(f"更新 {book_name} 作者信息-> {book_art} 成功！")

                    items = self.__get_items(parent_id=book_id)
                    if not items:
                        logger.error(f"获取有声书 {book_name} 剧集失败！")
                        self.post_message(channel=event.event_data.get("channel"),
                                          mtype=mtype,
                                          title=f"获取有声书 {book_name} 剧集失败！",
                                          userid=event.event_data.get("user"))
                        return

                    for item in items:
                        item_info = self.__get_item_info(item["Id"])
                        if not item_info:
                            logger.error(f"获取有声书 {book_name} 剧集 {item['Id']} 详情失败！")
                            continue
                        item_info["Artists"] = [book_art]
                        item_info["AlbumArtist"] = book_art
                        item_info["ArtistItems"] = {'Id': art_id, 'Name': book_art}
                        item_info["Composers"] = {'Id': art_id, 'Name': book_art}
                        item_info["AlbumArtists"] = {'Id': art_id, 'Name': book_art}
                        item_info["LockData"] = True
                        update_flag = self.__update_item_info(item_id=item_info["Id"], data=item_info)
                        if update_flag:
                            logger.info(f"更新 {book_name} 剧集 {item['Name']} 作者信息-> {book_art} 成功！")
                        else:
                            logger.error(f"更新 {book_name} 剧集 {item['Name']} 作者信息-> {book_art} 失败！")

                    if ori_art:
                        for art in ori_art:
                            flag = self.__delete_by_id(art["Id"])
                            logger.info(f"删除 {book_name} 原作者信息-> {art['Name']} {'成功' if flag else '失败'}！")

                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"更新 {book_name} 作者信息-> {book_art} 成功！",
                                      userid=event.event_data.get("user"))
                    return
                else:
                    logger.error(f"更新 {book_name} 作者信息-> {book_art} 失败！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"更新 {book_name} 作者信息-> {book_art} 失败！",
                                      userid=event.event_data.get("user"))
                    return

    @eventmanager.register(EventType.PluginAction)
    def audiobook(self, event: Event = None):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "audiobook":
                return
            mtype = NotificationType.Manual
            if self._msgtype:
                mtype = NotificationType.__getitem__(str(self._msgtype)) or NotificationType.Manual
            if not self._library_id:
                logger.error("请设置有声书文件夹ID！")
                self.post_message(channel=event.event_data.get("channel"),
                                  mtype=mtype,
                                  title="请设置有声书文件夹ID！",
                                  userid=event.event_data.get("user"))
                return

            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            args_list = args.split(" ")
            if len(args_list) != 3:
                logger.error(f"参数错误：{args_list}")
                self.post_message(channel=event.event_data.get("channel"),
                                  mtype=mtype,
                                  title=f"参数错误！ /ab 媒体库 书名 正确信息集数",
                                  userid=event.event_data.get("user"))
                return

            library_name = args_list[0]
            book_name = args_list[1]
            book_idx = args_list[2]
            logger.info(f"有声书整理：{library_name}:{book_name} - 正确信息从集数 {book_idx} 获取")

            emby_servers = self.mediaserver_helper.get_services(name_filters=self._mediaservers, type_filter="emby")
            if not emby_servers:
                logger.error("未配置Emby媒体服务器")
                return

            for emby_name, emby_server in emby_servers.items():
                if str(library_name).lower() != str(emby_name).lower():
                    continue
                logger.info(f"开始处理媒体服务器 {emby_name}")
                self._EMBY_USER = emby_server.instance.get_user()
                self._EMBY_APIKEY = emby_server.config.config.get("apikey")
                self._EMBY_HOST = emby_server.config.config.get("host")
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

                # 获取所有有声书
                items = self.__get_items(self._library_id)
                if not items:
                    logger.error(f"获取媒体库 {self._library_id} 有声书列表失败！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"获取 {self._library_id} 有声书失败！",
                                      userid=event.event_data.get("user"))
                    return

                # 获取指定有声书
                book_id = None
                book_info = None
                for item in items:
                    if book_name in item.get("Name"):
                        book_id = item.get("Id")
                        book_info = self.__get_item_info(book_id)
                        break

                if not book_id:
                    logger.error(f"未找到 {book_name} 有声书！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"未找到 {book_name} 有声书！",
                                      userid=event.event_data.get("user"))
                    return

                items = self.__get_items(book_id)
                if not items:
                    logger.error(f"获取 {book_name} {book_id} 有声书失败！")
                    self.post_message(channel=event.event_data.get("channel"),
                                      mtype=mtype,
                                      title=f"获取 {book_name} {book_id} 有声书失败！",
                                      userid=event.event_data.get("user"))
                    return

                self.__zl(items, int(book_idx))
                if book_info:
                    book_info.update({
                        "LockData": True,
                    })
                    self.__update_item_info(book_id, book_info)
                self.post_message(channel=event.event_data.get("channel"),
                                  mtype=mtype,
                                  title=f"{book_name} 有声书整理完成！",
                                  userid=event.event_data.get("user"))

    def __zl(self, items, book_idx):
        """
        有声书整理
        """
        AlbumId = None
        Album = None
        AlbumPrimaryImageTag = None
        Artists = None
        ArtistItems = None
        Composers = None
        AlbumArtist = None
        AlbumArtists = None
        ParentIndexNumber = None

        if book_idx == -1:
            for item in items:
                AlbumId = item.get("AlbumId")
                if not AlbumId:
                    continue

                Album = item.get("Album")
                AlbumPrimaryImageTag = item.get("AlbumPrimaryImageTag")
                Artists = item.get("Artists")
                ArtistItems = item.get("ArtistItems")
                Composers = item.get("Composers")
                AlbumArtist = item.get("AlbumArtist")
                AlbumArtists = item.get("AlbumArtists")
                ParentIndexNumber = item.get("ParentIndexNumber")
                if AlbumId and Album and Artists and AlbumArtist and AlbumArtists and ParentIndexNumber:
                    logger.info(
                        f"从集数 {item.get('IndexNumber')} 获取到有声书信息：{Album} - {Artists} - {Composers} - {AlbumArtist} - {AlbumArtists} - {ParentIndexNumber}")
                    break
        else:
            Album = items[book_idx - 1].get("Album")
            AlbumId = items[book_idx - 1].get("AlbumId")
            AlbumPrimaryImageTag = items[book_idx - 1].get("AlbumPrimaryImageTag")
            Artists = items[book_idx - 1].get("Artists")
            ArtistItems = items[book_idx - 1].get("ArtistItems")
            Composers = items[book_idx - 1].get("Composers")
            AlbumArtist = items[book_idx - 1].get("AlbumArtist")
            AlbumArtists = items[book_idx - 1].get("AlbumArtists")
            ParentIndexNumber = items[book_idx - 1].get("ParentIndexNumber")
            logger.info(
                f"从集数 {book_idx} 获取到有声书信息：{Album} - {Artists} - {Composers} - {AlbumArtist} - {AlbumArtists} - {ParentIndexNumber}")

        # 更新有声书信息
        for i, item in enumerate(items):
            episode = i + 1
            # 使用正则表达式匹配集数
            match = re.search(r'第(\d+)集', item.get("Name"))
            if match:
                episode = int(match.group(1))
            else:
                # 使用正则表达式匹配数字
                match = re.search(r'\d+', item.get("Name"))
                if match:
                    # 提取数字
                    episode = match.group()

            if Album == item.get("Album") and \
                    AlbumId == item.get("AlbumId") and \
                    AlbumPrimaryImageTag == item.get("AlbumPrimaryImageTag") and \
                    Artists == item.get("Artists") and \
                    ArtistItems == item.get("ArtistItems") and \
                    Composers == item.get("Composers") and \
                    AlbumArtist == item.get("AlbumArtist") and \
                    AlbumArtists == item.get("AlbumArtists") and not self._rename:
                logger.info(f"有声书 第{episode}集 {item.get('Name')} 信息完整，跳过！")
                continue

            retry = 0
            while retry < 3:
                try:
                    # 获取有声书信息
                    item_info = self.__get_item_info(item.get("Id"))

                    # 重命名前判断名称是否一致
                    if self._rename and item.get("Name") == Path(Path(item_info.get("Path")).name).stem:
                        logger.info(f"有声书 第{episode}集 {item.get('Name')} 名称相同，跳过！")
                        continue

                    item_info.update({
                        "Album": Album,
                        "AlbumId": AlbumId,
                        "AlbumPrimaryImageTag": AlbumPrimaryImageTag,
                        "Artists": Artists,
                        "ArtistItems": ArtistItems,
                        "Composers": Composers,
                        "AlbumArtist": AlbumArtist,
                        "AlbumArtists": AlbumArtists,
                        "ParentIndexNumber": ParentIndexNumber,
                        "IndexNumber": episode,
                        "LockData": True,
                    })
                    retry = 3
                except Exception as e:
                    retry += 1
                    logger.error(f"更新有声书 第{episode}集 {item.get('Name')} 信息出错：{e} 开始重试...{retry} / 3")
                    continue

            if item_info.get("Name") == "filename" or self._rename:
                item_info.update({
                    "Name": Path(Path(item_info.get("Path")).name).stem
                })
            flag = self.__update_item_info(item.get("Id"), item_info)
            logger.info(f"{Album} 第{episode}集 {item_info.get('Name')} 更新{'成功' if flag else '失败'}")
            time.sleep(0.5)

    def get_state(self) -> bool:
        return self._enabled

    def __delete_by_id(self, item_id):
        res = RequestUtils().post(
            f"{self._EMBY_HOST}/emby/Items/Delete?Ids={item_id}&api_key={self._EMBY_APIKEY}")
        if res and res.status_code == 204:
            return True
        return False

    def __get_artists(self) -> dict:
        """
        获取作者列表
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return {}
        req_url = f"%semby/Artists?api_key=%s" % (
            self._EMBY_HOST, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.json().get("Items", [])
            else:
                logger.info(f"获取有声书作者列表失败，无法连接Emby！")
                return {}

    def __get_items(self, parent_id) -> list:
        """
        获取有声书剧集
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"%semby/Users/%s/Items?ParentId=%s&api_key=%s" % (
            self._EMBY_HOST, self._EMBY_USER, parent_id, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.json().get("Items")
                else:
                    logger.info(f"获取有声书剧集失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接有声书Items出错：" + str(e))
            return []

    def __get_item_info(self, item_id) -> dict:
        """
        获取有声书剧集详情
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return {}
        req_url = f"%semby/Users/%s/Items/%s?fields=ShareLevel&ExcludeFields=Chapters,Overview,People,MediaStreams,Subviews&api_key=%s" % (
            self._EMBY_HOST, self._EMBY_USER, item_id, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.json()
            else:
                logger.info(f"获取有声书剧集详情失败，无法连接Emby！")
                return {}

    def __update_item_info(self, item_id, data):
        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json'
        }
        res = RequestUtils(headers=headers).post(
            f"{self._EMBY_HOST}/emby/Items/{item_id}?api_key={self._EMBY_APIKEY}",
            data=json.dumps(data))
        if res and res.status_code == 204:
            return True
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/ab",
                "event": EventType.PluginAction,
                "desc": "emby有声书整理",
                "category": "",
                "data": {
                    "action": "audiobook"
                }
            },
            {
                "cmd": "/aba",
                "event": EventType.PluginAction,
                "desc": "emby有声书演播者整理",
                "category": "",
                "data": {
                    "action": "audiobook_artist"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
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
                                            'model': 'notify',
                                            'label': '开启通知',
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
                                            'model': 'rename',
                                            'label': '重命名有声书',
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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '定时全量同步周期',
                                            'placeholder': '5位cron表达式，留空关闭'
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
                                            'model': 'msgtype',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions
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
                                            'model': 'library_id',
                                            'label': '有声书文件夹ID',
                                            'placeholder': '媒体库有声书-->文件夹-->看URL里的ParentId'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values() if
                                                      config.type == "emby"]
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
                                            'text': '仅支持交互命令运行: /ab 书名 正确信息集数。'
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
            "rename": False,
            "cron": "",
            "msgtype": "",
            "library_id": "",
            "mediaservers": [],
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
