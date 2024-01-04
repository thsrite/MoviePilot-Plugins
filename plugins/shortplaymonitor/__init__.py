import os
import re
import threading
import datetime
from pathlib import Path

from typing import Any, List, Dict, Tuple, Optional
from xml.dom import minidom
from app.utils.dom import DomUtils
from PIL import Image
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.core.meta.words import WordsMatcher
from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.utils.system import SystemUtils

ffmpeg_lock = threading.Lock()


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
    plugin_version = "2.0"
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
    _exclude_keywords = ""
    _observer = []
    _timeline = "00:00:10"
    _dirconf = {}
    _renameconf = {}
    _coverconf = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._renameconf = {}
        self._coverconf = {}

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor_confs = config.get("monitor_confs")
            self._exclude_keywords = config.get("exclude_keywords") or ""

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
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

            target_path = event_path.replace(source_dir, dest_dir)

            # 硬链接
            if isinstance(rename_conf, bool):
                target = target_path.replace(dest_dir, "")
                parent = Path(Path(target).parents[0])
                last = target.replace(str(parent), "")
                if rename_conf:
                    # 自定义识别次
                    title, _ = WordsMatcher().prepare(parent)
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
                # 目标文件夹不存在则创建
                if not Path(target_path).parent.exists():
                    logger.info(f"创建目标文件夹 {Path(target_path).parent}")
                    os.makedirs(Path(target_path).parent)

                # 文件：nfo、图片、视频文件
                if Path(target_path).exists():
                    logger.debug(f"目标文件 {target_path} 已存在")
                    return

                # 硬链接
                retcode, retmsg = SystemUtils.link(Path(event_path), target_path)
                if retcode == 0:
                    logger.info(f"文件 {event_path} 硬链接完成")
                    # 生成 tvshow.nfo
                    if not (target_path.parent / "tvshow.nfo").exists():
                        self.__gen_tv_nfo_file(dir_path=target_path.parent,
                                               title=title)

                    # 生成缩略图
                    if not (target_path.parent / "poster.jpg").exists():
                        thumb_path = self.gen_file_thumb(file_path=target_path)
                        if thumb_path and Path(thumb_path).exists():
                            self.__save_poster(input_path=thumb_path,
                                               poster_path=target_path.parent / "poster.jpg",
                                               cover_conf=cover_conf)
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

        except Exception as e:
            logger.error(f"event_handler_created error: {e}")
            print(str(e))

    def __save_poster(self, input_path, poster_path, cover_conf):
        """
        截取图片做封面
        """
        image = Image.open(input_path)

        # 需要截取的长宽比（比如 16:9）
        if not cover_conf:
            target_ratio = 2 / 3
        else:
            covers = cover_conf.split(":")
            target_ratio = covers[0] / covers[1]

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

    def gen_file_thumb(self, file_path: Path):
        """
        处理一个文件
        """
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
            "onlyonce": self._onlyonce,
            "monitor_confs": self._monitor_confs
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
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "monitor_confs": "",
            "exclude_keywords": ""
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
