import re
import urllib.parse
from pathlib import Path

from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple
from app.log import logger


class StrmConvert(_PluginBase):
    # 插件名称
    plugin_name = "Strm文件模式转换"
    # 插件描述
    plugin_desc = "Strm文件内容转为本地路径或者cd2/alist API路径。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/convert.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmconvert_"
    # 加载顺序
    plugin_order = 27
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _to_local = False
    _to_api = False
    _convert_confs = None
    _library_path = None
    _api_url = None

    def init_plugin(self, config: dict = None):
        if config:
            self._to_local = config.get("to_local")
            self._to_api = config.get("to_api")
            self._convert_confs = config.get("convert_confs")

            if self._to_local and self._to_api:
                logger.error(f"本地模式和API模式同时只能开启一个")
                return

            convert_confs = self._convert_confs.split("\n")
            if not convert_confs:
                return

            self.update_config({
                "to_local": False,
                "to_api": False,
                "convert_confs": self._convert_confs
            })

            if self._to_local:
                self.__convert_to_local(convert_confs)

            if self._to_api:
                self.__convert_to_api(convert_confs)

    def __convert_to_local(self, convert_confs: list):
        """
        转为本地模式
        """
        for convert_conf in convert_confs:
            if str(convert_conf).count("#") != 1:
                logger.error(f"转换配置 {convert_conf} 格式错误，已跳过处理")
                continue
            source_path = str(convert_conf).split("#")[0]
            library_path = str(convert_conf).split("#")[1]
            logger.info(f"{source_path} 开始转为本地模式")
            self.__to_local(source_path, library_path)
            logger.info(f"{source_path} 转换本地模式已结束")

    def __to_local(self, source_path: str, library_path: str):
        files = self.__list_files(Path(source_path), ['.strm'])
        for f in files:
            logger.debug(f"开始处理文件 {f}")
            try:
                with open(f, 'r') as file:
                    content = file.read()
                    # 获取扩展名
                    ext = str(content).split(".")[-1]
                    library_file = str(f).replace(source_path, library_path)
                    library_file = Path(library_file).parent.joinpath(Path(library_file).stem + "." + ext)
                    with open(f, 'w') as file2:
                        logger.debug(f"开始写入 媒体库路径 {library_file}")
                        file2.write(str(library_file))
            except Exception as e:
                print(e)

    def __convert_to_api(self, convert_confs: list):
        """
        转为api模式
        """
        for convert_conf in convert_confs:
            if str(convert_conf).count("#") != 3:
                logger.error(f"转换配置 {convert_conf} 格式错误，已跳过处理")
                continue
            source_path = str(convert_conf).split("#")[0]
            library_path = str(convert_conf).split("#")[1]
            cloud_type = str(convert_conf).split("#")[2]
            cloud_url = str(convert_conf).split("#")[3]
            logger.info(f"{source_path} 开始转为API模式")
            self.__to_api(source_path, library_path, cloud_type, cloud_url)
            logger.info(f"{source_path} 转换本地模式已结束")

    def __to_api(self, source_path: str, library_path: str, cloud_type: str, cloud_url: str):
        files = self.__list_files(Path(source_path), ['.strm'])
        for f in files:
            logger.debug(f"开始处理文件 {f}")
            try:
                library_file = str(f).replace(source_path, library_path)
                # 对盘符之后的所有内容进行url转码
                library_file = urllib.parse.quote(library_file, safe='')

                if str(cloud_type) == "cd2":
                    # 将路径的开头盘符"/mnt/user/downloads"替换为"http://localhost:19798/static/http/localhost:19798/False/"
                    # http://192.168.31.103:19798/static/http/192.168.31.103:19798/False/%2F115%2Femby%2Fanime%2F%20%E4%B8%83%E9%BE%99%E7%8F%A0%20%281986%29%2FSeason%201.%E5%9B%BD%E8%AF%AD%2F%E4%B8%83%E9%BE%99%E7%8F%A0%20-%20S01E002%20-%201080p%20AAC%20h264.mp4
                    api_file = f"http://{cloud_url}/static/http/{cloud_url}/False/{library_file}"
                else:
                    api_file = f"http://{cloud_url}/d/{library_file}"
                with open(f, 'w') as file2:
                    logger.debug(f"开始写入 api路径 {api_file}")
                    file2.write(str(api_file))
            except Exception as e:
                print(e)

    @staticmethod
    def __list_files(directory: Path, extensions: list, min_filesize: int = 0) -> List[Path]:
        """
        获取目录下所有指定扩展名的文件（包括子目录）
        """
        if not min_filesize:
            min_filesize = 0

        if not directory.exists():
            return []

        if directory.is_file():
            return [directory]

        if not min_filesize:
            min_filesize = 0

        files = []
        pattern = r".*(" + "|".join(extensions) + ")$"

        # 遍历目录及子目录
        for path in directory.rglob('**/*'):
            if path.is_file() \
                    and re.match(pattern, path.name, re.IGNORECASE) \
                    and path.stat().st_size >= min_filesize * 1024 * 1024:
                files.append(path)

        return files

    def get_state(self) -> bool:
        return False

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
                                            'model': 'to_local',
                                            'label': '转为本地模式',
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
                                            'model': 'to_api',
                                            'label': '转为API模式',
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
                                            'model': 'convert_confs',
                                            'label': '转换配置',
                                            'rows': 3,
                                            'placeholder': 'strm文件根路径#转换路径'
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
                                            'text': '转换配置（转为本地模式）：'
                                                    'strm文件根路径#转换路径。'
                                                    '转换路径为源文件挂载进媒体服务器的路径。'
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
                                            'text': '转换配置（转为API模式）：'
                                                    'strm文件根路径#转换路径#cd2/alist#cd2/alist服务地址(ip:port)。'
                                                    '转换路径为云盘根路径。'
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
                                                    'https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/plugins_record/PluginAutoUpdate.md'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "to_local": False,
            "to_api": False,
            "convert_confs": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
