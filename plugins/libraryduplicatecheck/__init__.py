from datetime import datetime, timedelta
import os
from collections import defaultdict
from pathlib import Path
import pytz

from app.core.config import settings
from app.modules.emby import Emby
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils


class LibraryDuplicateCheck(_PluginBase):
    # 插件名称
    plugin_name = "媒体库重复媒体检测。"
    # 插件描述
    plugin_desc = "媒体库重复媒体检查，可选择保留规则保留其一。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/libraryduplicate.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "libraryduplicatecheck_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _paths = {}
    _notify = False
    _delete_softlink = False
    _cron = None
    _onlyonce = False
    _retain_type = None
    _rmt_mediaext = ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

    _EMBY_HOST = settings.EMBY_HOST
    _EMBY_APIKEY = settings.EMBY_API_KEY

    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._cron = config.get("cron")
            self._delete_softlink = config.get("delete_softlink")
            self._onlyonce = config.get("onlyonce")
            self._retain_type = config.get("retain_type")
            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            if self._EMBY_HOST:
                if not self._EMBY_HOST.endswith("/"):
                    self._EMBY_HOST += "/"
                if not self._EMBY_HOST.startswith("http"):
                    self._EMBY_HOST = "http://" + self._EMBY_HOST

            if config.get("paths"):
                for path in str(config.get("paths")).split("\n"):
                    if path.count("#") == 1:
                        path = path.split("#")[0]
                        library_name = path.split("#")[1]
                        self._paths[path] = library_name
                    else:
                        self._paths[path] = None

            if self._enabled or self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)

                # 立即运行一次
                if self._onlyonce:
                    logger.info(f"媒体库重复媒体检测服务启动，立即运行一次")
                    self._scheduler.add_job(self.check_duplicate, 'date',
                                            run_date=datetime.now(
                                                tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                            name="媒体库重复媒体检测")
                    # 关闭一次性开关
                    self._onlyonce = False

                    # 保存配置
                    self.__update_config()

                # 周期运行
                if self._cron:
                    try:
                        self._scheduler.add_job(func=self.check_duplicate,
                                                trigger=CronTrigger.from_crontab(self._cron),
                                                name="媒体库重复媒体检测")
                    except Exception as err:
                        logger.error(f"定时任务配置错误：{err}")
                        # 推送实时消息
                        self.systemmessage.put(f"执行周期配置错误：{err}")

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def check_duplicate(self):
        """
        检查媒体库重复媒体
        """
        if not self._paths and not self._paths.keys():
            logger.warning("媒体库重复媒体检测服务未配置路径")
            return

        for path in self._paths.keys():
            logger.info(f"开始检查路径：{path}")
            self.__find_duplicate_videos(path)
            logger.info(f"路径 {path} 检查完毕")

            library_name = self._paths.get(path)
            if library_name:
                logger.info(f"开始刷新媒体库：{library_name}")
                # 获取emby 媒体库
                librarys = Emby().get_librarys()
                if not librarys:
                    logger.error("获取媒体库失败")
                    return

                for library in librarys:
                    if not library:
                        continue
                    if library.name == library_name:
                        logger.info(f"媒体库：{library_name} 刷新完成")
                        result = self.__refresh_emby_library_by_id(library.id)
                        if result:
                            logger.info(f"媒体库：{library_name} 刷新成功")
                        else:
                            logger.error(f"媒体库：{library_name} 刷新失败")
                        break

    def __refresh_emby_library_by_id(self, item_id: str) -> bool:
        """
        通知Emby刷新一个项目的媒体库
        """
        if not self._EMBY_HOST or not self._EMBY_APIKEY:
            return False
        req_url = "%semby/Items/%s/Refresh?Recursive=true&api_key=%s" % (self._EMBY_HOST, item_id, self._EMBY_APIKEY)
        try:
            res = RequestUtils().post_res(req_url)
            if res:
                return True
            else:
                logger.info(f"刷新媒体库对象 {item_id} 失败，无法连接Emby！")
        except Exception as e:
            logger.error(f"连接Items/Id/Refresh出错：" + str(e))
            return False
        return False

    def __find_duplicate_videos(self, directory):
        """
        检查目录下视频文件是否有重复
        """
        # Dictionary to hold the list of files for each video name
        video_files = defaultdict(list)

        # Traverse the directory and subdirectories
        for root, _, files in os.walk(directory):
            for file in files:
                # Check the file extension
                if Path(file).suffix.lower() in [ext.strip() for ext in
                                                 self._rmt_mediaext.split(",")]:
                    video_name = Path(file).stem.split('-')[0].rstrip()
                    logger.info(f'Scan file -> {file} -> {video_name}')
                    video_files[video_name].append(os.path.join(root, file))

        logger.info()
        logger.info("================== RESULT ==================")
        # Find and handle duplicate video files
        for name, paths in video_files.items():
            if len(paths) > 1:
                logger.info(f"Duplicate video files for '{name}':")
                for path in paths:
                    logger.info(f"  {path} 文件大小：{os.path.getsize(path)}，创建时间：{os.path.getmtime(path)}")

                if str(self._retain_type) != "仅检查":
                    # Decide which file to keep based on criteria (e.g., file size or creation date)
                    keep_path = self.__choose_file_to_keep(paths)
                    logger.info(f"文件保留规则：{str(self._retain_type)} Keeping: {keep_path}")
                    # Delete the other duplicate files (if needed)
                    for path in paths:
                        if path != keep_path:
                            cloud_file = os.readlink(path)
                            # Path(path).unlink()
                            logger.info(f"Deleted Local file: {path}")
                            self.__rmtree(Path(path), "监控")

                            # 同步删除软连接源目录
                            if cloud_file and self._delete_softlink:
                                logger.info(f"开始删除云盘文件 {cloud_file}")
                                if Path(cloud_file).exists():
                                    cloud_file_path = Path(cloud_file)
                                    # 删除文件、nfo、jpg等同名文件
                                    pattern = cloud_file_path.stem.replace('[', '?').replace(']', '?')
                                    logger.info(f"开始筛选 {cloud_file_path.parent} 下同名文件 {pattern}")
                                    files = cloud_file_path.parent.glob(f"{pattern}.*")
                                    for file in files:
                                        # Path(file).unlink()
                                        logger.info(f"云盘文件 {file} 已删除")
                                    self.__rmtree(cloud_file_path, "云盘")

            else:
                logger.info(f"'{name}' No Duplicate video files.")

    def __rmtree(self, path: Path, file_type: str):
        """
        删除目录及其子目录
        """
        # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
        if not SystemUtils.exits_files(path.parent, [ext.strip() for ext in
                                                     self._rmt_mediaext.split(",")]):
            # 判断父目录是否为空, 为空则删除
            for parent_path in path.parents:
                if str(parent_path.parent) != str(path.root):
                    # 父目录非根目录，才删除父目录
                    if not SystemUtils.exits_files(parent_path, [ext.strip() for ext in
                                                                 self._rmt_mediaext.split(",")]):
                        # 当前路径下没有媒体文件则删除
                        # shutil.rmtree(parent_path)
                        logger.warn(f"{file_type}目录 {parent_path} 已删除")

    @staticmethod
    def __choose_file_to_keep(paths):
        # Example: Choose based on file size (keeping the smallest)
        smallest_size = float('inf')
        smallest_path = None
        for path in paths:
            file_size = os.path.getmtime(path)
            if file_size < smallest_size:
                smallest_size = file_size
                smallest_path = path
        return smallest_path

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "delete_softlink": self._delete_softlink,
            "notify": self._notify,
            "paths": self._paths,
            "retain_type": self._retain_type,
            "rmt_mediaext": self._rmt_mediaext
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/libraryduplicatecheck",
            "event": EventType.PluginAction,
            "desc": "媒体库重复媒体检测",
            "category": "",
            "data": {
                "action": "libraryduplicatecheck"
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
                                            'model': 'delete_softlink',
                                            'label': '删除软连接源文件',
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
                                    'md': 9
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': False,
                                            'chips': True,
                                            'model': 'retain_type',
                                            'label': '质量',
                                            'items': [
                                                {'title': '仅检查', 'value': '仅检查'},
                                                {'title': '保留体积最小', 'value': '保留体积最小'},
                                                {'title': '保留体积最大', 'value': '保留体积最大'},
                                                {'title': '保留创建最早', 'value': '保留创建最早'},
                                                {'title': '保留创建最晚', 'value': '保留创建最晚'},
                                            ]
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
                                            'model': 'paths',
                                            'label': '检查路径',
                                            'rows': 2,
                                            'placeholder': "检查的媒体路径#媒体库名称\n"
                                                           "检查的媒体路径"
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
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "delete_softlink": False,
            "cron": "5 1 * * *",
            "paths": "",
            "notify": False,
            "retain_type": "仅检查",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
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
