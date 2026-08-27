import datetime
import os
import re
import threading
from pathlib import Path
from threading import Lock
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin
from xml.dom import minidom

import chardet
import pytz
from PIL import Image
from apscheduler.schedulers.background import BackgroundScheduler
from lxml import etree
from requests import RequestException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.chain.media import MediaChain
from app.chain.scraping import ScrapingChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.db.oper.site import SiteOper
from app.plugins import _PluginBase
from app.schemas import TransferInfo, TransferDirectoryConf
from app.schemas.types import MediaSource, MediaType, MessageType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.media import MediaInfo, MetaInfoPath, WordsMatcher, resolve_media_identity
from app.sdk.network import RequestUtils
from app.sdk.utilities import DomUtils, SystemUtils, retry

ffmpeg_lock = threading.Lock()
lock = Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, watching_path: str, file_change: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = watching_path
        self.file_change = file_change

    def on_created(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.dest_path)


class ShortPlayMonitor(_PluginBase):
    # 插件名称
    plugin_name = "短剧刮削"
    # 插件描述
    plugin_desc = "监控视频短剧创建，刮削。"
    # 插件图标
    plugin_icon = "Amule_B.png"
    # 插件版本
    plugin_version = "5.0.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "shortplaymonitor_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _onlyonce = False
    _image = False
    _exclude_keywords = ""
    _transfer_type = "link"
    _observer = []
    _timeline = "00:00:10"
    _dirconf = {}
    _renameconf = {}
    _coverconf = {}
    tmdbchain = None
    mediachain = None
    scrapingchain = None
    storagechain = None
    siteoper = None
    _interval = 10
    _notify = False
    _medias = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 热重载前必须先停止旧资源，否则覆盖句柄后无法回收目录观察器。
        self.stop_service()

        # 清空配置
        self._dirconf = {}
        self._renameconf = {}
        self._coverconf = {}
        self._observer = []
        self._medias = {}
        self.tmdbchain = TmdbChain()
        self.mediachain = MediaChain()
        self.scrapingchain = ScrapingChain()
        self.storagechain = StorageChain()
        self.siteoper = SiteOper()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._image = config.get("image")
            self._interval = config.get("interval")
            self._notify = config.get("notify")
            self._monitor_confs = config.get("monitor_confs")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._transfer_type = config.get("transfer_type") or "link"

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_confs = (self._monitor_confs or "").splitlines()
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 监控方式#监控目录#目的目录#是否重命名#封面比例
                if not monitor_conf:
                    continue
                if str(monitor_conf).count("#") != 4:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                mode = str(monitor_conf).split("#")[0]
                source_dir = str(monitor_conf).split("#")[1]
                target_dir = str(monitor_conf).split("#")[2]
                rename_conf = str(monitor_conf).split("#")[3]
                cover_conf = str(monitor_conf).split("#")[4]

                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir
                self._renameconf[source_dir] = rename_conf
                self._coverconf[source_dir] = cover_conf

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                            logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    try:
                        if mode == "compatibility":
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{source_dir} 的目录监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{source_dir} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{source_dir} 启动目录监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("短剧监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name="短剧监控全量执行")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

        if self._image:
            self._image = False
            self.__update_config()
            self.__handle_image()

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT):
                self.__handle_file(is_directory=Path(file_path).is_dir(),
                                   event_path=str(file_path),
                                   source_dir=mon_path)
        logger.info("全量同步短剧监控目录完成！")

    def __handle_image(self):
        """
        立即运行一次，裁剪封面
        """
        if not self._dirconf or not self._dirconf.keys():
            logger.error("未正确配置，停止裁剪 ...")
            return

        logger.info("开始全量裁剪封面 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            cover_conf = self._coverconf.get(mon_path)
            target_path = self._dirconf.get(mon_path)
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(target_path), ["poster.jpg"]):
                try:
                    if Path(file_path).name != "poster.jpg":
                        continue
                    image = Image.open(file_path)
                    if image.width / image.height != int(str(cover_conf).split(":")[0]) / int(
                            str(cover_conf).split(":")[1]):
                        self.__save_poster(input_path=file_path,
                                           poster_path=file_path,
                                           cover_conf=cover_conf)
                        logger.info(f"封面 {file_path} 已裁剪 比例为 {cover_conf}")
                except Exception:
                    continue
        logger.info("全量裁剪封面完成！")

    def event_handler(self, event, source_dir: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param source_dir: 监控目录
        :param event_path: 事件文件路径
        """
        # 回收站及隐藏的文件不处理
        if (event_path.find("/@Recycle") != -1
                or event_path.find("/#recycle") != -1
                or event_path.find("/.") != -1
                or event_path.find("/@eaDir") != -1):
            logger.info(f"{event_path} 是回收站或隐藏的文件，跳过处理")
            return

        # 命中过滤关键字不处理
        if self._exclude_keywords:
            for keyword in self._exclude_keywords.split("\n"):
                if keyword and re.findall(keyword, event_path):
                    logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                    return

        # 不是媒体文件不处理
        if Path(event_path).suffix not in settings.RMT_MEDIAEXT:
            logger.debug(f"{event_path} 不是媒体文件")
            return

        # 文件发生变化
        logger.debug(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(is_directory=event.is_directory,
                           event_path=event_path,
                           source_dir=source_dir)

    def _get_tmdb_episodes(self, mediainfo: MediaInfo, season: int) -> Optional[List[Any]]:
        """把完整媒体身份转换为 TMDB 身份后查询季集信息。"""
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if not media_source or not media_id:
            return None

        tmdb_id = media_id if media_source == MediaSource.TMDB else None
        if not tmdb_id:
            converted = self.mediachain.convert_media_identity(
                target_source=MediaSource.TMDB,
                media_source=media_source,
                media_id=media_id,
                mtype=mediainfo.type,
                season=season,
            )
            tmdb_id = converted.get("id") if converted else None
        if not tmdb_id or not str(tmdb_id).isdigit():
            return None
        return self.tmdbchain.tmdb_episodes(tmdbid=int(tmdb_id), season=season)

    def __handle_file(self, is_directory: bool, event_path: str, source_dir: str):
        """
        同步一个文件
        :event.is_directory
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        """
        try:
            # 转移路径
            dest_dir = self._dirconf.get(source_dir)
            # 是否重命名
            rename_conf = self._renameconf.get(source_dir)
            # 封面比例
            cover_conf = self._coverconf.get(source_dir)
            # 元数据
            file_meta = MetaInfoPath(Path(event_path))
            if not file_meta.name:
                logger.error(f"{Path(event_path).name} 无法识别有效信息")
                return
            if not dest_dir:
                logger.error(f"未配置监控目录 {source_dir} 的目的目录")
                return

            # 识别媒体信息
            mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)

            transfer_flag = False
            title = None
            # 走tmdb刮削
            media_source, media_id = resolve_media_identity(media=mediainfo)
            if mediainfo and media_source and media_id:
                try:
                    file_item = self.storagechain.get_file_item(storage="local", path=Path(event_path))
                    if not file_item:
                        logger.error(f"未找到媒体文件项：{event_path}")
                        return

                    # 更新媒体图片
                    self.chain.obtain_images(mediainfo=mediainfo)
                    episodes_info = (
                        self._get_tmdb_episodes(
                            mediainfo=mediainfo,
                            season=file_meta.begin_season or 1,
                        )
                        if mediainfo.type == MediaType.TV
                        else None
                    )
                    mediainfo.category = ""
                    target_dir = TransferDirectoryConf()
                    target_dir.library_path = Path(dest_dir)
                    target_dir.transfer_type = self._transfer_type
                    target_dir.scraping = False
                    target_dir.renaming = True
                    target_dir.notify = False
                    target_dir.overwrite_mode = "never"
                    target_dir.library_storage = "local"
                    # 转移
                    transferinfo: TransferInfo = self.chain.transfer(
                        fileitem=file_item,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        target_directory=target_dir,
                        episodes_info=episodes_info,
                    )
                    if not transferinfo:
                        logger.error("文件转移模块运行失败")
                        transfer_flag = False
                    elif not transferinfo.success:
                        logger.error(f"文件转移失败：{transferinfo.message or '未知原因'}")
                        transfer_flag = False
                    else:
                        self.scrapingchain.scrape_metadata(
                            fileitem=transferinfo.target_diritem,
                            meta=file_meta,
                            mediainfo=mediainfo,
                        )
                        transfer_flag = True
                except Exception as e:
                    transfer_flag = False
                    logger.error(f"{event_path} tmdb刮削失败：{e}")
                # 广播事件
                # self.eventmanager.send_event(EventType.TransferComplete, {
                #     'meta': file_meta,
                #     'mediainfo': mediainfo,
                #     'transferinfo': transferinfo
                # })
            if not transfer_flag:
                target_path = event_path.replace(source_dir, dest_dir)

                # 目录重命名
                if str(rename_conf) == "true" or str(rename_conf) == "false":
                    rename_conf = bool(rename_conf)
                    target = target_path.replace(dest_dir, "")
                    parent = Path(Path(target).parents[0])
                    last = target.replace(str(parent), "")
                    if rename_conf:
                        # 自定义识别次
                        title, _ = WordsMatcher().prepare(str(parent))
                        target_path = Path(dest_dir).joinpath(title + last)
                    else:
                        title = parent
                else:
                    if str(rename_conf) == "smart":
                        target = target_path.replace(dest_dir, "")
                        parent = Path(Path(target).parents[0])
                        last = target.replace(str(parent), "")
                        # 取.第一个
                        title = Path(parent).name.split(".")[0]
                        target_path = Path(dest_dir).joinpath(title + last)
                    else:
                        logger.error(f"{target_path} 智能重命名失败")
                        return

                # 文件夹同步创建
                if is_directory:
                    # 目标文件夹不存在则创建
                    if not Path(target_path).exists():
                        logger.info(f"创建目标文件夹 {target_path}")
                        os.makedirs(target_path)
                else:
                    # 媒体重命名
                    try:
                        pattern = r'S\d+E\d+'
                        matches = re.search(pattern, Path(target_path).name)
                        if matches:
                            target_path = Path(
                                target_path).parent / f"{matches.group()}{Path(Path(target_path).name).suffix}"
                        else:
                            print("未找到匹配的季数和集数")
                    except Exception as e:
                        print(e)

                    # 目标文件夹不存在则创建
                    if not Path(target_path).parent.exists():
                        logger.info(f"创建目标文件夹 {Path(target_path).parent}")
                        os.makedirs(Path(target_path).parent)

                    # 文件：nfo、图片、视频文件
                    if Path(target_path).exists():
                        logger.debug(f"目标文件 {target_path} 已存在")
                        return

                    # 硬链接
                    retcode = self.__transfer_command(file_item=Path(event_path),
                                                      target_file=target_path,
                                                      transfer_type=self._transfer_type)
                    if retcode == 0:
                        logger.info(f"文件 {event_path} 硬链接完成")
                        # 生成 tvshow.nfo
                        if not (target_path.parent / "tvshow.nfo").exists():
                            self.__gen_tv_nfo_file(dir_path=target_path.parent,
                                                   title=title)

                        # 生成缩略图
                        if not (target_path.parent / "poster.jpg").exists():
                            thumb_path = self.gen_file_thumb(title=title,
                                                             rename_conf=rename_conf,
                                                             file_path=target_path)
                            if thumb_path and Path(thumb_path).exists():
                                self.__save_poster(input_path=thumb_path,
                                                   poster_path=target_path.parent / "poster.jpg",
                                                   cover_conf=cover_conf)
                                if (target_path.parent / "poster.jpg").exists():
                                    logger.info(f"{target_path.parent / 'poster.jpg'} 缩略图已生成")
                                thumb_path.unlink()
                            else:
                                # 检查是否有缩略图
                                thumb_files = SystemUtils.list_files(directory=target_path.parent,
                                                                     extensions=[".jpg"])
                                if thumb_files:
                                    # 生成poster
                                    for thumb in thumb_files:
                                        self.__save_poster(input_path=thumb,
                                                           poster_path=target_path.parent / "poster.jpg",
                                                           cover_conf=cover_conf)
                                        break
                                    # 删除多余jpg
                                    for thumb in thumb_files:
                                        Path(thumb).unlink()
                    else:
                        logger.error(f"文件 {event_path} 硬链接失败，错误码：{retcode}")
            if self._notify:
                # 发送消息汇总
                media_list = self._medias.get(mediainfo.title_year if mediainfo else title) or {}
                if media_list:
                    media_files = media_list.get("files") or []
                    if media_files:
                        if str(event_path) not in media_files:
                            media_files.append(str(event_path))
                    else:
                        media_files = [str(event_path)]
                    media_list = {
                        "files": media_files,
                        "time": datetime.datetime.now()
                    }
                else:
                    media_list = {
                        "files": [str(event_path)],
                        "time": datetime.datetime.now()
                    }
                self._medias[mediainfo.title_year if mediainfo else title] = media_list
        except Exception as e:
            logger.error(f"event_handler_created error: {e}")
            print(str(e))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if self._notify:
            if not self._medias or not self._medias.keys():
                return

            # 遍历检查是否已刮削完，发送消息
            for medis_title_year in list(self._medias.keys()):
                media_list = self._medias.get(medis_title_year)
                logger.info(f"开始处理媒体 {medis_title_year} 消息")

                if not media_list:
                    continue

                # 获取最后更新时间
                last_update_time = media_list.get("time")
                media_files = media_list.get("files")
                if not last_update_time or not media_files:
                    continue

                # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
                if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval):
                    # 发送消息
                    self.post_message(mtype=MessageType.Organize,
                                      title=f"{medis_title_year} 共{len(media_files)}集已入库",
                                      text="类别：短剧")
                    # 发送完消息，移出key
                    del self._medias[medis_title_year]
                    continue

    @staticmethod
    def __transfer_command(file_item: Path, target_file: Path, transfer_type: str) -> int:
        """
        使用系统命令处理单个文件
        :param file_item: 文件路径
        :param target_file: 目标文件路径
        :param transfer_type: RmtMode转移方式
        """
        with lock:

            # 转移
            if transfer_type == 'link':
                # 硬链接
                retcode, retmsg = SystemUtils.link(file_item, target_file)
            elif transfer_type == 'softlink':
                # 软链接
                retcode, retmsg = SystemUtils.softlink(file_item, target_file)
            elif transfer_type == 'move':
                # 移动
                retcode, retmsg = SystemUtils.move(file_item, target_file)
            else:
                # 复制
                retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode

    def __save_poster(self, input_path, poster_path, cover_conf):
        """
        截取图片做封面
        """
        try:
            image = Image.open(input_path)

            # 需要截取的长宽比（比如 16:9）
            if not cover_conf:
                target_ratio = 2 / 3
            else:
                covers = cover_conf.split(":")
                target_ratio = int(covers[0]) / int(covers[1])

            # 获取原始图片的长宽比
            original_ratio = image.width / image.height

            # 计算截取后的大小
            if original_ratio > target_ratio:
                new_height = image.height
                new_width = int(new_height * target_ratio)
            else:
                new_width = image.width
                new_height = int(new_width / target_ratio)

            # 计算截取的位置
            left = (image.width - new_width) // 2
            top = (image.height - new_height) // 2
            right = left + new_width
            bottom = top + new_height

            # 截取图片
            cropped_image = image.crop((left, top, right, bottom))

            # 保存截取后的图片
            cropped_image.save(poster_path)
        except Exception as e:
            print(str(e))

    def __gen_tv_nfo_file(self, dir_path: Path, title: str):
        """
        生成电视剧的NFO描述文件
        :param dir_path: 电视剧根目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")

        # 标题
        DomUtils.add_node(doc, root, "title", title)
        DomUtils.add_node(doc, root, "originaltitle", title)
        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        # 保存
        self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

    def __save_nfo(self, doc, file_path: Path):
        """
        保存NFO
        """
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")

    def gen_file_thumb_from_site(self, title: str, file_path: Path):
        """
        从agsv或者萝莉站查询封面
        """
        try:
            image = None
            # 查询索引
            domain = "agsvpt.com"
            site = self.siteoper.get_by_domain(domain)
            if site:
                req_url = f"https://www.agsvpt.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat=419&search={title}"
                image_xpath = "//*[@id='kdescr']/img[1]/@src"
                # 查询站点资源
                logger.info(f"开始检索 {site.name} {title}")
                image = self.__get_site_torrents(url=req_url, site=site, image_xpath=image_xpath)
            if not image:
                domain = "ilolicon.com"
                site = self.siteoper.get_by_domain(domain)
                if site:
                    req_url = f"https://share.ilolicon.com/torrents.php?search_mode=0&search_area=0&page=0&notnewword=1&cat=402&search={title}"

                    image_xpath = "//*[@id='kdescr']/img[1]/@src"
                    # 查询站点资源
                    logger.info(f"开始检索 {site.name} {title}")
                    image = self.__get_site_torrents(url=req_url, site=site, image_xpath=image_xpath)

            if not image:
                logger.error(f"检索站点 {title} 封面失败")
                return None

            # 下载图片保存
            if self.__save_image(url=image, file_path=file_path):
                return file_path
            return None
        except Exception as e:
            logger.error(f"检索站点 {title} 封面失败 {str(e)}")
            return None

    @retry(RequestException, logger=logger)
    def __save_image(self, url: str, file_path: Path):
        """
        下载图片并保存
        """
        response = None
        try:
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            response = RequestUtils().get_res(url=url, raise_exception=True)
            if response is not None:
                file_path.write_bytes(response.content)
                logger.info(f"图片已保存：{file_path}")
                return True
            logger.info(f"{file_path.stem}图片下载失败，请检查网络连通性")
            return False
        except RequestException as err:
            raise err
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}")
            return False
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _extract_detail_url(page_source: str, base_url: str) -> Optional[str]:
        """从固定 NexusPHP 搜索页提取首个种子详情地址。"""
        html = etree.HTML(page_source)
        if html is None:
            return None
        detail_links = html.xpath('//a[contains(@href, "details.php")]/@href')
        if not detail_links:
            return None
        return urljoin(base_url, str(detail_links[0]))

    def __get_site_torrents(self, url: str, site, image_xpath):
        """
        查询站点资源
        """
        page_source = self.__get_page_source(url=url, site=site)
        if not page_source:
            logger.error(f"请求站点 {site.name} 失败")
            return None
        detail_url = self._extract_detail_url(page_source=page_source, base_url=url)
        if not detail_url:
            logger.error(f"未检索到站点 {site.name} 资源")
            return None

        # 获取种子详情页
        torrent_detail_source = self.__get_page_source(url=detail_url, site=site)
        if not torrent_detail_source:
            logger.error(f"请求种子详情页失败 {detail_url}")
            return None

        html = etree.HTML(torrent_detail_source)
        if html is None:
            logger.error(f"请求种子详情页失败 {detail_url}")
            return None

        images = html.xpath(image_xpath)
        if not images:
            logger.error(f"未获取到种子封面图 {detail_url}")
            return None

        return urljoin(detail_url, str(images[0]))

    def __get_page_source(self, url: str, site):
        """
        获取页面资源
        """
        response = None
        try:
            response = RequestUtils(
                cookies=site.cookie,
                timeout=30,
            ).get_res(url, allow_redirects=True)
            if response is None:
                return ""

            raw_data = response.content
            if not raw_data:
                return response.text
            try:
                result = chardet.detect(raw_data)
                return raw_data.decode(result["encoding"])
            except Exception:
                if re.search(r"charset=\"?utf-8\"?", response.text, re.IGNORECASE):
                    response.encoding = "utf-8"
                else:
                    response.encoding = response.apparent_encoding
                return response.text
        finally:
            if response is not None:
                response.close()

    def gen_file_thumb(self, title: str, file_path: Path, rename_conf: str):
        """
        处理一个文件
        """
        # 智能重命名时从站点检索
        if str(rename_conf) == "smart":
            thumb_path = file_path.with_name(file_path.stem + "-site.jpg")
            if thumb_path.exists():
                logger.info(f"缩略图已存在：{thumb_path}")
                return
            self.gen_file_thumb_from_site(title=title, file_path=thumb_path)
            if Path(thumb_path).exists():
                logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
                return thumb_path
        # 单线程处理
        with ffmpeg_lock:
            try:
                thumb_path = file_path.with_name(file_path.stem + "-thumb.jpg")
                if thumb_path.exists():
                    logger.info(f"缩略图已存在：{thumb_path}")
                    return
                self.get_thumb(video_path=str(file_path),
                               image_path=str(thumb_path),
                               frames=self._timeline)
                if Path(thumb_path).exists():
                    logger.info(f"{file_path} 缩略图已生成：{thumb_path}")
                    return thumb_path
            except Exception as err:
                logger.error(f"FFmpeg处理文件 {file_path} 时发生错误：{str(err)}")
                return None

    @staticmethod
    def get_thumb(video_path: str, image_path: str, frames: str = None):
        """
        使用ffmpeg从视频文件中截取缩略图
        """
        if not frames:
            frames = "00:00:10"
        if not video_path or not image_path:
            return False
        cmd = 'ffmpeg -y -i "{video_path}" -ss {frames} -frames 1 "{image_path}"'.format(
            video_path=video_path,
            frames=frames,
            image_path=image_path)
        result = SystemUtils.execute(cmd)
        if result:
            return True
        return False

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "exclude_keywords": self._exclude_keywords,
            "transfer_type": self._transfer_type,
            "onlyonce": self._onlyonce,
            "interval": self._interval,
            "notify": self._notify,
            "image": self._image,
            "monitor_confs": self._monitor_confs
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
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
                                            'model': 'image',
                                            'label': '封面裁剪',
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
                                            'label': '发送通知',
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
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                            ]
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
                                            'model': 'interval',
                                            'label': '入库消息延迟',
                                            'placeholder': '10'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '监控方式#监控目录#目的目录#是否重命名#封面比例'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
                                            'text': '配置说明：'
                                                    'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/ShortPlayMonitor.md'
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
                                            'text': '默认从tmdb刮削，刮削失败则从pt站刮削。当重命名方式为smart时，如站点管理已配置AGSV、ilolicon，则优先从站点获取短剧封面。'
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
                                            'text': '开启封面裁剪后，会把封面裁剪成配置的比例。'
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
            "image": False,
            "notify": False,
            "interval": 10,
            "monitor_confs": "",
            "exclude_keywords": "",
            "transfer_type": "link"
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

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
