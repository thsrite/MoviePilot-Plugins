import shutil
import time
from pathlib import Path
from typing import Any, List, Dict, Tuple

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaImageType, NotificationType, MediaType
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils


class CloudSyncDel(_PluginBase):
    # 插件名称
    plugin_name = "云盘同步删除"
    # 插件描述
    plugin_desc = "媒体库删除软连接/strm文件后，同步删除云盘文件。"
    # 插件图标
    plugin_icon = "clouddisk.png"
    # 插件版本
    plugin_version = "1.5.10"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudsyncdel_"
    # 加载顺序
    plugin_order = 9
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _paths = {}
    _cloud_paths = {}
    _local_paths = {}
    _notify = False
    _url = None
    _del_history = False

    _video_formats = ('.mp4', '.avi', '.rmvb', '.wmv', '.mov', '.mkv', '.flv', '.ts', '.webm', '.iso', '.mpg')

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._url = config.get("url")
            self._del_history = config.get("del_history")
            if config.get("path"):
                for path in str(config.get("path")).split("\n"):
                    paths = path.split("#")[0]
                    cloud_path = path.split("#")[1]
                    self._paths[paths.split(":")[0]] = paths.split(":")[1]
                    self._cloud_paths[paths.split(":")[1]] = cloud_path
            if config.get("local_path"):
                for path in str(config.get("local_path")).split("\n"):
                    self._local_paths[path.split(":")[0]] = path.split(":")[1]

            # 清理插件历史
            if self._del_history:
                self.del_data(key="history")
                self.update_config({
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "path": config.get("path"),
                    "url": self._url,
                    "del_history": False
                })

    @eventmanager.register(EventType.PluginAction)
    def clouddisk_del(self, event: Event = None):
        if not self._enabled:
            return
        if not event:
            return

        event_data = event.event_data
        if not event_data or (
                event_data.get("action") != "networkdisk_del" and event_data.get("action") != "cloudsyncdel"):
            return

        logger.info(f"接收到云盘删除请求 {event_data}")

        media_path = event_data.get("media_path")
        if not media_path:
            logger.error("未获取到删除媒体路径，跳过处理")
            return

        media_name = event_data.get("media_name")
        tmdb_id = event_data.get("tmdb_id")
        media_type = event_data.get("media_type")
        season_num = event_data.get("season_num")
        episode_num = event_data.get("episode_num")

        # 本地路径替换
        local_path = self.__get_path(self._local_paths, media_path)
        logger.info(f"获取到 {self._local_paths} 替换后本地文件路径 {local_path}")

        is_local = False
        if Path(local_path).exists() and (
                Path(local_path).is_dir() or (Path(local_path).is_file() and not Path(local_path).is_symlink())):
            if Path(local_path).is_dir():
                shutil.rmtree(local_path)
            elif Path(local_path).is_file():
                Path(local_path).unlink()  # 删除文件
            logger.info(f"获取到本地路径 {local_path}, 通知媒体库同步删除插件删除")
            self.eventmanager.send_event(EventType.PluginAction, {
                'media_type': media_type,
                'media_name': media_name,
                'media_path': local_path,
                'tmdb_id': tmdb_id,
                'season_num': season_num,
                'episode_num': episode_num,
                'action': 'media_sync_del'
            })
            is_local = True
        else:
            if Path(local_path).parent.exists():
                if Path(local_path).is_dir() and Path(local_path).exists():
                    shutil.rmtree(local_path)
                    logger.info(f"本地目录 {local_path} 已删除")
                    logger.info(f"获取到本地路径 {local_path}, 通知媒体库同步删除插件删除")
                    self.eventmanager.send_event(EventType.PluginAction, {
                        'media_type': media_type,
                        'media_name': media_name,
                        'media_path': local_path,
                        'tmdb_id': tmdb_id,
                        'season_num': season_num,
                        'episode_num': episode_num,
                        'action': 'media_sync_del'
                    })
                    is_local = True
                else:
                    if Path(local_path).is_file():
                        # 检索相同目录下同名的媒体文件
                        pattern = Path(local_path).stem.replace('[', '?').replace(']', '?')
                        logger.info(f"开始筛选 {Path(local_path).parent} 下同名文件 {pattern}")
                        files = Path(local_path).parent.glob(f"{pattern}.*")

                        if not files:
                            logger.info(f"未找到本地同名文件 {pattern}，开始删除云盘")
                        else:
                            for file in files:
                                is_local = True
                                Path(file).unlink()
                                logger.info(f"本地文件 {file} 已删除")
                                if Path(file).suffix in settings.RMT_MEDIAEXT:
                                    logger.info(f"获取到本地路径 {file}, 通知媒体库同步删除插件删除")
                                    self.eventmanager.send_event(EventType.PluginAction, {
                                        'media_type': media_type,
                                        'media_name': media_name,
                                        'media_path': str(file),
                                        'tmdb_id': tmdb_id,
                                        'season_num': season_num,
                                        'episode_num': episode_num,
                                        'action': 'media_sync_del'
                                    })

                            # 删除thumb图片
                            thumb_file = Path(local_path).parent / (Path(local_path).stem + "-thumb.jpg")
                            if thumb_file.exists():
                                thumb_file.unlink()
                                logger.info(f"本地文件 {thumb_file} 已删除")

                            # 删除空目录
                            # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
                            if not SystemUtils.exits_files(local_path.parent, settings.RMT_MEDIAEXT):
                                # 判断父目录是否为空, 为空则删除
                                for parent_path in local_path.parents:
                                    if str(parent_path.parent) != str(local_path.root):
                                        # 父目录非根目录，才删除父目录
                                        if not SystemUtils.exits_files(parent_path, settings.RMT_MEDIAEXT):
                                            # 当前路径下没有媒体文件则删除
                                            shutil.rmtree(parent_path)
                                            logger.warn(f"本地目录 {parent_path} 已删除")

        # 本地文件不继续处理
        if is_local:
            return

        media_path = self.__get_path(self._paths, media_path)
        if not media_path:
            return
        logger.info(f"获取到 {self._paths} 替换后本地路径 {media_path}")

        # 判断文件是否存在
        cloud_file_flag = False
        media_path = Path(media_path)
        cloud_path = None
        if media_path.suffix:
            # 删除云盘文件
            cloud_file = self.__get_path(self._cloud_paths, str(media_path))
            logger.info(f"获取到 {self._cloud_paths} 替换后云盘文件路径 {cloud_file}")

            cloud_file_path = Path(cloud_file)
            # 删除文件、nfo、jpg等同名文件
            pattern = cloud_file_path.stem.replace('[', '?').replace(']', '?')
            logger.info(f"开始筛选 {cloud_file_path.parent} 下同名文件 {pattern}")
            files = cloud_file_path.parent.glob(f"{pattern}.*")
            for file in files:
                Path(file).unlink()
                logger.info(f"云盘文件 {file} 已删除")
                if Path(file).suffix in settings.RMT_MEDIAEXT:
                    cloud_path = file
                cloud_file_flag = True

            # 删除thumb图片
            thumb_file = cloud_file_path.parent / (cloud_file_path.stem + "-thumb.jpg")
            if thumb_file.exists():
                thumb_file.unlink()
                logger.info(f"云盘文件 {thumb_file} 已删除")

            # 删除空目录
            # 判断当前媒体父路径下是否有媒体文件，如有则无需遍历父级
            if not SystemUtils.exits_files(cloud_file_path.parent, settings.RMT_MEDIAEXT):
                # 判断父目录是否为空, 为空则删除
                for parent_path in cloud_file_path.parents:
                    if str(parent_path.parent) != str(cloud_file_path.root):
                        # 父目录非根目录，才删除父目录
                        if not SystemUtils.exits_files(parent_path, settings.RMT_MEDIAEXT):
                            # 当前路径下没有媒体文件则删除
                            shutil.rmtree(parent_path)
                            logger.warn(f"云盘目录 {parent_path} 已删除")
                            cloud_file_flag = True
        else:
            # 删除云盘文件
            cloud_path = self.__get_path(self._cloud_paths, str(media_path))
            if Path(cloud_path).exists():
                shutil.rmtree(cloud_path)
                logger.warn(f"云盘目录 {cloud_path} 已删除")
                cloud_file_flag = True

        # 发送消息
        image = 'https://emby.media/notificationicon.png'
        media_type = MediaType.MOVIE if media_type in ["Movie", "MOV"] else MediaType.TV

        if cloud_file_flag:
            if self._url:
                if not Path(cloud_path).suffix or Path(cloud_path).suffix in settings.RMT_MEDIAEXT:
                    RequestUtils(content_type="application/json").post(url=self._url, json={
                        "path": str(cloud_path),
                        "type": "del"
                    })

            if self._notify:
                backrop_image = self.chain.obtain_specific_image(
                    mediaid=tmdb_id,
                    mtype=media_type,
                    image_type=MediaImageType.Backdrop,
                    season=season_num,
                    episode=episode_num
                ) or image

                # 类型
                if media_type == MediaType.MOVIE:
                    msg = f'电影 {media_name} {tmdb_id}'
                # 删除电视剧
                elif media_type == MediaType.TV and not season_num and not episode_num:
                    msg = f'剧集 {media_name} {tmdb_id}'
                # 删除季 S02
                elif media_type == MediaType.TV and season_num and (not episode_num or not str(episode_num).isdigit()):
                    msg = f'剧集 {media_name} S{season_num} {tmdb_id}'
                # 删除剧集S02E02
                elif media_type == MediaType.TV and season_num and episode_num and str(episode_num).isdigit():
                    msg = f'剧集 {media_name} S{season_num}E{episode_num} {tmdb_id}'
                else:
                    msg = media_name

                # 发送通知
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="云盘同步删除任务完成",
                    image=backrop_image,
                    text=f"{msg}\n"
                         f"时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"
                )

        # 读取历史记录
        history = self.get_data('history') or []

        # 获取poster
        poster_image = self.chain.obtain_specific_image(
            mediaid=tmdb_id,
            mtype=media_type,
            image_type=MediaImageType.Poster,
        ) or image
        history.append({
            "type": media_type.value,
            "title": media_name,
            "path": str(media_path),
            "season": season_num if season_num and str(season_num).isdigit() else None,
            "episode": episode_num if episode_num and str(episode_num).isdigit() else None,
            "image": poster_image,
            "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
            "unique": f"{media_name} {tmdb_id}"
        })

        # 保存历史
        self.save_data("history", history)

    def __get_path(self, paths, file_path: str):
        """
        路径转换
        """
        if paths and paths.keys():
            for library_path in paths.keys():
                if str(file_path).startswith(str(library_path)):
                    # 替换网盘路径
                    return str(file_path).replace(str(library_path), str(paths.get(str(library_path))))
        # 未匹配到路径，返回原路径
        return file_path

    def delete_history(self, key: str, apikey: str):
        """
        删除同步历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data('history')
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data('history', historys)
        return schemas.Response(success=True, message="删除成功")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloudsyncdel",
            "event": EventType.PluginAction,
            "desc": "云盘同步删除",
            "category": "",
            "data": {
                "action": "cloudsyncdel"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除订阅历史记录"
            }
        ]

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
                                            'label': '开启通知',
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
                                            'model': 'del_history',
                                            'label': '清空历史',
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
                                            'model': 'path',
                                            'rows': '2',
                                            'label': '媒体库路径映射（删除云盘文件）',
                                            'placeholder': '媒体服务器软连接/strm路径:MoviePilot软连接/strm路径#MoviePilot云盘路径（一行一个）'
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
                                            'model': 'local_path',
                                            'rows': '2',
                                            'label': '本地路径映射（回调【媒体文件同步删除】插件删除本地文件）',
                                            'placeholder': '媒体服务器软连接/strm路径:MoviePilot本地文件路径（一行一个）'
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
                                            'placeholder': 'post请求json方式推送path和type(del)字段'
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
                                            'text': '需要开启媒体库删除插件且正确配置排除路径。'
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
                                            'text': '关于路径映射：'
                                                    'emby软连接路径:/data/series/A.mp4,'
                                                    'MoviePilot软连接路径:/mnt/link/series/A.mp4。'
                                                    'MoviePilot云盘路径:/mnt/cloud/series/A.mp4。'
                                                    '路径映射填/data:/mnt/link#/mnt/cloud'
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
            "path": "",
            "url": "",
            "local_path": "",
            "notify": False,
            "del_history": False
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
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
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get('del_time'), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            htype = history.get("type")
            title = history.get("title")
            season = history.get("season")
            episode = history.get("episode")
            image = history.get("image")
            del_time = history.get("del_time")
            unique = history.get("unique")

            if season:
                if episode:
                    sub_contents = [
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'类型：{htype}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'标题：{title}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'季：{season}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'集：{episode}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'时间：{del_time}'
                        }
                    ]
                else:
                    sub_contents = [
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'类型：{htype}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'标题：{title}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'季：{season}'
                        },
                        {
                            'component': 'VCardText',
                            'props': {
                                'class': 'pa-0 px-2'
                            },
                            'text': f'时间：{del_time}'
                        }
                    ]
            else:
                sub_contents = [
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'类型：{htype}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'标题：{title}'
                    },
                    {
                        'component': 'VCardText',
                        'props': {
                            'class': 'pa-0 px-2'
                        },
                        'text': f'时间：{del_time}'
                    }
                ]

            contents.append(
                {
                    'component': 'VCard',
                    'content': [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                'innerClass': 'absolute top-0 right-0',
                            },
                            'events': {
                                'click': {
                                    'api': 'plugin/CloudSyncDel/delete_history',
                                    'method': 'get',
                                    'params': {
                                        'key': unique,
                                        'apikey': settings.API_TOKEN
                                    }
                                }
                            },
                        },
                        {
                            'component': 'div',
                            'props': {
                                'class': 'd-flex justify-space-start flex-nowrap flex-row',
                            },
                            'content': [
                                {
                                    'component': 'div',
                                    'content': [
                                        {
                                            'component': 'VImg',
                                            'props': {
                                                'src': image,
                                                'height': 120,
                                                'width': 80,
                                                'aspect-ratio': '2/3',
                                                'class': 'object-cover shadow ring-gray-500',
                                                'cover': True
                                            }
                                        }
                                    ]
                                },
                                {
                                    'component': 'div',
                                    'content': sub_contents
                                }
                            ]
                        }
                    ]
                }
            )

        return [
            {
                'component': 'div',
                'props': {
                    'class': 'grid gap-3 grid-info-card',
                },
                'content': contents
            }
        ]

    def stop_service(self):
        """
        退出插件
        """
        pass
