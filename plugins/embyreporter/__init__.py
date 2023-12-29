import os

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas import NotificationType
from pathlib import Path

import random
from io import BytesIO
from PIL import Image
from PIL import ImageFont
from PIL import ImageDraw
import pytz
from cacheout import Cache
from datetime import datetime, timedelta

from app.utils.http import RequestUtils

cache = Cache()


class EmbyReporter(_PluginBase):
    # 插件名称
    plugin_name = "Emby观影报告"
    # 插件描述
    plugin_desc = "推送Emby观影报告，需Emby安装Playback Report 插件。"
    # 插件图标
    plugin_icon = "Pydiocells_A.png"
    # 插件版本
    plugin_version = "1.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyreporter_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled: bool = False
    _onlyonce: bool = False
    _res_dir = None
    _cron = None
    _days = None
    _type = None
    _mp_host = None
    _scheduler: Optional[BackgroundScheduler] = None

    PLAYBACK_REPORTING_TYPE_MOVIE = "ItemName"
    PLAYBACK_REPORTING_TYPE_TVSHOWS = "substr(ItemName,0, instr(ItemName, ' - '))"
    host = None

    def init_plugin(self, config: dict = None):
        self.host = "http://" + settings.EMBY_HOST
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._res_dir = config.get("res_dir")
            self._days = config.get("days") or 7
            self._type = config.get("type") or "tg"
            self._mp_host = config.get("mp_host")

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"Emby观影报告服务启动，立即运行一次")
                    self._scheduler.add_job(self.__report, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="Emby观影报告")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.__report,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="Emby观影报告")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def __report(self):
        """
        发送Emby观影报告
        """
        # 本地路径转为url
        if not self._mp_host:
            return

        if not self._type:
            return

        # 获取数据
        success, movies = self.get_report(types=self.PLAYBACK_REPORTING_TYPE_MOVIE, days=int(self._days), limit=10)
        if not success:
            exit(movies)
        logger.info(f"获取到电影 {movies}")
        success, tvshows = self.get_report(types=self.PLAYBACK_REPORTING_TYPE_TVSHOWS, days=int(self._days), limit=10)
        if not success:
            exit(tvshows)
        logger.info(f"获取到电视剧 {tvshows}")

        # 绘制海报
        report_path = self.draw(self._res_dir, movies, tvshows)

        if not report_path:
            logger.error("生成海报失败")
            return

        # 发送海报
        report_text = f"🌟*过去{self._days}日观影排行*\r\n\r\n"

        report_url = self._mp_host + report_path.replace("/public", "")
        mtype = NotificationType.MediaServer
        if self._type:
            mtype = NotificationType.__getitem__(str(self._type)) or NotificationType.MediaServer
        self.post_message(title=report_text,
                          mtype=mtype,
                          image=report_url)
        logger.info(f"Emby观影记录推送成功 {report_url}")

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "days": self._days,
            "type": self._type,
            "mp_host": self._mp_host,
            "res_dir": self._res_dir
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
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        # 编历 NotificationType 枚举，生成消息类型选项
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
                                                   'model': 'res_dir',
                                                   'label': '素材路径',
                                                   'placeholder': '本地素材路径，不传用默认'
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
                                           'md': 6
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'days',
                                                   'label': '报告天数',
                                                   'placeholder': '向前获取数据的天数'
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
                                               'component': 'VSelect',
                                               'props': {
                                                   'multiple': False,
                                                   'chips': True,
                                                   'model': 'type',
                                                   'label': '推送方式',
                                                   'items': MsgTypeOptions
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
                                           'cols': 12
                                       },
                                       'content': [
                                           {
                                               'component': 'VTextField',
                                               'props': {
                                                   'model': 'mp_host',
                                                   'label': 'MoviePilot域名',
                                                   'placeholder': '必填，末尾不带/'
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
                   "onlyonce": False,
                   "cron": "5 1 * * *",
                   "res_dir": "",
                   "days": 7,
                   "mp_host": "",
                   "type": ""
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

    def draw(self, res_path, movies, tvshows, show_count=True):
        # 默认路径 默认图
        if not res_path:
            res_path = os.path.join(Path(__file__).parent, "res")
        # 绘图文件路径初始化
        bg_path = os.path.join(res_path, "bg")
        mask_path = os.path.join(res_path, "cover-ranks-mask-2.png")
        font_path = os.path.join(res_path, "PingFang Bold.ttf")
        # 随机调取背景, 路径: res/ranks/bg/...
        bg_list = os.listdir(bg_path)
        bg_path = os.path.join(bg_path, bg_list[random.randint(0, len(bg_list) - 1)])
        # 初始绘图对象
        bg = Image.open(bg_path)
        mask = Image.open(mask_path)
        bg.paste(mask, (0, 0), mask)
        font = ImageFont.truetype(font_path, 18)
        font_small = ImageFont.truetype(font_path, 14)
        font_count = ImageFont.truetype(font_path, 12)

        exites_movies = []
        for i in movies:
            try:
                # 榜单项数据
                user_id, item_id, item_type, name, count, duarion = tuple(i)
                print(item_type, item_id, name, count)
                # 封面图像获取
                success, data = self.primary(item_id)
                if not success:
                    continue
                exites_movies.append(i)
            except Exception:
                continue

        logger.info(f"过滤后未删除电影 {len(exites_movies)} 部")
        # 合并绘制
        if len(exites_movies) < 5:
            for i in range(5 - len(exites_movies) + 1):
                exites_movies.append({"item_id": i})
        if len(exites_movies) > 5:
            exites_movies = exites_movies[:5]

        exites_tvs = []
        for i in tvshows:
            try:
                # 榜单项数据
                user_id, item_id, item_type, name, count, duarion = tuple(i)
                print(item_type, item_id, name, count)
                # 图片获取，剧集主封面获取
                # 获取剧ID
                success, data = self.items(user_id, item_id)
                if not success:
                    continue
                item_id = data["SeriesId"]
                # 封面图像获取
                success, data = self.primary(item_id)
                if not success:
                    continue
                exites_tvs.append(i)
            except Exception:
                continue
        logger.info(f"过滤后未删除电视剧 {len(exites_tvs)} 部")
        if len(exites_tvs) > 5:
            exites_tvs = exites_tvs[:5]

        all_ranks = exites_movies + exites_tvs
        index, offset_y = (-1, 0)
        for i in all_ranks:
            index += 1
            try:
                # 榜单项数据
                user_id, item_id, item_type, name, count, duarion = tuple(i)
                # 图片获取，剧集主封面获取
                if item_type != "Movie":
                    # 获取剧ID
                    success, data = self.items(user_id, item_id)
                    if not success:
                        index -= 1
                        continue
                    item_id = data["SeriesId"]
                # 封面图像获取
                success, data = self.primary(item_id)
                if not success:
                    if item_type != "Movie":
                        index -= 1
                    continue
                # 剧集Y偏移
                if index >= 5:
                    index = 0
                    offset_y = 331
                # 名称显示偏移
                font_offset_y = 0
                temp_font = font
                # 名称超出长度缩小省略
                if font.getlength(name) > 110:
                    temp_font = font_small
                    font_offset_y = 4
                    for i in range(len(name)):
                        name = name[:len(name) - 1]
                        if font.getlength(name) <= 110:
                            break
                    name += ".."
                # 绘制封面
                cover = Image.open(BytesIO(data))
                cover = cover.resize((108, 159))
                bg.paste(cover, (73 + 145 * index, 379 + offset_y))
                # 绘制 播放次数、影片名称
                text = ImageDraw.Draw(bg)
                if show_count:
                    self.draw_text_psd_style(text,
                                             (
                                                 177 + 145 * index - font_count.getlength(str(count)),
                                                 353 + offset_y),
                                             str(count), font_count, 126)
                self.draw_text_psd_style(text, (74 + 145 * index, 542 + font_offset_y + offset_y), name, temp_font, 126)
            except Exception:
                continue

        if index > 0:
            save_path = "/public/report.jpg"
            if Path(save_path).exists():
                Path.unlink(Path(save_path))
            bg.save(save_path)
            return save_path
        return None

    @staticmethod
    def draw_text_psd_style(draw, xy, text, font, tracking=0, leading=None, **kwargs):
        """
        usage: draw_text_psd_style(draw, (0, 0), "Test",
                    tracking=-0.1, leading=32, fill="Blue")

        Leading is measured from the baseline of one line of text to the
        baseline of the line above it. Baseline is the invisible line on which most
        letters—that is, those without descenders—sit. The default auto-leading
        option sets the leading at 120% of the type size (for example, 12‑point
        leading for 10‑point type).

        Tracking is measured in 1/1000 em, a unit of measure that is relative to
        the current type size. In a 6 point font, 1 em equals 6 points;
        in a 10 point font, 1 em equals 10 points. Tracking
        is strictly proportional to the current type size.
        """

        def stutter_chunk(lst, size, overlap=0, default=None):
            for i in range(0, len(lst), size - overlap):
                r = list(lst[i:i + size])
                while len(r) < size:
                    r.append(default)
                yield r

        x, y = xy
        font_size = font.size
        lines = text.splitlines()
        if leading is None:
            leading = font.size * 1.2
        for line in lines:
            for a, b in stutter_chunk(line, 2, 1, ' '):
                w = font.getlength(a + b) - font.getlength(b)
                draw.text((x, y), a, font=font, **kwargs)
                x += w + (tracking / 1000) * font_size
            y += leading
            x = xy[0]

    @cache.memoize(ttl=600)
    def primary(self, item_id, width=200, height=300, quality=90, ret_url=False):
        try:
            url = self.host + f"/emby/Items/{item_id}/Images/Primary?maxHeight={height}&maxWidth={width}&quality={quality}"
            if ret_url:
                return url
            resp = RequestUtils().get_res(url=url)

            if resp.status_code != 204 and resp.status_code != 200:
                return False, "🤕Emby 服务器连接失败!"
            return True, resp.content
        except Exception:
            return False, "🤕Emby 服务器连接失败!"

    @cache.memoize(ttl=600)
    def backdrop(self, item_id, width=1920, quality=70, ret_url=False):
        try:
            url = self.host + f"/emby/Items/{item_id}/Images/Backdrop/0?&maxWidth={width}&quality={quality}"
            if ret_url:
                return url
            resp = RequestUtils().get_res(url=url)

            if resp.status_code != 204 and resp.status_code != 200:
                return False, "🤕Emby 服务器连接失败!"
            return True, resp.content
        except Exception:
            return False, "🤕Emby 服务器连接失败!"

    @cache.memoize(ttl=600)
    def logo(self, item_id, quality=70, ret_url=False):
        url = self.host + f"/emby/Items/{item_id}/Images/Logo?quality={quality}"
        if ret_url:
            return url
        resp = RequestUtils().get_res(url=url)

        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        return True, resp.content

    @cache.memoize(ttl=300)
    def items(self, user_id, item_id):
        try:
            url = f"{self.host}/emby/Users/{user_id}/Items/{item_id}?api_key={settings.EMBY_API_KEY}"
            resp = RequestUtils().get_res(url=url)

            if resp.status_code != 204 and resp.status_code != 200:
                return False, "🤕Emby 服务器连接失败!"
            return True, resp.json()
        except Exception:
            return False, "🤕Emby 服务器连接失败!"

    def get_report(self, days, types=None, user_id=None, end_date=datetime.now(pytz.timezone("Asia/Shanghai")),
                   limit=10):
        if not types:
            types = self.PLAYBACK_REPORTING_TYPE_MOVIE
        sub_date = end_date - timedelta(days=int(days))
        start_time = sub_date.strftime("%Y-%m-%d 00:00:00")
        end_time = end_date.strftime("%Y-%m-%d 23:59:59")
        sql = "SELECT UserId, ItemId, ItemType, "
        sql += types + " AS name, "
        sql += "COUNT(1) AS play_count, "
        sql += "SUM(PlayDuration - PauseDuration) AS total_duarion "
        sql += "FROM PlaybackActivity "
        sql += f"WHERE ItemType = '{'Movie' if types == self.PLAYBACK_REPORTING_TYPE_MOVIE else 'Episode'}' "
        sql += f"AND DateCreated >= '{start_time}' AND DateCreated <= '{end_time}' "
        sql += "AND UserId not IN (select UserId from UserList) "
        if user_id:
            sql += f"AND UserId = '{user_id}' "
        sql += "GROUP BY name "
        sql += "ORDER BY play_count DESC "
        sql += "LIMIT " + str(limit)

        url = f"{self.host}/emby/user_usage_stats/submit_custom_query?api_key={settings.EMBY_API_KEY}"

        data = {
            "CustomQueryString": sql,
            "ReplaceUserId": False
        }
        resp = RequestUtils().post_res(url=url, data=data)
        if resp.status_code != 204 and resp.status_code != 200:
            return False, "🤕Emby 服务器连接失败!"
        ret = resp.json()
        if len(ret["colums"]) == 0:
            return False, ret["message"]
        return True, ret["results"]
