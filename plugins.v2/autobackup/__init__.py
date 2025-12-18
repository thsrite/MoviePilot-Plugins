import glob
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import docker
import docker.errors
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import schemas
from app.core.config import settings
from app.db import db_query
from app.helper.system import SystemHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.utils.system import SystemUtils


class AutoBackup(_PluginBase):
    # 插件名称
    plugin_name = "自动备份"
    # 插件描述
    plugin_desc = "自动备份数据和配置文件。"
    # 插件图标
    plugin_icon = "Time_machine_B.png"
    # 插件版本
    plugin_version = "2.1.3"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "autobackup_"
    # 加载顺序
    plugin_order = 17
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _cnt = None
    _onlyonce = False
    _notify = False
    _back_path = None

    # WebDAV相关配置
    _webdav_enabled = False
    _webdav_hostname = None
    _webdav_login = None
    _webdav_password = None
    _webdav_digest_auth = False
    _webdav_max_count = None
    _webdav_notify = False
    _webdav_disable_check = False
    _webdav_client = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._cnt = config.get("cnt")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._back_path = config.get("back_path")

            # WebDAV配置
            self._webdav_enabled = config.get("webdav_enabled", False)
            self._webdav_hostname = config.get("webdav_hostname")
            self._webdav_login = config.get("webdav_login")
            self._webdav_password = config.get("webdav_password")
            self._webdav_digest_auth = config.get("webdav_digest_auth", False)
            self._webdav_max_count = config.get("webdav_max_count")
            self._webdav_notify = config.get("webdav_notify", False)
            self._webdav_disable_check = config.get("webdav_disable_check", False)

            # 初始化WebDAV客户端
            if self._webdav_enabled:
                self.__init_webdav_client()

            # 加载模块
        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"自动备份服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__backup, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="自动备份")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "cnt": self._cnt,
                "notify": self._notify,
                "back_path": self._back_path,
                "webdav_enabled": self._webdav_enabled,
                "webdav_hostname": self._webdav_hostname,
                "webdav_login": self._webdav_login,
                "webdav_password": self._webdav_password,
                "webdav_digest_auth": self._webdav_digest_auth,
                "webdav_max_count": self._webdav_max_count,
                "webdav_notify": self._webdav_notify,
                "webdav_disable_check": self._webdav_disable_check
            })

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def api_backup(self, apikey: str):
        """
        API调用备份
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        return self.__backup()

    def __backup(self):
        """
        自动备份、删除备份
        """
        logger.info(f"当前时间 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))} 开始备份")

        # 备份保存路径
        bk_path = Path(self._back_path) if self._back_path else self.get_data_path()

        # 备份
        zip_file = self.backup_file(bk_path=bk_path)

        if zip_file:
            success = True
            msg = f"备份完成 备份文件 {zip_file}"
            logger.info(msg)
        else:
            success = False
            msg = "创建备份失败"
            logger.error(msg)

        # 清理备份
        bk_cnt = 0
        del_cnt = 0
        if self._cnt:
            # 获取指定路径下所有以"bk"开头的文件，按照创建时间从旧到新排序
            files = sorted(glob.glob(f"{bk_path}/bk**"), key=os.path.getctime)
            bk_cnt = len(files)
            # 计算需要删除的文件数
            del_cnt = bk_cnt - int(self._cnt)
            if del_cnt > 0:
                logger.info(
                    f"获取到 {bk_path} 路径下备份文件数量 {bk_cnt} 保留数量 {int(self._cnt)} 需要删除备份文件数量 {del_cnt}")

                # 遍历并删除最旧的几个备份
                for i in range(del_cnt):
                    file_path = Path(files[i])
                    if file_path.is_file():
                        file_path.unlink()
                    elif file_path.is_dir():
                        shutil.rmtree(file_path)
                    logger.debug(f"删除备份 {files[i]} 成功")
            else:
                logger.info(
                    f"获取到 {bk_path} 路径下备份文件数量 {bk_cnt} 保留数量 {int(self._cnt)} 无需删除")

        # 如果启用了WebDAV备份，则上传到WebDAV
        webdav_success = True
        webdav_msg = ""
        if self._webdav_enabled and zip_file and success:
            webdav_success, webdav_msg = self.__upload_to_webdav(zip_file)
            if webdav_success and self._webdav_max_count:
                self.__clean_old_webdav_backups(self._webdav_max_count)

        # 发送通知
        if self._notify or self._webdav_notify:
            notification_msg = f"创建备份{'成功' if success else '失败'}\n"
            if success:
                notification_msg += f"清理备份数量 {del_cnt}\n"
                notification_msg += f"剩余备份数量 {bk_cnt - del_cnt}\n"

            if self._webdav_enabled:
                notification_msg += f"\nWebDAV上传{'成功' if webdav_success else '失败'}"
                if webdav_msg:
                    notification_msg += f"\n{webdav_msg}"

            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【自动备份任务完成】",
                text=notification_msg)

        return success and webdav_success, f"{msg}{(' ' + webdav_msg) if webdav_msg else ''}"

    @staticmethod
    def backup_file(bk_path: Path = None):
        """
        @param bk_path     自定义备份路径
        """
        try:
            # 创建备份文件夹
            config_path = Path(settings.CONFIG_PATH)
            backup_file = f"bk_{time.strftime('%Y%m%d%H%M%S')}"
            backup_path = bk_path / backup_file
            if not backup_path.exists():
                backup_path.mkdir(parents=True)

            # 把现有的相关文件进行copy备份
            category_file = config_path / "category.yaml"
            if category_file.exists():
                shutil.copy(category_file, backup_path)

            # 备份数据库
            if settings.DB_TYPE == "sqlite":
                # 查找所有以 "user.db" 开头的文件
                userdb_files = list(config_path.glob("user.db*"))
                # 如果找到了任何匹配的文件，则进行复制
                for userdb_file in userdb_files:
                    if userdb_file.exists():
                        shutil.copy(userdb_file, backup_path)
            if settings.DB_TYPE == "postgresql":
                # 获取数据库连接信息
                db_host = str(settings.DB_POSTGRESQL_HOST)
                db_port = str(settings.DB_POSTGRESQL_PORT)
                db_name = str(settings.DB_POSTGRESQL_DATABASE)
                db_user = str(settings.DB_POSTGRESQL_USERNAME)
                db_password = str(settings.DB_POSTGRESQL_PASSWORD)

                # 设置环境变量以避免在命令行中暴露密码
                env = os.environ.copy()
                env['PGPASSWORD'] = db_password

                # 检查 pg_dump 是否可用
                if not shutil.which('pg_dump'):
                    # 获取 PostgreSQL 版本号
                    pg_version = AutoBackup._get_postgresql_major_version()
                    # 执行安装 PostgreSQL 客户端的命令
                    try:
                        AutoBackup.install_postgresql_client(pg_version)
                        logger.info(f"PostgreSQL {pg_version} 客户端安装成功。")
                    except (docker.errors.APIError, docker.errors.ContainerError, subprocess.CalledProcessError) as e:
                        logger.error(f"安装 PostgreSQL {pg_version} 客户端失败: {e.stderr.strip() if e.stderr else str(e)}")
                        logger.error("请手动执行安装命令。")
                        logger.error(f'apt-get update && apt-get install -y wget gnupg lsb-release && wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/postgresql.gpg && echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && apt-get update && apt-get install -y postgresql-client-{pg_version}')
                        return None

                # 构建 pg_dump 命令
                pg_dump_cmd = [
                    'pg_dump',
                    '-h', db_host,
                    '-p', db_port,
                    '-U', db_user,
                    '-d', db_name,
                    '-f', str(backup_path / 'postgresql_backup.sql')
                ]

                # 执行备份
                try:
                    result = subprocess.run(pg_dump_cmd, env=env, check=True, capture_output=True, text=True)
                    logger.info(f"PostgreSQL数据库备份成功: {backup_path / 'postgresql_backup.sql'}")
                except subprocess.CalledProcessError as e:
                    error_message = e.stderr.strip() if e.stderr else str(e)
                    logger.error(f"PostgreSQL数据库备份失败: {error_message}")
                    # 检查是否是版本不匹配的错误
                    if "server version mismatch" in error_message:
                        logger.error("PostgreSQL数据库备份失败: pg_dump 版本与服务器版本不匹配，请安装与服务器版本匹配的 pg_dump。")
                    return None

            app_file = config_path / "app.env"
            if app_file.exists():
                shutil.copy(app_file, backup_path)
            cookies_path = config_path / "cookies"
            if cookies_path.exists():
                shutil.copytree(cookies_path, f'{backup_path}/cookies')

            zip_file = str(backup_path) + '.zip'
            if os.path.exists(zip_file):
                zip_file = str(backup_path) + '.zip'
            shutil.make_archive(str(backup_path), 'zip', str(backup_path))
            shutil.rmtree(str(backup_path))
            return zip_file
        except IOError as e:
            logger.error(f"创建备份失败: {e}")
            return None

    @classmethod
    @db_query
    def _get_postgresql_major_version(cls, db: Session = None):
        """
        获取 PostgreSQL 版本号
        """

        try:
            result = db.execute(text("SHOW server_version;"))
            row = result.fetchone()
            if row:
                version_string = row[0]  # 获取第一个字段的值
                # 提取主版本号
                major_version = version_string.split('.')[0]
                return int(major_version)  # 转换为整数
        except Exception as e:
            logger.debug(f"获取PostgreSQL版本失败: {e}")

        return 17  # 默认版本

    @staticmethod
    def install_postgresql_client(pg_version):
        """
        安装 PostgreSQL 客户端
        """
        logger.info(f"正在安装 PostgreSQL {pg_version} 客户端...")
        proxy_url = settings.PROXY_HOST

        # 设置代理
        exec_env = {}
        if proxy_url and proxy_url.startswith(('http://', 'https://')):
            exec_env.update(dict.fromkeys(['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'], proxy_url))
        # 构建安装脚本
        install_script = (
                'apt-get update && '
                'apt-get install -y wget gnupg lsb-release && '
                'wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | '
                'gpg --dearmor > /etc/apt/trusted.gpg.d/postgresql.gpg && '
                'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" '
                '> /etc/apt/sources.list.d/pgdg.list && '
                'apt-get update && '
                'apt-get install -y postgresql-%s' % pg_version
        )
        # 非root用户且是docker环境
        if os.geteuid() != 0 and SystemUtils.is_docker() and (container_id := SystemHelper._get_container_id()):
            # 创建 Docker 客户端
            client = docker.DockerClient(base_url=settings.DOCKER_CLIENT_API)
            # 获取容器对象
            container = client.containers.get(container_id)
            # 执行命令
            exit_code, output = container.exec_run(
                cmd=['sh', '-c', install_script],
                environment=exec_env
            )
            if exit_code != 0:
                raise docker.errors.ContainerError(exit_status=exit_code, command=install_script, stderr=output)

        else:
            subprocess.run(
                ['sh', '-c', install_script],
                capture_output=True,
                text=True,
                check=True,
                env=exec_env
            )

    def __init_webdav_client(self):
        """初始化WebDAV客户端"""
        try:
            from webdav3.client import Client

            # 检查必要配置
            if not self._webdav_hostname or not self._webdav_login or not self._webdav_password:
                logger.warning("WebDAV配置不完整，请检查服务器地址、登录名和密码")
                return False

            # WebDAV配置
            webdav_config = {
                'webdav_hostname': self._webdav_hostname,
                'webdav_login': self._webdav_login,
                'webdav_password': self._webdav_password,
                'webdav_digest_auth': self._webdav_digest_auth
            }

            if self._webdav_disable_check:
                webdav_config.update({"disable_check": True})

            self._webdav_client = Client(webdav_config)
            logger.info("WebDAV客户端初始化成功")
            return True
        except ImportError:
            logger.error("缺少webdavclient3依赖包，请安装：pip install webdavclient3")
            return False
        except Exception as e:
            logger.error(f"WebDAV客户端初始化失败: {str(e)}")
            return False

    def __connect_to_webdav(self):
        """尝试连接到WebDAV服务器，并验证连接是否成功。"""
        try:
            if not self._webdav_client:
                logger.error("无法获取WebDAV客户端实例，请尝试重新启用插件")
                return False
            # 尝试列出根目录来检查连接
            self._webdav_client.list('/')  # 如果不成功，会抛出异常
            logger.info("成功连接到WebDAV服务器")
            return True
        except Exception as e:
            logger.error(f"连接到WebDAV服务器失败: {str(e)}")
            return False

    def __upload_to_webdav(self, local_file_path):
        """
        上传备份文件到WebDAV服务器
        """
        logger.info("开始上传备份文件到WebDAV服务器")

        # 检查WebDAV客户端
        if not self._webdav_client:
            if not self.__init_webdav_client():
                return False, "WebDAV客户端初始化失败"

        # 检查连接
        if not self.__connect_to_webdav():
            return False, "连接到WebDAV服务器失败"

        # 初始化外部变量以捕获回调结果
        result_message = ""

        def upload_callback():
            """上传完成后的回调函数"""
            nonlocal result_message
            # 检查文件是否在WebDAV上存在
            try:
                if self._webdav_client.check(remote_file_path):
                    logger.info(f"上传完成，远程备份路径：{remote_file_path}")
                    result_message = f"WebDAV备份验证成功"
                else:
                    logger.error(
                        f"上传完成，但远程备份路径没有检测到备份文件，请检查备份路径是否正确，远程备份路径：{remote_file_path}")
                    result_message = f"WebDAV备份上传完成但验证失败"
            except Exception as e:
                logger.warning(f"检查文件存在性时出错: {str(e)}，但上传可能已完成")
                result_message = f"WebDAV备份上传完成但验证失败"

        try:
            # 使用urljoin确保路径正确
            file_name = os.path.basename(local_file_path)
            remote_file_path = urljoin(f'{self._webdav_hostname}/', file_name)
            logger.info(f"远程备份路径为：{remote_file_path}")
            self._webdav_client.upload_sync(remote_path=file_name, local_path=local_file_path, callback=upload_callback)
            return True, result_message
        except Exception as e:
            error_msg = f"上传到WebDAV服务器失败: {str(e)}"
            logger.error(error_msg)
            if hasattr(e, 'response'):
                logger.error(f"服务器响应: {getattr(e.response, 'text', '无响应内容')}")
            return False, error_msg

    def __clean_old_webdav_backups(self, max_count):
        """
        清理WebDAV服务器上的旧备份文件
        """
        if not max_count or not self._webdav_client:
            return

        # 定义备份文件的正则表达式模式
        pattern = re.compile(r"bk_\d{14}\.zip")

        # 清理WebDAV服务器上的旧备份
        try:
            remote_files = self._webdav_client.list('/')
            filtered_files = [f for f in remote_files if pattern.match(f)]
            sorted_files = sorted(filtered_files,
                                  key=lambda x: datetime.strptime(x, "bk_%Y%m%d%H%M%S.zip"))
            excess_count = len(sorted_files) - int(max_count)

            if excess_count > 0:
                logger.info(
                    f"WebDAV上备份文件数量为 {len(sorted_files)}，超出最大保留数 {max_count}，需删除 {excess_count} 个备份文件")
                for file_info in sorted_files[:-int(max_count)]:
                    remote_file_path = f"/{file_info}"
                    try:
                        self._webdav_client.clean(remote_file_path)
                        logger.info(f"WebDAV上的备份文件 {remote_file_path} 已删除")
                    except Exception as e:
                        logger.error(f"删除WebDAV文件 {remote_file_path} 失败: {str(e)}")
            else:
                logger.info(
                    f"WebDAV上备份文件数量为 {len(sorted_files)}，符合最大保留数 {max_count}，不需删除文件")
        except Exception as e:
            logger.error(f"获取WebDAV文件列表失败: {str(e)}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/backup",
            "endpoint": self.api_backup,
            "methods": ["GET"],
            "summary": "MoviePilot备份",
            "description": "MoviePilot备份",
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
                "id": "AutoBackup",
                "name": "自动备份定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__backup,
                "kwargs": {}
            }]

    def backup(self) -> schemas.Response:
        """
        API调用备份
        """
        success, msg = self.__backup()
        return schemas.Response(
            success=success,
            message=msg
        )

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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '备份周期'
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
                                            'model': 'cnt',
                                            'label': '最大保留备份数'
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
                                            'model': 'back_path',
                                            'label': '备份保存路径'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # WebDAV配置部分
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VDivider'
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
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSubheader',
                                        'props': {
                                            'text': 'WebDAV备份配置'
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'webdav_enabled',
                                            'label': '启用WebDAV备份',
                                            'hint': '开启后插件将备份文件上传到WebDAV服务器',
                                            'persistent-hint': True
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
                                            'model': 'webdav_notify',
                                            'label': 'WebDAV通知',
                                            'hint': '是否在WebDAV上传事件发生时发送通知',
                                            'persistent-hint': True,
                                            'show': '{{webdav_enabled}}'
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
                                            'model': 'webdav_digest_auth',
                                            'label': '启用Digest认证',
                                            'hint': '开启后将使用Digest认证',
                                            'persistent-hint': True,
                                            'show': '{{webdav_enabled}}'
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
                                            'model': 'webdav_disable_check',
                                            'label': '忽略校验',
                                            'hint': '开启后将忽略Webdav目录校验',
                                            'persistent-hint': True,
                                            'show': '{{webdav_enabled}}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'show': '{{webdav_enabled}}'},
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'webdav_hostname',
                                            'label': 'WebDAV服务器地址',
                                            'hint': '输入WebDAV服务器的地址',
                                            'persistent-hint': True
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
                                            'model': 'webdav_login',
                                            'label': '登录名',
                                            'hint': '输入登录名',
                                            'persistent-hint': True
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
                                            'model': 'webdav_password',
                                            'label': '登录密码',
                                            'hint': '输入登录密码',
                                            'persistent-hint': True
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
                                            'model': 'webdav_max_count',
                                            'label': 'WebDAV最大保留备份数',
                                            "min": "0",
                                            'hint': '输入WebDAV最大保留备份数',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'show': '{{webdav_enabled}}'},
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
                                            'text': '注意：如WebDAV备份失败，请检查日志，并确认WebDAV目录存在，如果存在中文字符，可以尝试进行Url编码后重试'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {'show': '{{webdav_enabled}}'},
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
                                            'text': '注意：如WebDAV备份失败，请尝试开启忽略校验后重试'
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
                                            'text': '备份文件路径默认为本地映射的config/plugins/AutoBackup。如果是postgresql，请手动执行以下命令安装postgresql客户端'
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
                                            'text': 'apt-get update && apt-get install -y wget gnupg lsb-release && wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/postgresql.gpg && echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list && apt-get update && apt-get install -y postgresql-client-17'
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
            "request_method": "POST",
            "webhook_url": "",
            "back_path": str(self.get_data_path()),
            "webdav_enabled": False,
            "webdav_notify": True
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
