import datetime
import os
import re
import shutil
import threading
import time
import traceback
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
from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey
from app.utils.system import SystemUtils

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
    plugin_desc = "定时移动到云盘，软连接回本地，定时清理无效软连接"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/cloudassistant.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudassistant_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 2

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
    _copy_files = False
    _cron = None
    _clean = False
    # 模式 compatibility/fast
    _mode = "fast"
    # 转移方式
    _transfer_type = "link"
    _monitor_dirs = ""
    _exclude_keywords = ""
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _softdirconf: Dict[str, Optional[str]] = {}
    _historyconf: Dict[str, Optional[bool]] = {}

    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        # 清空配置
        self._dirconf = {}
        self._softdirconf = {}
        self._transferconf = {}
        self._historyconf = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._clean = config.get("clean")
            self._copy_files = config.get("copy_files")
            self._mode = config.get("mode")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._cron = config.get("cron")
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            # 清理插件历史
            if self._clean:
                self.del_data(key="history")
                self._clean = False
                self.__update_config()

            # 停止现有任务
            self.stop_service()

            if self._enabled or self._onlyonce:
                # 定时服务管理器
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 读取目录配置
                monitor_dirs = self._monitor_dirs.split("\n")
                if not monitor_dirs:
                    return
                for mon_path in monitor_dirs:
                    # 格式  本地媒体路径:云盘挂载本地路径$软连接回本地路径%True/False#转移方式
                    # /mnt/meida:/mnt/cloud/115/emby$/mnt/softlink%True#
                    if not mon_path:
                        continue

                    # 自定义转移方式
                    _transfer_type = self._transfer_type
                    if mon_path.count("#") == 1:
                        _transfer_type = mon_path.split("#")[1]
                        mon_path = mon_path.split("#")[0]

                    # 转移完是否删除历史记录
                    _history = False
                    if mon_path.count("%") == 1:
                        _history = mon_path.split("%")[1]
                        _history = True if _history == "True" else False
                        mon_path = mon_path.split("%")[0]

                    # 软连接回本地路径
                    _soft_path = None
                    if mon_path.count("$") == 1:
                        _soft_path = mon_path.split("$")[1]
                        mon_path = mon_path.split("$")[0]

                    # 存储目的目录
                    if SystemUtils.is_windows():
                        if mon_path.count(":") > 1:
                            paths = [mon_path.split(":")[0] + ":" + mon_path.split(":")[1],
                                     mon_path.split(":")[2] + ":" + mon_path.split(":")[3]]
                        else:
                            paths = [mon_path]
                    else:
                        paths = mon_path.split(":")

                    # 目的目录
                    target_path = None
                    if len(paths) > 1:
                        mon_path = paths[0]
                        target_path = Path(paths[1])
                        self._dirconf[mon_path] = target_path
                    else:
                        self._dirconf[mon_path] = None

                    # 转移方式
                    self._transferconf[mon_path] = _transfer_type
                    # 软连接回本地路径
                    self._softdirconf[mon_path] = _soft_path
                    # 是否删除历史记录
                    self._historyconf[mon_path] = _history

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
                            if self._mode == "compatibility":
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
            "copy_files": self._copy_files,
            "clean": self._clean,
            "mode": self._mode,
            "transfer_type": self._transfer_type,
            "monitor_dirs": self._monitor_dirs,
            "exclude_keywords": self._exclude_keywords,
            "cron": self._cron,
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
            # 遍历目录下所有文件
            for root, dirs, files in os.walk(mon_path):
                for name in dirs + files:
                    path = os.path.join(root, name)
                    if Path(path).is_file():
                        self.__handle_file(event_path=str(path), mon_path=mon_path)
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

                # 查询转移目的目录
                target: Path = self._dirconf.get(mon_path)
                # 查询转移方式
                transfer_type = self._transferconf.get(mon_path)
                # 软连接回本地路径
                soft_path = self._softdirconf.get(mon_path)
                # 是否删除历史记录
                history_type = self._historyconf.get(mon_path)

                # 1、转移到云盘挂载路径
                target_cloud_file = str(file_path).replace(str(mon_path), str(target))
                retcode = self.__transfer_file(file_path=file_path, target_file=target_cloud_file,
                                               transfer_type=transfer_type)

                # 2、软连接回本地路径
                if retcode == 0:
                    if not Path(target_cloud_file).exists():
                        logger.info(f"目标文件 {target_cloud_file} 不存在，不创建软连接")
                        return
                    target_soft_file = str(target_cloud_file).replace(str(target), str(soft_path))
                    # 媒体文件软连接
                    if Path(target_soft_file).suffix.lower() in [ext.strip() for ext in
                                                                 self._rmt_mediaext.split(",")]:
                        retcode = self.__transfer_file(file_path=target_cloud_file, target_file=target_soft_file,
                                                       transfer_type="softlink")
                    else:
                        # 非媒体文件可选择复制
                        if self._copy_files:
                            # 其他nfo、jpg等复制文件
                            shutil.copy2(str(file_path), target_soft_file)
                            logger.info(f"复制其他文件 {str(file_path)} 到 {target_soft_file}")

                    if retcode == 0:
                        # 是否删除本地历史
                        if history_type:
                            transferhis = self.transferhis.get_by_src(str(file_path))
                            if transferhis:
                                self.transferhis.delete(transferhis.id)
                                logger.info(f"删除本地历史记录：{transferhis.id}")

                        # 3、存操作记录
                        history = self.get_data('history') or []
                        history.append({
                            "file_path": file_path,
                            "transfer_type": transfer_type,
                            "target_cloud_file": target_cloud_file,
                            "target_soft_file": target_soft_file,
                            "delete_history": history_type,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                        })
                        # 保存历史
                        self.save_data(key="history", value=history)

                # 移动模式删除空目录
                if transfer_type == "move":
                    for file_dir in file_path.parents:
                        if len(str(file_dir)) <= len(str(Path(mon_path))):
                            # 重要，删除到监控目录为止
                            break
                        files = SystemUtils.list_files(file_dir, settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT)
                        if not files:
                            logger.warn(f"移动模式，删除空目录：{file_dir}")
                            shutil.rmtree(file_dir, ignore_errors=True)

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def __transfer_file(self, file_path, target_file, transfer_type):
        """
        转移文件
        """
        logger.info(f"开始{transfer_type}文件 {str(file_path)} 到 {target_file}")
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

            # 媒体文件软连接
            retcode, retmsg = self.__transfer_command(file_path, Path(target_file), transfer_type)
            logger.info(
                f"媒体文件{str(file_path)} {transfer_type} 到 {target_file} {retcode} {retmsg}")
            return retcode

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
            elif transfer_type == 'rclone_move':
                # Rclone 移动
                retcode, retmsg = SystemUtils.rclone_move(file_item, target_file)
            elif transfer_type == 'rclone_copy':
                # Rclone 复制
                retcode, retmsg = SystemUtils.rclone_copy(file_item, target_file)
            else:
                # 复制
                retcode, retmsg = SystemUtils.copy(file_item, target_file)

        if retcode != 0:
            logger.error(retmsg)

        return retcode

    @staticmethod
    def is_broken_symlink(path):
        return os.path.islink(path) and not os.path.exists(path)

    def scan_and_remove_broken_symlinks(self, directory):
        for root, dirs, files in os.walk(directory):
            for name in dirs + files:
                path = os.path.join(root, name)
                if self.is_broken_symlink(path):
                    print(f"Removing broken symlink: {path}")
                    os.remove(path)

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
                                    'md': 4
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
                                            'model': 'copy_files',
                                            'label': '复制非媒体文件',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'mode',
                                            'label': '监控模式',
                                            'items': [
                                                {'title': '兼容模式', 'value': 'compatibility'},
                                                {'title': '性能模式', 'value': 'fast'}
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
                                            'model': 'transfer_type',
                                            'label': '整理方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '软链接', 'value': 'softlink'},
                                                {'title': 'Rclone复制', 'value': 'rclone_copy'},
                                                {'title': 'Rclone移动', 'value': 'rclone_move'}
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
                                            'model': 'cron',
                                            'label': '定时全量同步周期',
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
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_dirs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '本地媒体路径:云盘挂载本地路径$软连接回本地路径%是否删除转移历史记录True/False#转移方式'
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
                                            'text': '如未开启转移，则不会从本地媒体路径转移到云盘挂载本地路径，仅会进行软连接操作。'
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
            "copy_files": True,
            "clean": False,
            "mode": "fast",
            "transfer_type": "link",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "cron": "",
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
                        'text': history.get("transfer_type")
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
                                                'text': '转移方式'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '云盘文件'
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
