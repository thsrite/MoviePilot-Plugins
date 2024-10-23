import datetime
import os
import re
import shutil
import threading
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
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class FileSoftLink(_PluginBase):
    # 插件名称
    plugin_name = "实时软连接"
    # 插件描述
    plugin_desc = "监控目录文件变化，媒体文件软连接，其他文件可选复制。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/softlink.png"
    # 插件版本
    plugin_version = "2.0.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "filesoftlink_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _observer = []
    _enabled = False
    _onlyonce = False
    _copy_files = False
    _cron = None
    _url = None
    _size = 0
    # 模式 compatibility/fast
    _mode = "compatibility"
    _monitor_dirs = ""
    _exclude_keywords = ""
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[Path]] = {}
    _medias = {}

    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._categoryconf = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._copy_files = config.get("copy_files")
            self._mode = config.get("mode")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._cron = config.get("cron")
            self._url = config.get("url")
            self._size = config.get("size") or 0
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

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
                # 格式源目录:目的目录
                if not mon_path:
                    continue

                monitor = None
                if mon_path.count("$") == 1:
                    monitor = str(mon_path.split("$")[1])
                    mon_path = mon_path.split("$")[0]

                category = None
                if mon_path.count("#") == 1:
                    category = str(mon_path.split("#")[1]).split(",")
                    mon_path = mon_path.split("#")[0]

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

                self._categoryconf[mon_path] = category

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_path and target_path.is_relative_to(Path(mon_path)):
                            logger.warn(f"{target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_path} 是下载目录 {mon_path} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    # 异步开启云盘监控
                    self._mode = monitor or self._mode
                    if str(self._mode) != "nomonitor":
                        logger.info(f"异步开启实时软连接链接 {mon_path} {self._mode}，延迟3s启动")
                        self._scheduler.add_job(func=self.start_monitor, trigger='date',
                                                run_date=datetime.datetime.now(
                                                    tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                                name=f"实时软连接 {mon_path}",
                                                kwargs={
                                                    "source_dir": mon_path
                                                })
                    else:
                        logger.info(f"{mon_path} 实时软链接服务已关闭")
            # 运行一次定时服务
            if self._onlyonce:
                logger.info("实时软连接服务启动，立即运行一次")
                self._scheduler.add_job(name="实时软连接", func=self.sync_all, trigger='date',
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

    def start_monitor(self, source_dir: str):
        """
        异步开启实时软链接
        """
        try:
            if str(self._mode) == "compatibility":
                # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                observer = PollingObserver(timeout=10)
            else:
                # 内部处理系统操作类型选择最优解
                observer = Observer(timeout=10)
            self._observer.append(observer)
            observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
            observer.daemon = True
            observer.start()
            logger.info(f"{source_dir} 的实时软链接服务启动")
        except Exception as e:
            err_msg = str(e)
            if "inotify" in err_msg and "reached" in err_msg:
                logger.warn(
                    f"云盘监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                    + """
                                           echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                           echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                           sudo sysctl -p
                                           """)
            else:
                logger.error(f"{source_dir} 启动云盘监控失败：{err_msg}")
            self.systemmessage.put(f"{source_dir} 启动云盘监控失败：{err_msg}")

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "copy_files": self._copy_files,
            "mode": self._mode,
            "monitor_dirs": self._monitor_dirs,
            "exclude_keywords": self._exclude_keywords,
            "cron": self._cron,
            "url": self._url,
            "size": self._size,
            "rmt_mediaext": self._rmt_mediaext
        })

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "softlink_sync":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始同步监控目录 ...",
                              userid=event.event_data.get("user"))
        self.sync_all()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="监控目录同步完成！", userid=event.event_data.get("user"))

    @eventmanager.register(EventType.PluginAction)
    def softlink_file(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "softlink_file":
                return
            file_path = event_data.get("file_path")
            if not file_path:
                logger.error(f"缺少参数：{event_data}")
                return

            # 遍历所有监控目录
            mon_path = None
            for mon in self._dirconf.keys():
                if str(file_path).startswith(mon):
                    mon_path = mon
                    break

            if not mon_path:
                logger.error(f"未找到文件 {file_path} 对应的监控目录")
                return

            # 处理单文件
            self.__handle_file(event_path=file_path, mon_path=mon_path)

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or (
                    event_data.get("action") != "softlink_one" and event_data.get("action") != "softlink_all"):
                return
            args = event_data.get("arg_str")
            if not args:
                logger.error(f"缺少参数：{event_data}")
                return
            all_args = args

            # 使用正则表达式匹配
            category = None
            args_arr = args.split(maxsplit=1)
            limit = None
            if len(args_arr) == 2:
                category = args_arr[0]
                args = args_arr[1]
                if str(args).isdigit():
                    limit = int(args)

            if category:
                # 判断是不是目录
                if Path(category).is_dir() and Path(category).exists() and limit is not None:
                    # 遍历所有监控目录
                    mon_path = None
                    for mon in self._dirconf.keys():
                        if str(category).startswith(mon):
                            mon_path = mon
                            break

                    # 指定路径软连接
                    if not mon_path:
                        logger.error(f"未找到 {category} 对应的监控目录")
                        self.post_message(channel=event.event_data.get("channel"),
                                          title=f"未找到 {category} 对应的监控目录",
                                          userid=event.event_data.get("user"))
                        return

                    self.__handle_limit(path=category, mon_path=mon_path, limit=limit, event=event)
                    return
                else:
                    for mon_path in self._categoryconf.keys():
                        mon_category = self._categoryconf.get(mon_path)
                        logger.info(f"开始检查 {mon_path} {mon_category}")
                        if mon_category and str(category) in mon_category:
                            parent_path = os.path.join(mon_path, category)
                            if limit:
                                logger.info(f"获取到 {category} 对应的监控目录 {parent_path}")
                                self.__handle_limit(path=parent_path, mon_path=mon_path, limit=limit, event=event)
                            else:
                                logger.info(f"获取到 {category} {args} 对应的监控目录 {parent_path}")
                                target_path = os.path.join(str(parent_path), args)
                                logger.info(f"开始处理 {target_path}")
                                target_paths = self.__find_related_paths(os.path.join(str(parent_path), args))
                                if not target_paths:
                                    logger.error(f"未查找到 {category} {args} 对应的具体目录")
                                    self.post_message(channel=event.event_data.get("channel"),
                                                      title=f"未查找到 {category} {args} 对应的具体目录",
                                                      userid=event.event_data.get("user"))
                                    return
                                for target_path in target_paths:
                                    logger.info(f"开始定向处理文件夹 ...{target_path}")
                                    for sroot, sdirs, sfiles in os.walk(target_path):
                                        for file_name in sdirs + sfiles:
                                            src_file = os.path.join(sroot, file_name)
                                            if Path(src_file).is_file():
                                                self.__handle_file(event_path=str(src_file), mon_path=mon_path)

                                    if event.event_data.get("user"):
                                        self.post_message(channel=event.event_data.get("channel"),
                                                          title=f"{target_path} 软连接完成！",
                                                          userid=event.event_data.get("user"))

                                    if limit is None and event_data and event_data.get("action") == "softlink_one":
                                        return
                            return
            else:
                # 遍历所有监控目录
                mon_path = None
                for mon in self._dirconf.keys():
                    if str(args).startswith(mon):
                        mon_path = mon
                        break

                # 指定路径软连接
                if mon_path:
                    if not Path(args).exists():
                        logger.info(f"同步路径 {args} 不存在")
                        return
                    # 处理单文件
                    if Path(args).is_file():
                        self.__handle_file(event_path=str(args), mon_path=mon_path)
                        return
                    else:
                        # 处理指定目录
                        logger.info(f"获取到 {args} 对应的监控目录 {mon_path}")

                        logger.info(f"开始定向处理文件夹 ...{args}")
                        for sroot, sdirs, sfiles in os.walk(args):
                            for file_name in sdirs + sfiles:
                                src_file = os.path.join(sroot, file_name)
                                if Path(str(src_file)).is_file():
                                    self.__handle_file(event_path=str(src_file), mon_path=mon_path)
                        if event.event_data.get("user"):
                            self.post_message(channel=event.event_data.get("channel"),
                                              title=f"{all_args} 软连接完成！", userid=event.event_data.get("user"))
                        return
                else:
                    for mon_path in self._categoryconf.keys():
                        mon_category = self._categoryconf.get(mon_path)
                        logger.info(f"开始检查 {mon_path} {mon_category}")
                        if mon_category and str(args) in mon_category:
                            parent_path = os.path.join(mon_path, args)
                            logger.info(f"获取到 {args} 对应的监控目录 {parent_path}")
                            for sroot, sdirs, sfiles in os.walk(parent_path):
                                for file_name in sdirs + sfiles:
                                    src_file = os.path.join(sroot, file_name)
                                    if Path(str(src_file)).is_file():
                                        self.__handle_file(event_path=str(src_file), mon_path=mon_path)
                            if event.event_data.get("user"):
                                self.post_message(channel=event.event_data.get("channel"),
                                                  title=f"{all_args} 软连接完成！",
                                                  userid=event.event_data.get("user"))
                            return
            if event.event_data.get("user"):
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"{all_args} 未检索到，请检查输入是否正确！",
                                  userid=event.event_data.get("user"))

    def __handle_limit(self, path, limit, mon_path, event):
        """
        处理文件数量限制
        """
        sub_paths = []
        for entry in os.listdir(path):
            full_path = os.path.join(path, entry)
            if os.path.isdir(full_path):
                sub_paths.append(full_path)

        if not sub_paths:
            logger.error(f"未找到 {path} 目录下的文件夹")
            return

        # 按照修改时间倒序排列
        sub_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        logger.info(f"开始定向处理文件夹 ...{path}, 最新 {limit} 个文件夹")
        for sub_path in sub_paths[:limit]:
            logger.info(f"开始定向处理文件夹 ...{sub_path}")
            for sroot, sdirs, sfiles in os.walk(sub_path):
                for file_name in sdirs + sfiles:
                    src_file = os.path.join(sroot, file_name)
                    if Path(src_file).is_file():
                        self.__handle_file(event_path=str(src_file), mon_path=mon_path)
            if event.event_data.get("user"):
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"{sub_path} 软连接完成！", userid=event.event_data.get("user"))

    @staticmethod
    def __find_related_paths(base_path):
        related_paths = []
        base_dir = os.path.dirname(base_path)
        base_name = os.path.basename(base_path)

        for entry in os.listdir(base_dir):
            if entry.startswith(base_name):
                full_path = os.path.join(base_dir, entry)
                if os.path.isdir(full_path):
                    related_paths.append(full_path)

        # 按照修改时间倒序排列
        related_paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)

        return related_paths

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for root, dirs, files in os.walk(mon_path):
                for name in dirs + files:
                    path = os.path.join(root, name)
                    if Path(path).is_file():
                        self.__handle_file(event_path=str(path), mon_path=mon_path)
        logger.info("全量同步监控目录完成！")

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

                # 判断文件大小
                if self._size and float(self._size) > 0 and file_path.stat().st_size < float(self._size) * 1024 ** 3:
                    logger.info(f"{file_path} 文件大小小于监控文件大小，不处理")
                    return

                # 查询转移目的目录
                target: Path = self._dirconf.get(mon_path)
                if not target:
                    logger.info(f"{mon_path} 没有配置转移目的目录，不处理")
                    return

                target_file = str(file_path).replace(str(mon_path), str(target))

                # 如果是文件夹
                if Path(target_file).is_dir():
                    if not Path(target_file).exists():
                        logger.info(f"创建目标文件夹 {target_file}")
                        os.makedirs(target_file)
                        return
                else:
                    # 文件
                    if Path(target_file).exists():
                        logger.info(f"目标文件 {target_file} 已存在")
                        return

                    if not Path(target_file).parent.exists():
                        logger.info(f"创建目标文件夹 {Path(target_file).parent}")
                        os.makedirs(Path(target_file).parent)

                    # 媒体文件软连接
                    if Path(target_file).suffix.lower() in [ext.strip() for ext in
                                                            self._rmt_mediaext.split(",")]:
                        retcode, retmsg = SystemUtils.softlink(file_path, Path(target_file))
                        logger.info(f"创建媒体文件软连接 {str(file_path)} 到 {target_file} {retcode} {retmsg}")
                        if self._url and file_path.suffix in settings.RMT_MEDIAEXT:
                            RequestUtils(content_type="application/json").post(url=self._url, json={
                                "path": str(file_path),
                                "type": "add"
                            })
                    else:
                        if self._copy_files:
                            # 其他nfo、jpg等复制文件
                            shutil.copy2(str(file_path), target_file)
                            logger.info(f"复制其他文件 {str(file_path)} 到 {target_file}")
        except Exception as e:
            logger.error("软连接发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [
            {
                "cmd": "/softlink_sync",
                "event": EventType.PluginAction,
                "desc": "文件软连接同步",
                "category": "",
                "data": {
                    "action": "softlink_sync"
                }
            },
            {
                "cmd": "/soft",
                "event": EventType.PluginAction,
                "desc": "定向软连接处理",
                "category": "",
                "data": {
                    "action": "softlink_one"
                }
            },
            {
                "cmd": "/softall",
                "event": EventType.PluginAction,
                "desc": "定向软连接处理",
                "category": "",
                "data": {
                    "action": "softlink_all"
                }
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/softlink_sync",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "实时软连接同步",
            "description": "实时软连接同步",
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
                "id": "FileSoftLink",
                "name": "实时软连接全量同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_all,
                "kwargs": {}
            }]
        return []

    def sync(self) -> schemas.Response:
        """
        API调用目录同步
        """
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
                                                {'title': '性能模式', 'value': 'fast'},
                                                {'title': '不监控', 'value': 'nomonitor'},
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
                                            'model': 'size',
                                            'label': '监控文件大小（GB）',
                                            'placeholder': '0'
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
                                            'model': 'monitor_dirs',
                                            'label': '监控目录',
                                            'rows': 5,
                                            'placeholder': '监控目录:转移目的目录'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'url',
                                            'label': '任务推送url',
                                            'placeholder': 'post请求json方式推送path和type(add)字段'
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '监控文件大小：单位GB，0为不开启，低于监控文件大小的文件不会被监控转移。'
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
            "copy_files": True,
            "mode": "compatibility",
            "monitor_dirs": "",
            "exclude_keywords": "",
            "cron": "",
            "size": 0,
            "url": "",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
        }

    def get_page(self) -> List[dict]:
        pass

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
