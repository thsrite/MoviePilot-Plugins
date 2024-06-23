import datetime
import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.modules.emby import Emby
from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

# from clouddrive import CloudDriveClient

lock = threading.Lock()


class CloudFileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(CloudFileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class CloudAssistant(_PluginBase):
    # 插件名称
    plugin_name = "云盘助手"
    # 插件描述
    plugin_desc = "定时移动到云盘，软连接/strm回本地，定时清理无效软连接"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/cloudassistant.png"
    # 插件版本
    plugin_version = "1.5"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudassistant_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 3

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    tmdbchain = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _invalid = False
    _only_media = False
    _refresh = False
    _cron = None
    _invalid_cron = None
    _clean = False
    _exclude_keywords = ""
    _dir_confs = {}
    _transfer_type = None
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

    # 退出事件
    _event = threading.Event()

    example = {
        "transfer_type": "copy/move",
        "return_mode": "softlink",
        "monitor_dirs": [
            {
                "monitor_mode": "模式 compatibility/fast",
                "local_path": "/mnt/media/movies",
                "mount_path": "/mnt/cloud/115/media/movies",
                "return_path": "/mnt/softlink/movies",
                "delete_local": "false",
                "local_preserve_hierarchy": 0,
                "delete_history": "false",
                "delete_source": "false",
                "source_dirs": "/mnt/media/movies, /mnt/media/series",
                "source_preserve_hierarchy": 0,
                "just_media": "true",
                "overwrite": "false",
                "upload_cloud": "true"
            }
        ]
    }
    # _client = None
    # _fs = None
    _return_mode = None
    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        # 清空配置
        self._dirconf = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._invalid = config.get("invalid")
            self._clean = config.get("clean")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._cron = config.get("cron")
            self._invalid_cron = config.get("invalid_cron")
            self._dir_confs = config.get("dir_confs") or None
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

            # 清理插件历史
            if self._clean:
                self.del_data(key="history")
                self._clean = False
                self.__update_config()

            if not self._dir_confs:
                return

            # 停止现有任务
            self.stop_service()

            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._invalid:
                logger.info("清理无效软连接服务启动，立即运行一次")
                self._scheduler.add_job(func=self.handle_invalid_links, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                # 关闭无效软连接开关
                self._invalid = False
                # 保存配置
                self.__update_config()

            if self._enabled or self._onlyonce:
                dir_confs = json.loads(self._dir_confs)
                # 检查cd2配置
                # if not dir_confs.get("cd2_url") or not dir_confs.get("username") or not dir_confs.get("password"):
                #     if not dir_confs.get("transfer_type"):
                #         logger.error("未正确配置CloudDrive2或者transfer_type，请检查配置")
                #         return
                #     else:
                #         self._transfer_type = dir_confs.get("transfer_type")
                #         logger.warn("未配置CloudDrive2，使用transfer_type转移模式")
                # else:
                #     try:
                #         self._client = CloudDriveClient(dir_confs.get("cd2_url"),
                #                                         dir_confs.get("username"),
                #                                         dir_confs.get("password"))
                #         if self._client:
                #             self._fs = self._client.fs
                #     except Exception as e:
                #         logger.warn(f"未正确配置CloudDrive2，请检查配置：{e}")
                #         return

                self._transfer_type = dir_confs.get("transfer_type")
                self._return_mode = dir_confs.get("return_mode") or "softlink"

                # 读取目录配置
                monitor_dirs = dir_confs.get("monitor_dirs") or []
                if not monitor_dirs:
                    return
                for monitor_dir in monitor_dirs:
                    if not monitor_dir:
                        continue

                    mon_path = monitor_dir.get("local_path")
                    target_path = monitor_dir.get("mount_path")
                    monitor_mode = monitor_dir.get("monitor_mode") or "compatibility"
                    self._dirconf[mon_path] = monitor_dir

                    # 启用目录监控
                    if self._enabled:
                        # 检查媒体库目录是不是下载目录的子目录
                        try:
                            if target_path and target_path.is_relative_to(Path(mon_path)):
                                logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                                self.systemmessage.put(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控",
                                                       title="目录监控")
                                continue
                        except Exception as e:
                            logger.debug(str(e))
                            pass

                        try:
                            if str(monitor_mode) == "compatibility":
                                # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                                observer = PollingObserver(timeout=10)
                            else:
                                # 内部处理系统操作类型选择最优解
                                observer = Observer(timeout=10)
                            self._observer.append(observer)
                            observer.schedule(CloudFileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                            observer.daemon = True
                            observer.start()
                            logger.info(f"{mon_path} 的目录监控服务启动")
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
                                logger.error(f"{mon_path} 启动目录监控失败：{err_msg}")
                            self.systemmessage.put(f"{mon_path} 启动目录监控失败：{err_msg}", title="目录监控")

                # 运行一次定时服务
                if self._onlyonce:
                    logger.info("目录监控服务启动，立即运行一次")
                    self._scheduler.add_job(func=self.sync_all, trigger='date',
                                            run_date=datetime.datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                            )
                    # 关闭一次性开关
                    self._onlyonce = False
                    # 保存配置
                    self.__update_config()

                if self._invalid_cron:
                    self._scheduler.add_job(func=self.handle_invalid_links,
                                            trigger=CronTrigger.from_crontab(self._invalid_cron),
                                            id="handle_invalid_links")
                    logger.info(f"清理无效软连接服务启动，定时任务：{self._invalid_cron}")

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
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "invalid": self._invalid,
            "clean": self._clean,
            "dir_confs": self._dir_confs,
            "exclude_keywords": self._exclude_keywords,
            "cron": self._cron,
            "only_media": self._only_media,
            "refresh": self._refresh,
            "invalid_cron": self._invalid_cron,
            "rmt_mediaext": self._rmt_mediaext
        })

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloudassistant":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘助手开始同步监控目录 ...",
                              userid=event.event_data.get("user"))
        self.sync_all()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘助手监控目录同步完成！", userid=event.event_data.get("user"))

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("云盘助手全量同步监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            monitor_conf = self._dirconf.get(mon_path)
            just_media = monitor_conf.get("just_media") or True
            # 遍历目录下所有文件
            for root, dirs, files in os.walk(mon_path):
                for name in dirs + files:
                    file_path = os.path.join(root, name)
                    if Path(str(file_path)).is_file():
                        if str(just_media) == "true" and Path(str(file_path)).suffix.lower() not in [ext.strip() for ext
                                                                                                     in
                                                                                                     self._rmt_mediaext.split(
                                                                                                         ",")]:
                            continue
                        self.__handle_file(event_path=str(file_path), mon_path=mon_path)
        logger.info("云盘助手全量同步监控目录完成！")

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug("文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """
        file_path = Path(event_path)
        if Path(file_path).is_dir():
            return
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:
                # 回收站及隐藏的文件不处理
                if event_path.find('/@Recycle/') != -1 \
                        or event_path.find('/#recycle/') != -1 \
                        or event_path.find('/.') != -1 \
                        or event_path.find('/@eaDir') != -1:
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, event_path, re.IGNORECASE):
                            logger.info(f"{event_path} 命中整理屏蔽词 {keyword}，不处理")
                            return

                # 判断是不是蓝光目录
                if re.search(r"BDMV[/\\]STREAM", event_path, re.IGNORECASE):
                    # 截取BDMV前面的路径
                    blurray_dir = event_path[:event_path.find("BDMV")]
                    file_path = Path(blurray_dir)
                    logger.info(f"{event_path} 是蓝光目录，更正文件路径为：{str(file_path)}")

                # 查询转移配置
                monitor_dir = self._dirconf.get(mon_path)
                mount_path = monitor_dir.get("mount_path")
                # cd2_path = monitor_dir.get("cd2_path")
                return_path = monitor_dir.get("return_path")
                delete_local = monitor_dir.get("delete_local") or "false"
                delete_source = monitor_dir.get("delete_source") or "false"
                delete_history = monitor_dir.get("delete_history") or "false"
                overwrite = monitor_dir.get("overwrite") or "false"
                upload_cloud = monitor_dir.get("upload_cloud") or "true"
                local_preserve_hierarchy = monitor_dir.get("local_preserve_hierarchy") or 0
                source_dirs = monitor_dir.get("source_dirs") or ""
                source_preserve_hierarchy = monitor_dir.get("source_preserve_hierarchy") or 0

                # 1、转移到云盘挂载路径 上传到cd2
                # 挂载的路径
                mount_file = str(file_path).replace(str(mon_path), str(mount_path))
                logger.info(f"挂载目录文件 {mount_file}")

                if str(upload_cloud) == "true":
                    # cd2模式
                    # if self._client:
                    #     logger.info("开始上传文件到CloudDrive2")
                    #     # cd2目标路径
                    #     cd2_file = str(file_path).replace(str(mon_path), str(cd2_path))
                    #     logger.info(f"cd2目录文件 {cd2_file}")
                    #
                    #     # 上传前先检查文件是否存在
                    #     cd2_file_exists = False
                    #     if str(overwrite) == "false":
                    #         if self._fs.exists(Path(cd2_file)):  # 云盘文件存在则跳过
                    #             logger.info(f"云盘文件 {cd2_file} 已存在，跳过上传")
                    #             cd2_file_exists = True
                    #
                    #     if not cd2_file_exists:
                    #         # cd2目录不存在则创建
                    #         if not self._fs.exists(Path(cd2_file).parent):
                    #             self._fs.mkdir(Path(cd2_file).parent)
                    #             logger.info(f"创建cd2目录 {Path(cd2_file).parent}")
                    #         # 切换cd2路径
                    #         self._fs.chdir(Path(cd2_file).parent)
                    #
                    #         # 上传文件到cd2
                    #         logger.info(f"开始上传文件 {file_path} 到 {cd2_file}")
                    #         self._fs.upload(file_path, overwrite_or_ignore=True)
                    #         self._fs.move(file_path)
                    #         logger.info(f"上传文件 {file_path} 到 {cd2_file}完成")
                    #
                    #     # 上传任务列表
                    #     # upload_tasklist = self._client.upload_tasklist
                    #     # logger.info(f"上传任务列表 {upload_tasklist}")
                    # else:
                    upload = True
                    if str(overwrite) == "false":
                        if Path(mount_file).exists():
                            logger.info(f"云盘文件 {mount_file} 已存在且未开启覆盖，跳过上传")
                            upload = False

                    if upload:
                        if Path(file_path).suffix.lower() in [ext.strip() for ext in
                                                              self._rmt_mediaext.split(",")]:
                            self.__transfer_file(file_path=file_path,
                                                 target_file=mount_file,
                                                 transfer_type=self._transfer_type)
                        else:
                            # 其他文件复制
                            SystemUtils.copy(file_path, Path(mount_file))

                # 2、软连接回本地路径
                if not Path(mount_file).exists():
                    logger.info(f"挂载目录文件 {mount_file} 不存在，不创建 {self._return_mode}")
                    return

                target_return_file = str(file_path).replace(str(mon_path), str(return_path))
                if Path(target_return_file).suffix.lower() in [ext.strip() for ext in
                                                               self._rmt_mediaext.split(",")]:
                    # 媒体文件软连接
                    if str(self._return_mode) == "softlink":
                        retcode = self.__transfer_file(file_path=mount_file,
                                                       target_file=target_return_file,
                                                       transfer_type="softlink")
                    else:
                        # 生成strm文件
                        retcode = self.__create_strm_file(mount_file=mount_file,
                                                          mount_path=mount_path,
                                                          file_path=str(file_path),
                                                          library_dir=monitor_dir.get("library_dir"),
                                                          cloud_type=monitor_dir.get("cloud_type"),
                                                          cloud_path=monitor_dir.get("cloud_path"),
                                                          cloud_url=monitor_dir.get("cloud_url"),
                                                          cloud_scheme=monitor_dir.get("cloud_scheme"))

                else:
                    # 其他nfo、jpg等复制文件
                    SystemUtils.copy(file_path, Path(target_return_file))
                    # shutil.copy2(str(file_path), target_return_file)
                    logger.info(f"复制其他文件 {str(file_path)} 到 {target_return_file}")
                    retcode = 0

                if retcode == 0:
                    transferhis = self.transferhis.get_by_dest(str(file_path))
                    if transferhis and self._refresh:
                        self.__refresh_emby(transferhis)

                    # 是否删除本地历史
                    if str(delete_history) == "true":
                        if transferhis:
                            self.transferhis.delete(transferhis.id)
                            logger.info(f"删除本地历史记录：{transferhis.id}")

                    # 3、存操作记录
                    if (self._only_media and Path(file_path).suffix.lower() in [ext.strip() for ext in
                                                                                self._rmt_mediaext.split(",")]) \
                            or not self._only_media:
                        history = self.get_data('history') or []
                        history.append({
                            "file_path": str(file_path),
                            "target_cloud_file": mount_file,
                            "target_soft_file": target_return_file,
                            "delete_local": delete_local,
                            "delete_history": delete_history,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                        })
                        # 保存历史
                        self.save_data(key="history", value=history)

                    # 移动模式删除空目录
                    if str(delete_local) == "true":
                        if file_path.exists():
                            file_path.unlink()
                            logger.info(f"删除监控文件：{file_path}")

                        # 保留层级
                        mon_path_depth = len(Path(mon_path).parts)
                        retain_depth = mon_path_depth + int(local_preserve_hierarchy)

                        for file_dir in file_path.parents:
                            if len(file_dir.parts) <= retain_depth:
                                # 重要，删除到保留层级目录为止
                                break
                            files = SystemUtils.list_files(file_dir, settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT)
                            if not files:
                                logger.warn(f"删除监控空目录：{file_dir}")
                                shutil.rmtree(file_dir, ignore_errors=True)

                    # 是否删除源文件
                    if str(delete_source) == "true" and transferhis:
                        if Path(transferhis.src).exists():
                            Path(transferhis.src).unlink()
                            logger.info(f"删除源文件：{transferhis.src}")

                        # 删除下载文件记录
                        self.downloadhis.delete_file_by_fullpath(transferhis.src)

                        # 发送事件 删种
                        eventmanager.send_event(
                            EventType.DownloadFileDeleted,
                            {
                                "src": transferhis.src,
                                "hash": transferhis.download_hash
                            }
                        )

                        # 源文件保留层级
                        source_path = None
                        for source_dir in source_dirs.split(","):
                            source_dir = source_dir.strip()
                            if not source_dir:
                                continue
                            if transferhis.src.startswith(source_dir):
                                source_path = source_dir
                                break

                        # 删除源文件空目录
                        if source_path:
                            # 保留层级
                            source_path_depth = len(Path(source_path).parts)
                            retain_depth = source_path_depth + int(source_preserve_hierarchy)

                            for file_dir in Path(transferhis.src).parents:
                                if len(file_dir.parts) <= retain_depth:
                                    # 重要，删除到保留层级目录为止
                                    break
                                files = SystemUtils.list_files(file_dir,
                                                               settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT)
                                if not files:
                                    logger.warn(f"删除源文件空目录：{file_dir}")
                                    shutil.rmtree(file_dir, ignore_errors=True)
        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def __transfer_file(self, file_path, target_file, transfer_type):
        """
        转移文件
        """
        logger.info(f"开始 {transfer_type} 文件 {str(file_path)} 到 {target_file}")
        # 如果是文件夹
        if Path(target_file).is_dir():
            if not Path(target_file).exists():
                logger.info(f"创建目标文件夹 {target_file}")
                os.makedirs(target_file)
                return 1
        else:
            # 文件
            if Path(target_file).exists():
                logger.info(f"目标文件 {target_file} 已存在")
                return 1

            if not Path(target_file).parent.exists():
                logger.info(f"创建目标文件夹 {Path(target_file).parent}")
                os.makedirs(Path(target_file).parent)

            # 媒体文件转移
            retcode, retmsg = self.__transfer_command(file_path, Path(target_file), transfer_type)
            logger.info(
                f"媒体文件{str(file_path)} {transfer_type} 到 {target_file} {retcode} {retmsg}")
            return retcode

    def __transfer_command(self, file_item: Path, target_file: Path, transfer_type: str):
        """
        使用系统命令处理单个文件
        :param file_item: 文件路径
        :param target_file: 目标文件路径
        :param transfer_type: RmtMode转移方式
        """
        # 转移
        if transfer_type == 'link':
            # 硬链接
            retcode, retmsg = SystemUtils.link(file_item, target_file)
        elif transfer_type == 'softlink':
            # 软链接
            retcode, retmsg = SystemUtils.softlink(file_item, target_file)
        elif transfer_type == 'move':
            # 复制
            retcode, retmsg = SystemUtils.copy(file_item, target_file)
            if retcode == 0:
                file_item.unlink()
            else:
                logger.error(f"移动文件失败 {file_item} {target_file} {retcode} {retmsg}")
            # 移动
            # retcode, retmsg = SystemUtils.move(file_item, target_file)
        else:
            # 复制
            retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode, retmsg

    @staticmethod
    def __create_strm_file(mount_file: str, mount_path: str, file_path: str, library_dir: str = None,
                           cloud_type: str = None, cloud_path: str = None, cloud_url: str = None,
                           cloud_scheme: str = None):
        """
        生成strm文件
        :param library_dir:
        :param mount_path:
        :param mount_file:
        """
        try:
            # 获取视频文件名和目录
            video_name = Path(mount_file).name
            # 获取视频目录
            dest_path = Path(mount_file).parent

            if not dest_path.exists():
                logger.info(f"创建目标文件夹 {dest_path}")
                os.makedirs(str(dest_path))

            # 构造.strm文件路径
            strm_path = os.path.join(dest_path, f"{os.path.splitext(video_name)[0]}.strm")
            # strm已存在跳过处理
            if Path(strm_path).exists():
                logger.info(f"strm文件已存在 {strm_path}")
                return

            logger.info(f"替换前本地路径:::{mount_file}")

            # 云盘模式
            if cloud_type:
                # 替换路径中的\为/
                dest_file = file_path.replace("\\", "/")
                dest_file = dest_file.replace(cloud_path, "")
                # 对盘符之后的所有内容进行url转码
                dest_file = urllib.parse.quote(dest_file, safe='')
                if str(cloud_type) == "cd2":
                    # 将路径的开头盘符"/mnt/user/downloads"替换为"http://localhost:19798/static/http/localhost:19798/False/"
                    dest_file = f"{cloud_scheme}://{cloud_url}/static/{cloud_scheme}/{cloud_url}/False/{dest_file}"
                    logger.info(f"替换后cd2路径:::{dest_file}")
                elif str(cloud_type) == "alist":
                    dest_file = f"{cloud_scheme}://{cloud_url}/d/{dest_file}"
                    logger.info(f"替换后alist路径:::{dest_file}")
                else:
                    logger.error(f"云盘类型 {cloud_type} 错误")
                    return
            else:
                # 本地挂载路径转为emby路径
                dest_file = mount_file.replace(mount_path, library_dir)
                logger.info(f"替换后emby容器内路径:::{dest_file}")

            # 写入.strm文件
            with open(strm_path, 'w') as f:
                f.write(dest_file)

            logger.info(f"创建strm文件 {strm_path}")
            return 0
        except Exception as e:
            logger.error(f"创建strm文件失败")
            print(str(e))
            return 1

    @staticmethod
    def is_broken_symlink(path):
        current_target = os.readlink(path)
        if not os.path.exists(current_target):
            return True
        return False

    def handle_invalid_links(self):
        """
        立即运行一次，清理无效软连接
        """
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for root, dirs, files in os.walk(mon_path):
                for name in dirs + files:
                    file_path = os.path.join(root, name)
                    if Path(str(file_path)).is_file() and self.is_broken_symlink(file_path):
                        print(f"删除无效软连接: {file_path}")
                        os.remove(file_path)
        logger.info("云盘助手清理无效软连接完成！")

    @staticmethod
    def update_symlink(target_from, target_to, directory):
        for root, dirs, files in os.walk(directory):
            for name in dirs + files:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    current_target = os.readlink(path)
                    if str(current_target).startswith(target_from):
                        new_target = current_target.replace(target_from, target_to)
                        os.remove(path)
                        os.symlink(new_target, path)
                        print(f"Updated symlink: {path} -> {new_target}")

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

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloud_assistant",
            "event": EventType.PluginAction,
            "desc": "云盘助手同步",
            "category": "",
            "data": {
                "action": "cloud_assistant"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/cloud_assistant",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "云盘助手同步",
            "description": "云盘助手同步",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "CloudAssistantSyncAll",
                "name": "云盘助手全量同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_all,
                "kwargs": {}
            }]
        return []

    def sync(self, apikey: str) -> schemas.Response:
        """
        API调用目录同步
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        self.sync_all()
        return schemas.Response(success=True)

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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即同步一次',
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
                                            'model': 'clean',
                                            'label': '清空插件历史',
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'refresh',
                                            'label': '刷新媒体库（emby）',
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
                                            'model': 'only_media',
                                            'label': '插件历史仅媒体文件',
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
                                            'model': 'invalid',
                                            'label': '立即清理无效软连接',
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 3
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dialog_closed",
                                            "label": "监控路径配置"
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'invalid_cron',
                                            'label': '定时清理无效软连接周期',
                                            'placeholder': '5位cron表达式，留空关闭'
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {
                            'style': {
                                'margin-top': '12px'
                            },
                        },
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
                                            'type': 'success',
                                            'variant': 'tonal'
                                        },
                                        'content': [
                                            {
                                                'component': 'span',
                                                'text': '配置教程请参考：'
                                            },
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/CloudAssistant.md',
                                                    'target': '_blank'
                                                },
                                                'text': 'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/CloudAssistant.md'
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "dialog_closed",
                            "max-width": "65rem",
                            "overlay-class": "v-dialog--scrollable v-overlay--scroll-blocked",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t"
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "监控路径配置"
                                },
                                "content": [
                                    {
                                        "component": "VDialogCloseBtn",
                                        "props": {
                                            "model": "dialog_closed"
                                        }
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        "content": [
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
                                                                'component': 'VAceEditor',
                                                                'props': {
                                                                    'modelvalue': 'dir_confs',
                                                                    'lang': 'json',
                                                                    'theme': 'monokai',
                                                                    'style': 'height: 30rem',
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
                                                                    'variant': 'tonal'
                                                                },
                                                                'content': [
                                                                    {
                                                                        'component': 'span',
                                                                        'text': '注意：只有正确配置时，该助手才能正常工作。'
                                                                    }
                                                                ]
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
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
            "invalid": False,
            "refresh": False,
            "only_media": False,
            "clean": False,
            "exclude_keywords": "",
            "cron": "",
            "invalid_cron": "",
            "dir_confs": json.dumps(CloudAssistant.example, indent=4, ensure_ascii=False),
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照时间倒序
        historys = sorted(historys, key=lambda x: x.get("time") or 0, reverse=True)

        msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("time")
                    },
                    {
                        'component': 'td',
                        'text': history.get("file_path")
                    },
                    {
                        'component': 'td',
                        'text': history.get("target_cloud_file")
                    },
                    {
                        'component': 'td',
                        'text': history.get("target_soft_file")
                    },
                    {
                        'component': 'td',
                        'text': history.get("delete_local")
                    },
                    {
                        'component': 'td',
                        'text': history.get("delete_history")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
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
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': 'time'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '本地文件'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '云盘挂载文件'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '软连接文件'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '是否删除本地文件'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '是否删除历史记录'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
