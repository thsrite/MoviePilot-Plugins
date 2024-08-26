import json
import re
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.modules.emby import Emby
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class EmbyDanmu(_PluginBase):
    # 插件名称
    plugin_name = "Emby弹幕下载"
    # 插件描述
    plugin_desc = "通知Emby Danmu插件下载弹幕。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/danmu.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embydanmu_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _library_task = {}

    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_USER = Emby().get_user()
    _EMBY_APIKEY = settings.EMBY_API_KEY

    def init_plugin(self, config: dict = None):
        self._library_task = {}
        # 读取配置
        if config:
            self._enabled = config.get("enabled")

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

    @eventmanager.register(EventType.PluginAction)
    def audiobook(self, event: Event = None):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "embydanmu":
                return

            args = event_data.get("args")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return

            args_list = args.split(" ")
            if len(args_list) != 2 and len(args_list) != 3:
                logger.error(f"参数错误：{args_list}")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"参数错误！ /danmu 媒体库名 媒体名 (季)",
                                  userid=event.event_data.get("user"))
                return

            # 检查插件是否正确配置
            danmu_source = self.__get_danmu_source()
            if not danmu_source:
                logger.error(f"未配置弹幕源")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"Emby未正确配置弹幕源",
                                  userid=event.event_data.get("user"))
                return

            library_name = args_list[0]
            library_item_name = args_list[1]
            library_item_season = None
            if len(args_list) == 3:
                library_item_season = int(args_list[2])
            logger.info(
                f"开始下载字幕文件：{library_name} - {library_item_name} {f'(季{library_item_season})' if library_item_season else ''}")

            # 获取媒体库信息
            librarys = self.__get_librarys()

            library_id = None
            library_options = None
            library_type = None
            # 匹配需要的媒体库
            for library in librarys:
                if library.get("Name") == library_name:
                    logger.info(f"找到媒体库：{library_name}，ID：{library.get('Id')}")
                    library_type = library.get("CollectionType")
                    library_id = library.get("Id")
                    library_options = library.get("LibraryOptions")
                    break

            if not library_id or not library_options:
                logger.error(f"未找到媒体库：{library_name}")
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"未找到媒体库：{library_name}",
                                  userid=event.event_data.get("user"))
                return

            # 开启Danmu插件
            # 检查是否已经禁用了Danmu插件，如禁用则先启用
            enabled_danmu = False
            library_disabled_subtitle_fetchers = library_options.get("DisabledSubtitleFetchers", [])
            if "Danmu" in library_disabled_subtitle_fetchers:
                library_disabled_subtitle_fetchers.remove("Danmu")
                library_options.update({
                    "DisabledSubtitleFetchers": library_disabled_subtitle_fetchers,
                })
                enabled_danmu = True

            # 启用Danmu插件
            if enabled_danmu:
                update_flag = self.__update_library(library_id, library_options)
                if update_flag:
                    logger.info(f"已启用媒体库：{library_name}的Danmu插件")
                else:
                    logger.error(f"启用媒体库：{library_name}的Danmu插件失败")
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"启用媒体库：{library_name}的Danmu插件失败",
                                      userid=event.event_data.get("user"))
                    return
            else:
                logger.info(f"媒体库：{library_name}的Danmu插件已启用")

            # 媒体库设置为正在任务，不关闭弹幕插件
            _library_task = self._library_task.get(library_id, [])
            _library_task.append(library_item_name)
            self._library_task[library_id] = _library_task

            try:
                # 获取媒体库媒体列表
                library_items = self.__get_items(library_id)
                if not library_items:
                    logger.error(f"获取媒体库：{library_name}的媒体列表失败")
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"获取媒体库：{library_name}的媒体列表失败",
                                      userid=event.event_data.get("user"))
                else:
                    self.post_message(channel=event.event_data.get("channel"),
                                      title=f"开始通知Emby下载弹幕，异步执行，请耐心等候执行完成消息",
                                      userid=event.event_data.get("user"))

                    # 遍历媒体列表，获取媒体的ID和名称
                    for item in library_items:
                        if library_type == "tvshows":
                            if item.get("Name") == library_item_name:
                                logger.info(f"找到媒体：{library_item_name}，ID：{item.get('Id')}")

                                # 电视剧弹幕
                                seasons = self.__get_items(item.get("Id"))
                                if len(seasons) == 1:
                                    season_item = seasons[0]
                                    # 通知Danmu插件获取弹幕
                                    season_id = season_item.get("Id")
                                    danmu_flag = self.__download_danmu(season_id)
                                    if danmu_flag:
                                        logger.info(
                                            f"已通知弹幕插件获取 {library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 的弹幕")
                                        season_item_cnt, danmu_cnt = self.__check_danmu_exists(season_id)
                                        if season_item_cnt == 0:
                                            logger.error(
                                                f"{library_name} {library_item_name} Emby已配置弹幕源全部匹配弹幕失败")
                                            self.post_message(channel=event.event_data.get("channel"),
                                                              title=f"{library_name} {library_item_name} Emby已配置弹幕源全部匹配弹幕失败",
                                                              userid=event.event_data.get("user"))
                                        else:
                                            if season_item_cnt == danmu_cnt:
                                                logger.info(
                                                    f"{library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}")
                                                self.post_message(channel=event.event_data.get("channel"),
                                                                  title=f"{library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}",
                                                                  userid=event.event_data.get("user"))
                                            else:
                                                logger.error(
                                                    f"{library_name} {library_item_name} 字幕文件未全部下载完成：{season_item_cnt}/{danmu_cnt}")
                                                self.post_message(channel=event.event_data.get("channel"),
                                                                  title=f"{library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 字幕文件未全部下载完成：{season_item_cnt}/{danmu_cnt}",
                                                                  userid=event.event_data.get("user"))
                                    else:
                                        logger.error(
                                            f"通知弹幕插件获取 {library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 的弹幕失败")
                                        self.post_message(channel=event.event_data.get("channel"),
                                                          title=f"通知弹幕插件获取 {library_name} {library_item_name} 第{season_item.get('IndexNumber')}季 的弹幕失败",
                                                          userid=event.event_data.get("user"))
                                else:
                                    for season in seasons:
                                        # 指定季度则只获取指定季度的弹幕
                                        if library_item_season:
                                            if season.get("Id") == library_item_season:
                                                season_id = season.get("Id")
                                                # 通知Danmu插件获取弹幕
                                                danmu_flag = self.__download_danmu(season_id)
                                                if danmu_flag:
                                                    logger.info(
                                                        f"已通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕")
                                                    season_item_cnt, danmu_cnt = self.__check_danmu_exists(season_id)
                                                    if season_item_cnt == 0:
                                                        logger.error(
                                                            f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 Emby已配置弹幕源全部匹配弹幕失败")
                                                        self.post_message(channel=event.event_data.get("channel"),
                                                                          title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 Emby已配置弹幕源全部匹配弹幕失败",
                                                                          userid=event.event_data.get("user"))
                                                    else:
                                                        if season_item_cnt == danmu_cnt:
                                                            logger.info(
                                                                f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}")
                                                            self.post_message(channel=event.event_data.get("channel"),
                                                                              title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}",
                                                                              userid=event.event_data.get("user"))
                                                        else:
                                                            logger.error(
                                                                f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件未全部下载完成：{season_item_cnt}/{danmu_cnt}")
                                                            self.post_message(channel=event.event_data.get("channel"),
                                                                              title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件未全部下载完成：{season_item_cnt}/{danmu_cnt}",
                                                                              userid=event.event_data.get("user"))
                                                else:
                                                    logger.error(
                                                        f"通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕失败")
                                                    self.post_message(channel=event.event_data.get("channel"),
                                                                      title=f"通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕失败",
                                                                      userid=event.event_data.get("user"))
                                        else:
                                            # 未指定季度则获取全部季度的弹幕
                                            season_id = season.get("Id")
                                            # 通知Danmu插件获取弹幕
                                            danmu_flag = self.__download_danmu(season_id)
                                            if danmu_flag:
                                                logger.info(
                                                    f"已通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕")
                                                season_item_cnt, danmu_cnt = self.__check_danmu_exists(season_id)
                                                if season_item_cnt == 0:
                                                    logger.error(
                                                        f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 Emby已配置弹幕源全部匹配弹幕失败")
                                                    self.post_message(channel=event.event_data.get("channel"),
                                                                      title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 Emby已配置弹幕源全部匹配弹幕失败",
                                                                      userid=event.event_data.get("user"))
                                                else:
                                                    if season_item_cnt == danmu_cnt:
                                                        logger.info(
                                                            f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}")
                                                        self.post_message(channel=event.event_data.get("channel"),
                                                                          title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件已全部下载完成：{danmu_cnt}",
                                                                          userid=event.event_data.get("user"))
                                                    else:
                                                        logger.error(
                                                            f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件未全部下载完成：{danmu_cnt}/{season_item_cnt}")
                                                        self.post_message(channel=event.event_data.get("channel"),
                                                                          title=f"{library_name} {library_item_name} 第{season.get('IndexNumber')}季 字幕文件未全部下载完成：{danmu_cnt}/{season_item_cnt}",
                                                                          userid=event.event_data.get("user"))
                                            else:
                                                logger.error(
                                                    f"通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕失败")
                                                self.post_message(channel=event.event_data.get("channel"),
                                                                  title=f"通知弹幕插件获取 {library_name} {library_item_name} 第{season.get('IndexNumber')}季 的弹幕失败",
                                                                  userid=event.event_data.get("user"))
                        else:
                            # 电影弹幕
                            if item.get("Name").split(" ")[0] == library_item_name:
                                movie_id = item.get("Id")
                                movie_items = self.__get_items(movie_id)
                                if not movie_items:
                                    logger.error(f"获取 {library_name} {item.get('Name')}的媒体列表失败")
                                    self.post_message(channel=event.event_data.get("channel"),
                                                      title=f"获取电影：{library_name} {item.get('Name')}的媒体列表失败",
                                                      userid=event.event_data.get("user"))
                                else:
                                    movie_id = movie_items[0].get("Id")
                                    # 通知Danmu插件获取弹幕
                                    danmu_flag = self.__download_danmu(movie_id)
                                    if danmu_flag:
                                        logger.info(
                                            f"已通知弹幕插件获取 {library_name} {item.get('Name')} {movie_id} 的弹幕")
                                        # 获取媒体详情
                                        item_info = self.__get_item_info(movie_id)
                                        item_path = item_info.get("Path")
                                        parent_path = Path(item_path).parent
                                        logger.info(f"开始检查路径 {parent_path} 下是是否有字幕文件")
                                        # 检查是否有字幕文件
                                        danmu_path_pattern = Path(item_path).stem + "*.xml"
                                        retry_cnt = 3
                                        while len(list(parent_path.glob(danmu_path_pattern))) == 0 and retry_cnt > 0:
                                            # 解析日志判断是否全部失败
                                            if self.__check_all_failed_by_log(item_name=item_info.get("Name"),
                                                                              item_year=item_info.get(
                                                                                  "ProductionYear")):
                                                logger.error(f"解析日志判断已配置弹幕源全部匹配弹幕失败")
                                                retry_cnt = -1
                                            else:
                                                retry_cnt -= 1
                                                logger.warn(
                                                    f"{parent_path} 下未找到字幕文件：{danmu_path_pattern}，等待60秒后重试 ({retry_cnt}次)")
                                                time.sleep(60)

                                        if len(list(parent_path.glob(danmu_path_pattern))) >= 1:
                                            logger.info(f"{parent_path} 下已找到字幕文件：{danmu_path_pattern}")
                                            self.post_message(channel=event.event_data.get("channel"),
                                                              title=f"{library_name} {item.get('Name')} 下载字幕文件成功",
                                                              userid=event.event_data.get("user"))
                                        else:
                                            logger.error(f"{parent_path} 下未找到字幕文件：{danmu_path_pattern}")
                                            self.post_message(channel=event.event_data.get("channel"),
                                                              title=f"{library_name} {item.get('Name')} 已配置弹幕源全部匹配弹幕失败",
                                                              userid=event.event_data.get("user"))
                                    else:
                                        logger.error(
                                            f"通知弹幕插件获取 {library_name} {item.get('Name')} {movie_id} 的弹幕失败")
                                        self.post_message(channel=event.event_data.get("channel"),
                                                          title=f"通知弹幕插件获取 {library_name} 电影 {item.get('Name')} {movie_id} 的弹幕失败",
                                                          userid=event.event_data.get("user"))
            except Exception as e:
                logger.error(
                    f"{library_name} {library_item_name} {f'第{library_item_season}季 ' if library_item_season else ''} 获取弹幕任务出错：{str(e)}")

            # 判断当前媒体库是否有其他任务在执行
            self._library_task[library_id].remove(library_item_name)
            if len(self._library_task[library_id]) == 0:
                # 关闭弹幕插件
                logger.info(
                    f"{library_name} {library_item_name} {f'第{library_item_season}季 ' if library_item_season else ''} 获取弹幕任务完成，关闭弹幕插件")
                # 禁用媒体库的Danmu插件
                library_disabled_subtitle_fetchers = library_options.get("DisabledSubtitleFetchers", [])
                library_disabled_subtitle_fetchers.append("Danmu")
                library_options.update({
                    "DisabledSubtitleFetchers": library_disabled_subtitle_fetchers,
                })
                update_flag = self.__update_library(library_id, library_options)
                if update_flag:
                    logger.info(f"已禁用媒体库：{library_name} Danmu插件")
                else:
                    logger.error(f"禁用媒体库：{library_name} Danmu插件失败")

    def get_state(self) -> bool:
        return self._enabled

    def __get_librarys(self) -> list:
        """
        获取媒体库信息
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"%semby/Library/VirtualFolders/Query?api_key=%s" % (
            self._EMBY_HOST, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.json().get("Items")
                else:
                    logger.info(f"获取媒体库失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接媒体库emby/Library/VirtualFolders/Query出错：" + str(e))
            return []

    def __update_library(self, library_id, library_options) -> bool:
        """
        获取媒体库信息
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        headers = {
            'accept': '*/*',
            'Content-Type': 'application/json'
        }
        req_url = f"%semby/Library/VirtualFolders/LibraryOptions?api_key=%s" % (
            self._EMBY_HOST, self._EMBY_APIKEY)
        res = RequestUtils(headers=headers).post(url=req_url,
                                                 data=json.dumps({"Id": library_id, "LibraryOptions": library_options}))
        if res and res.status_code == 204:
            return True
        return False

    def __get_items(self, parent_id) -> list:
        """
        获取媒体库媒体列表
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
                    logger.info(f"获取媒体库媒体列表失败，无法连接Emby！")
                    return []
        except Exception as e:
            logger.error(f"连接媒体库媒体列表Items出错：" + str(e))
            return []

    def __download_danmu(self, item_id) -> bool:
        """
        通知Danmu插件获取弹幕
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        req_url = f"%sapi/danmu/%s?option=Refresh&api_key=%s" % (
            self._EMBY_HOST, item_id, self._EMBY_APIKEY)
        try:
            with RequestUtils().get_res(req_url) as res:
                if res:
                    return res.text == "ok"
                else:
                    logger.info(f"通知Danmu插件获取弹幕失败，无法连接Emby！")
                    return False
        except Exception as e:
            logger.error(f"通知Danmu插件获取弹幕api/danmu/{item_id}?option=Refresh出错：" + str(e))
            return False

    def __get_item_info(self, item_id) -> dict:
        """
        获取媒体详情
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return {}
        req_url = f"%semby/Users/%s/Items/%s?fields=ShareLevel&ExcludeFields=Chapters,Overview,People,MediaStreams,Subviews&api_key=%s" % (
            self._EMBY_HOST, self._EMBY_USER, item_id, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.json()
            else:
                logger.info(f"获取媒体详情失败，无法连接Emby！")
                return {}

    def __check_danmu_exists(self, season_id):
        """
        检查媒体是否有弹幕
        """
        season_items = self.__get_items(season_id)
        item_info = self.__get_item_info(season_items[0].get("Id"))
        item_path = item_info.get("Path")
        parent_path = Path(item_path).parent
        logger.info(f"开始检查路径 {parent_path} 下是是否有字幕文件")
        # 检查是否有字幕文件
        danmu_path_pattern = "*.xml"

        retry_cnt = len(season_items)
        _downloaded_danmu_files = []
        while len(_downloaded_danmu_files) < len(season_items) and retry_cnt > 0:
            # 解析日志判断是否全部失败
            if self.__check_all_failed_by_log(item_name=item_info.get("SeriesName"),
                                              item_year=item_info.get("ProductionYear")):
                logger.error(f"解析日志判断已配置弹幕源全部匹配弹幕失败")
                retry_cnt = -1
            else:
                danmu_files = list(parent_path.glob(danmu_path_pattern))
                for danmu_file in danmu_files:
                    if danmu_file.name not in _downloaded_danmu_files:
                        _downloaded_danmu_files.append(danmu_file.name)
                        logger.info(f"已下载字幕文件：{danmu_file.name}")
                # 判断是否完成任务
                if len(_downloaded_danmu_files) != len(season_items):
                    retry_cnt -= 1
                    logger.warn(
                        f"{parent_path} 下字幕文件：{danmu_path_pattern} 未下载完成，等待60秒后重试 ({retry_cnt}次)")
                    time.sleep(60)

        return len(_downloaded_danmu_files), len(season_items)

    def __get_plugins(self) -> list:
        """
        获取插件列表
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return []
        req_url = f"%semby/web/configurationpages?PageType=PluginConfiguration&EnableInMainMenu=true&UserId=%s&api_key=%s" % (
            self._EMBY_HOST, self._EMBY_USER, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.json()
            else:
                logger.info(f"获取插件列表失败，无法连接Emby！")
                return []

    def __get_plugin_info(self, plugin_id) -> dict:
        """
        获取插件详情
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return {}
        req_url = f"%semby/Plugins/%s/Configuration?api_key=%s" % (
            self._EMBY_HOST, plugin_id, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.json()
            else:
                logger.info(f"获取插件详情失败，无法连接Emby！")
                return {}

    def __get_danmu_source(self) -> list:
        """
        获取弹幕源
        """
        # 获取插件列表
        list_plugins = self.__get_plugins()
        if not list_plugins:
            return []

        # 获取弹幕配置插件
        plugin_id = None
        for plugin in list_plugins:
            if plugin.get("Name") == "danmu":
                plugin_id = plugin.get("PluginId")
                break

        if not plugin_id:
            logger.error("弹幕配置插件未安装")
            return []

        # 获取弹幕源
        plugin_info = self.__get_plugin_info(plugin_id)
        if not plugin_info:
            return []

        scrapers = plugin_info.get("Scrapers", [])
        if not scrapers:
            return []

        return [scraper.get("Name") for scraper in scrapers if scraper.get("Enable") == True]

    def __get_emby_log(self) -> str:
        """
        获取emby日志
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return ""
        req_url = f"%sSystem/Logs/embyserver.txt?api_key=%s" % (
            self._EMBY_HOST, self._EMBY_APIKEY)
        with RequestUtils().get_res(req_url) as res:
            if res:
                return res.text
            else:
                logger.info(f"获取插件详情失败，无法连接Emby！")
                return ""

    def __check_all_failed_by_log(self, item_name, item_year) -> bool:
        """
        解析emby日志
        """
        danmu_source = self.__get_danmu_source()
        emby_log = self.__get_emby_log()
        if not emby_log:
            return False

        # 正则解析删除的媒体信息
        all_matched = True
        for source in danmu_source:
            pattern = fr'\[{source}\]匹配失败：{item_name} \({item_year}\)'
            matches = re.findall(pattern, emby_log)
            if not matches:
                all_matched = False
                break
        return all_matched

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/danmu",
                "event": EventType.PluginAction,
                "desc": "emby弹幕下载",
                "category": "",
                "data": {
                    "action": "embydanmu"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
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
                                            'text': '仅支持交互命令运行: /danmu 媒体库名 媒体名 (季)。 季可选，不填则获取全部季度。'
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
                                            'text': '需Emby安装Danmu插件，并启用弹幕功能（https://github.com/fengymi/emby-plugin-danmu）。'
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
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        pass
