import os
import shutil
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, watching_path: str, file_change: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = watching_path
        self.file_change = file_change

    # def on_any_event(self, event):
    #     logger.info(f"目录监控event_type {event.event_type} 路径 {event.src_path}")

    def on_created(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.dest_path)


class CloudStrmApi(_PluginBase):
    # 插件名称
    plugin_name = "云盘Strm生成（API直链版）"
    # 插件描述
    plugin_desc = "监控文件创建，生成Strm文件。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/create.png"
    # 插件版本
    plugin_version = "2.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrm_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _onlyonce = False
    _relay = 3
    _observer = []
    _video_formats = ('.mp4', '.avi', '.rmvb', '.wmv', '.mov', '.mkv', '.flv', '.ts', '.webm', '.iso', '.mpg', '.m2ts')

    _dirconf = {}
    _modeconf = {}
    _libraryconf = {}
    _cloudtypeconf = {}
    _cloudurlconf = {}
    _cloudpathconf = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._modeconf = {}
        self._libraryconf = {}
        self._cloudtypeconf = {}
        self._cloudurlconf = {}
        self._cloudpathconf = {}

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor_confs = config.get("monitor_confs")
            self._relay = config.get("relay") or 3

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
                # 格式 源目录:目的目录:媒体库内网盘路径:监控模式
                if not monitor_conf:
                    continue
                if str(monitor_conf).count("#") == 3:
                    mode = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    library_dir = str(monitor_conf).split("#")[3]
                    self._libraryconf[source_dir] = library_dir
                elif str(monitor_conf).count("#") == 5:
                    mode = str(monitor_conf).split("#")[0]
                    source_dir = str(monitor_conf).split("#")[1]
                    target_dir = str(monitor_conf).split("#")[2]
                    cloud_type = str(monitor_conf).split("#")[3]
                    cloud_path = str(monitor_conf).split("#")[4]
                    cloud_url = str(monitor_conf).split("#")[5]
                    self._cloudtypeconf[source_dir] = cloud_type
                    self._cloudpathconf[source_dir] = cloud_path
                    self._cloudurlconf[source_dir] = cloud_url
                else:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir
                self._modeconf[source_dir] = mode

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

                    # 异步开启云盘监控
                    logger.info(f"异步开启云盘监控 {source_dir} {mode}")
                    self._scheduler.add_job(func=self.start_monitor, trigger='date',
                                            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                                seconds=int(self._relay)),
                                            name=f"云盘监控 {source_dir}",
                                            kwargs={
                                                "mode": mode,
                                                "source_dir": source_dir
                                            })
            # 运行一次定时服务
            if self._onlyonce:
                logger.info("云盘监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="云盘监控全量执行")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def start_monitor(self, mode: str, source_dir: str):
        """
        异步开启云盘监控
        """
        try:
            if str(mode) == "compatibility":
                # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                observer = PollingObserver(timeout=10)
            else:
                # 内部处理系统操作类型选择最优解
                observer = Observer(timeout=10)
            self._observer.append(observer)
            observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
            observer.daemon = True
            observer.start()
            logger.info(f"{source_dir} 的云盘监控服务启动")
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

        # 文件发生变化
        logger.info(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(event=event, event_path=event_path, source_dir=source_dir)

    def __handle_file(self, event, event_path: str, source_dir: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        """
        try:
            # 转移路径
            dest_dir = self._dirconf.get(source_dir)
            # 媒体库容器内挂载路径
            library_dir = self._libraryconf.get(source_dir)
            # 云服务类型
            cloud_type = self._cloudtypeconf.get(source_dir)
            # 云服务挂载本地跟路径
            cloud_path = self._cloudpathconf.get(source_dir)
            # 云服务地址
            cloud_url = self._cloudurlconf.get(source_dir)
            # 文件夹同步创建
            if event.is_directory:
                target_path = event_path.replace(source_dir, dest_dir)
                # 目标文件夹不存在则创建
                if not Path(target_path).exists():
                    logger.info(f"创建目标文件夹 {target_path}")
                    os.makedirs(target_path)
            else:
                # 文件：nfo、图片、视频文件
                dest_file = event_path.replace(source_dir, dest_dir)
                if Path(dest_file).exists():
                    logger.debug(f"目标文件 {dest_file} 已存在")
                    return

                    # 目标文件夹不存在则创建
                if not Path(dest_file).parent.exists():
                    logger.info(f"创建目标文件夹 {Path(dest_file).parent}")
                    os.makedirs(Path(dest_file).parent)

                # 视频文件创建.strm文件
                if event_path.lower().endswith(self._video_formats):
                    # 如果视频文件小于1MB，则直接复制，不创建.strm文件
                    if os.path.getsize(event_path) < 1024 * 1024:
                        shutil.copy2(event_path, dest_file)
                        logger.info(f"复制视频文件 {event_path} 到 {dest_file}")
                    else:
                        # 创建.strm文件
                        self.__create_strm_file(dest_file=dest_file,
                                                dest_dir=dest_dir,
                                                source_file=event_path,
                                                library_dir=library_dir,
                                                cloud_type=cloud_type,
                                                cloud_path=cloud_path,
                                                cloud_url=cloud_url)
                else:
                    # 其他nfo、jpg等复制文件
                    shutil.copy2(event_path, dest_file)
                    logger.info(f"复制其他文件 {event_path} 到 {dest_file}")

        except Exception as e:
            logger.error(f"event_handler_created error: {e}")
            print(str(e))

    def sync_all(self):
        """
        同步所有文件
        """
        if not self._dirconf or not self._dirconf.keys():
            logger.error("未获取到可用目录监控配置，请检查")
            return
        for source_dir in self._dirconf.keys():
            # 转移路径
            dest_dir = self._dirconf.get(source_dir)
            # 媒体库容器内挂载路径
            library_dir = self._libraryconf.get(source_dir)
            # 云服务类型
            cloud_type = self._cloudtypeconf.get(source_dir)
            # 云服务挂载本地跟路径
            cloud_path = self._cloudpathconf.get(source_dir)
            # 云服务地址
            cloud_url = self._cloudurlconf.get(source_dir)

            logger.info(f"开始初始化生成strm文件 {source_dir}")
            self.__handle_all(source_dir=source_dir,
                              dest_dir=dest_dir,
                              library_dir=library_dir,
                              cloud_type=cloud_type,
                              cloud_path=cloud_path,
                              cloud_url=cloud_url)
            logger.info(f"{source_dir} 初始化生成strm文件完成")

    def __handle_all(self, source_dir, dest_dir, library_dir, cloud_type=None, cloud_path=None, cloud_url=None):
        """
        遍历生成所有文件的strm
        """
        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        for root, dirs, files in os.walk(source_dir):
            # 如果遇到名为'extrafanart'的文件夹，则跳过处理该文件夹，继续处理其他文件夹
            if "extrafanart" in dirs:
                dirs.remove("extrafanart")

            for file in files:
                source_file = os.path.join(root, file)
                logger.info(f"处理源文件::: {source_file}")

                dest_file = os.path.join(dest_dir, os.path.relpath(source_file, source_dir))
                if Path(dest_file).exists():
                    logger.debug(f"目标文件 {dest_file} 已存在")
                    return
                logger.info(f"开始生成目标文件::: {dest_file}")

                # 创建目标目录中缺少的文件夹
                if not os.path.exists(Path(dest_file).parent):
                    os.makedirs(Path(dest_file).parent)

                # 如果目标文件已存在，跳过处理
                if os.path.exists(dest_file):
                    logger.warn(f"文件已存在，跳过处理::: {dest_file}")
                    continue

                if file.lower().endswith(self._video_formats):
                    # 如果视频文件小于1MB，则直接复制，不创建.strm文件
                    if os.path.getsize(source_file) < 1024 * 1024:
                        logger.info(f"视频文件小于1MB的视频文件到:::{dest_file}")
                        shutil.copy2(source_file, dest_file)
                    else:
                        # 创建.strm文件
                        self.__create_strm_file(dest_file=dest_file,
                                                dest_dir=dest_dir,
                                                source_file=source_file,
                                                library_dir=library_dir,
                                                cloud_type=cloud_type,
                                                cloud_path=cloud_path,
                                                cloud_url=cloud_url)
                else:
                    # 复制文件
                    logger.info(f"复制其他文件到:::{dest_file}")
                    shutil.copy2(source_file, dest_file)

    @staticmethod
    def __create_strm_file(dest_file: str, dest_dir: str, source_file: str, library_dir: str = None,
                           cloud_type: str = None, cloud_path: str = None, cloud_url: str = None):
        """
        生成strm文件
        :param library_dir:
        :param dest_dir:
        :param dest_file:
        """
        try:
            # 获取视频文件名和目录
            video_name = Path(dest_file).name
            # 获取视频目录
            dest_path = Path(dest_file).parent

            if not dest_path.exists():
                logger.info(f"创建目标文件夹 {dest_path}")
                os.makedirs(str(dest_path))

            # 构造.strm文件路径
            strm_path = os.path.join(dest_path, f"{os.path.splitext(video_name)[0]}.strm")
            logger.info(f"替换前本地路径:::{dest_file}")

            # 云盘模式
            if cloud_type:
                # 替换路径中的\为/
                dest_file = source_file.replace("\\", "/")
                dest_file = dest_file.replace(cloud_path, "")
                # 对盘符之后的所有内容进行url转码
                dest_file = urllib.parse.quote(dest_file, safe='')
                if str(cloud_type) == "cd2":
                    # 将路径的开头盘符"/mnt/user/downloads"替换为"http://localhost:19798/static/http/localhost:19798/False/"
                    dest_file = f"http://{cloud_url}/static/http/{cloud_url}/False/{dest_file}"
                    logger.info(f"替换后cd2路径:::{dest_file}")
                elif str(cloud_type) == "alist":
                    dest_file = f"http://{cloud_url}/d/{dest_file}"
                    logger.info(f"替换后alist路径:::{dest_file}")
                else:
                    logger.error(f"云盘类型 {cloud_type} 错误")
                    return
            else:
                # 本地挂载路径转为emby路径
                dest_file = dest_file.replace(dest_dir, library_dir)
                logger.info(f"替换后emby容器内路径:::{dest_file}")

            # 写入.strm文件
            with open(strm_path, 'w') as f:
                f.write(dest_file)

            logger.info(f"创建strm文件 {strm_path}")
        except Exception as e:
            logger.error(f"创建strm文件失败")
            print(str(e))

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "relay": self._relay,
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'relay',
                                            'label': '监控延迟',
                                            'placeholder': '3'
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
                                            'placeholder': '监控方式#监控目录#目的目录#媒体服务器内源文件路径'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'straight_chain',
                                            'label': '直链API',
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
                                            'model': 'straight_confs',
                                            'label': '直链配置',
                                            'rows': 5,
                                            'placeholder': '媒体服务器内源文件路径#cd2#cd2挂载本地跟路径#cd2服务地址'
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
                                            'text': '目录监控格式：'
                                                    '1.监控方式#监控目录#目的目录#媒体服务器内源文件路径；'
                                                    '2.监控方式#监控目录#目的目录#cd2#cd2挂载本地跟路径#cd2服务地址；'
                                                    '3.监控方式#监控目录#目的目录#alist#alist挂载本地跟路径#alist服务地址。'
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
                                            'text': '媒体服务器内源文件路径：'
                                                    '源文件目录即云盘挂载到媒体服务器的路径。'
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
                                            'text': '监控方式：'
                                                    'fast:性能模式（快）；'
                                                    'compatibility:兼容模式（稳，推荐）'
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
                                            'text': '立即运行一次：'
                                                    '全量运行一次。'
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
                                            'text': '由于unraid开启云盘监控很慢，所以采取异步方式开启磁盘监控，'
                                                    '具体开启情况可稍等3-5分钟查看日志。'
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
                                                    'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/docs/CloudStrm.md'
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
            "relay": 3,
            "onlyonce": False,
            "monitor_confs": "",
            "straight_chain": False
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
