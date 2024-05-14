import re
import shutil
from pathlib import Path

from fastapi import APIRouter

from app.core.config import settings
from app.core.plugin import PluginManager
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.plugin import PluginHelper
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.scheduler import Scheduler
from app.utils.system import SystemUtils

router = APIRouter()


class PluginReInstall(_PluginBase):
    # 插件名称
    plugin_name = "插件强制重装"
    # 插件描述
    plugin_desc = "卸载当前插件，强制重装。"
    # 插件图标
    plugin_icon = "refresh.png"
    # 插件版本
    plugin_version = "1.6"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "pluginreinstall_"
    # 加载顺序
    plugin_order = 98
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _reload = False
    _plugin_ids = []
    _plugin_url = []
    _proxy_url = ""
    _base_url = "https://raw.githubusercontent.com/%s/%s/main/"

    def init_plugin(self, config: dict = None):
        if config:
            self._reload = config.get("reload")
            self._plugin_ids = config.get("plugin_ids") or []
            if not self._plugin_ids:
                return
            self._plugin_url = config.get("plugin_url")
            self._proxy_url = config.get("proxy_url") or ""

            # 仅重载插件
            if self._reload:
                for plugin_id in self._plugin_ids:
                    # 加载插件到内存
                    PluginManager().reload_plugin(plugin_id)
                    # 注册插件服务
                    Scheduler().update_plugin_job(plugin_id)
                    # 注册插件API
                    self.register_plugin_api(plugin_id)
                    logger.info(f"插件 {plugin_id} 热重载成功")
                self.__update_conifg()
            else:
                # 校验插件仓库格式
                plugin_url = None
                if self._plugin_url:
                    pattern = "https://github.com/(.*?)/(.*?)/"
                    matches = re.findall(pattern, str(self._plugin_url))
                    if not matches:
                        logger.warn(f"指定插件仓库地址 {self._plugin_url} 错误，将使用插件默认地址重装")
                        self._plugin_url = ""

                    user, repo = PluginHelper().get_repo_info(self._plugin_url)
                    plugin_url = self._base_url % (user, repo)

                self.__update_conifg()

                # 本地插件
                local_plugins = self.get_local_plugins()

                # 开始重载插件
                for plugin_id in list(local_plugins.keys()):
                    local_plugin = local_plugins.get(plugin_id)
                    if plugin_id in self._plugin_ids:
                        logger.info(
                            f"开始重载插件 {local_plugin.get('plugin_name')} v{local_plugin.get('plugin_version')}")

                        # 开始安装线上插件
                        state, msg = self.install(pid=plugin_id,
                                                  repo_url=plugin_url or local_plugin.get("repo_url"))
                        # 安装失败
                        if not state:
                            logger.error(
                                f"插件 {local_plugin.get('plugin_name')} 重装失败，当前版本 v{local_plugin.get('plugin_version')}")
                            continue

                        logger.info(
                            f"插件 {local_plugin.get('plugin_name')} 重装成功，当前版本 v{local_plugin.get('plugin_version')}")

                        # 加载插件到内存
                        PluginManager().reload_plugin(plugin_id)
                        # 注册插件服务
                        Scheduler().update_plugin_job(plugin_id)
                        # 注册插件API
                        self.register_plugin_api(plugin_id)

    def __update_conifg(self):
        self.update_config({
            "reload": self._reload,
            "plugin_url": self._plugin_url,
            "proxy_url": self._proxy_url
        })

    def install(self, pid: str, repo_url: str) -> Tuple[bool, str]:
        """
        安装插件
        """
        if SystemUtils.is_frozen():
            return False, "可执行文件模式下，只能安装本地插件"

        # 从Github的repo_url获取用户和项目名
        user, repo = PluginHelper().get_repo_info(repo_url)
        if not user or not repo:
            return False, "不支持的插件仓库地址格式"

        def __get_filelist(_p: str) -> Tuple[Optional[list], Optional[str]]:
            """
            获取插件的文件列表
            """
            file_api = f"https://api.github.com/repos/{user}/{repo}/contents/plugins/{_p.lower()}"
            r = RequestUtils(proxies=settings.PROXY, headers=settings.GITHUB_HEADERS, timeout=30).get_res(file_api)
            if r is None:
                return None, "连接仓库失败"
            elif r.status_code != 200:
                return None, f"连接仓库失败：{r.status_code} - {r.reason}"
            ret = r.json()
            if ret and ret[0].get("message") == "Not Found":
                return None, "插件在仓库中不存在"
            return ret, ""

        def __download_files(_p: str, _l: List[dict]) -> Tuple[bool, str]:
            """
            下载插件文件
            """
            if not _l:
                return False, "文件列表为空"
            for item in _l:
                if item.get("download_url"):
                    # 下载插件文件
                    res = RequestUtils(proxies=settings.PROXY,
                                       headers=settings.GITHUB_HEADERS, timeout=60).get_res(
                        self._proxy_url + item["download_url"])
                    if not res:
                        return False, f"文件 {item.get('name')} 下载失败！"
                    elif res.status_code != 200:
                        return False, f"下载文件 {item.get('name')} 失败：{res.status_code} - {res.reason}"
                    # 创建插件文件夹
                    file_path = Path(settings.ROOT_PATH) / "app" / item.get("path")
                    if not file_path.parent.exists():
                        file_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(res.text)
                else:
                    # 递归下载子目录
                    p = f"{_p}/{item.get('name')}"
                    l, m = __get_filelist(p)
                    if not l:
                        return False, m
                    __download_files(p, l)
            return True, ""

        if not pid or not repo_url:
            return False, "参数错误"

        # 获取插件的文件列表
        """
        [
            {
                "name": "__init__.py",
                "path": "plugins/autobackup/__init__.py",
                "sha": "cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "size": 12385,
                "url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                "html_url": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py",
                "git_url": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                "download_url": "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/plugins/autobackup/__init__.py",
                "type": "file",
                "_links": {
                    "self": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/contents/plugins/autobackup/__init__.py?ref=main",
                    "git": "https://api.github.com/repos/jxxghp/MoviePilot-Plugins/git/blobs/cd10eba3f0355d61adeb35561cb26a0a36c15a6c",
                    "html": "https://github.com/jxxghp/MoviePilot-Plugins/blob/main/plugins/autobackup/__init__.py"
                }
            }
        ]
        """
        # 获取第一级文件列表
        file_list, msg = __get_filelist(pid.lower())
        if not file_list:
            return False, msg
        # 本地存在时先删除
        plugin_dir = Path(settings.ROOT_PATH) / "app" / "plugins" / pid.lower()
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir, ignore_errors=True)
        # 下载所有文件
        __download_files(pid.lower(), file_list)
        # 插件目录下如有requirements.txt则安装依赖
        requirements_file = plugin_dir / "requirements.txt"
        if requirements_file.exists():
            SystemUtils.execute(f"pip install -r {requirements_file} > /dev/null 2>&1")
        # 安装成功后统计
        PluginHelper().install_reg(pid)

        return True, ""

    @staticmethod
    def register_plugin_api(plugin_id: str = None):
        """
        注册插件API（先删除后新增）
        """
        for api in PluginManager().get_plugin_apis(plugin_id):
            for r in router.routes:
                if r.path == api.get("path"):
                    router.routes.remove(r)
                    break
            router.add_api_route(**api)

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
        # 已安装插件
        local_plugins = self.get_local_plugins()
        # 编历 local_plugins，生成插件类型选项
        pluginOptions = []

        for plugin_id in list(local_plugins.keys()):
            local_plugin = local_plugins.get(plugin_id)
            pluginOptions.append({
                "title": f"{local_plugin.get('plugin_name')} v{local_plugin.get('plugin_version')}",
                "value": local_plugin.get("id")
            })
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
                                            'model': 'reload',
                                            'label': '仅重载',
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'plugin_ids',
                                            'label': '重装插件',
                                            'items': pluginOptions
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
                                            'model': 'proxy_url',
                                            'label': '代理地址',
                                            'placeholder': 'https://raw.proxy.com/'
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
                                            'model': 'plugin_url',
                                            'label': '仓库地址',
                                            'placeholder': 'https://github.com/%s/%s/'
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
                                            'text': '选择已安装的本地插件，强制安装插件市场最新版本。'
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
                                            'text': '支持指定插件仓库地址（https://github.com/%s/%s/）'
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
                                            'text': '仅重载：不会获取最新代码，而是基于本地代码重新加载插件。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "reload": False,
            "plugin_ids": [],
            "plugin_url": "",
            "proxy_url": ""
        }

    @staticmethod
    def get_local_plugins():
        """
        获取本地插件
        """
        # 已安装插件
        install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []

        local_plugins = {}
        # 线上插件列表
        markets = settings.PLUGIN_MARKET.split(",")
        for market in markets:
            online_plugins = PluginHelper().get_plugins(market) or {}
            for pid, plugin in online_plugins.items():
                if pid in install_plugins:
                    local_plugin = local_plugins.get(pid)
                    if local_plugin:
                        if StringUtils.compare_version(local_plugin.get("plugin_version"), plugin.get("version")) < 0:
                            local_plugins[pid] = {
                                "id": pid,
                                "plugin_name": plugin.get("name"),
                                "repo_url": market,
                                "plugin_version": plugin.get("version")
                            }
                    else:
                        local_plugins[pid] = {
                            "id": pid,
                            "plugin_name": plugin.get("name"),
                            "repo_url": market,
                            "plugin_version": plugin.get("version")
                        }

        return local_plugins

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        pass
