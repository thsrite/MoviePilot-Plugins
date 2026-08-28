"""使用 MoviePilot V3 数据库治理服务备份数据库和配置文件。"""

from __future__ import annotations

import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.plugins import _PluginBase
from app.schemas.types import MessageType
from app.sdk.config import settings
from app.sdk.database import create_backup
from app.sdk.logging import logger


_LOCAL_BACKUP_NAME = re.compile(r"^bk_\d{14}(?:_\d+)?\.zip$")
_REMOTE_BACKUP_NAME = _LOCAL_BACKUP_NAME


class AutoBackup(_PluginBase):
    """创建宿主一致性数据库快照，并把配置文件打包保存或上传。"""

    plugin_name = "自动备份"
    plugin_desc = "自动备份数据和配置文件。"
    plugin_icon = "Time_machine_B.png"
    plugin_version = "3.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "autobackup_"
    plugin_order = 17
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = False
        self._cron: str | None = None
        self._cnt: Any = None
        self._onlyonce = False
        self._notify = False
        self._back_path = ""
        self._webdav_enabled = False
        self._webdav_hostname = ""
        self._webdav_login = ""
        self._webdav_password = ""
        self._webdav_digest_auth = False
        self._webdav_max_count: Any = 0
        self._webdav_notify = False
        self._webdav_disable_check = False
        self._webdav_client = None
        self._scheduler: BackgroundScheduler | None = None

    def init_plugin(self, config: dict | None = None):
        """读取配置，并为一次性执行建立独立调度器。"""
        self.stop_service()
        config = dict(config or {})

        self._enabled = bool(config.get("enabled", False))
        self._cron = config.get("cron") or None
        self._cnt = config.get("cnt")
        self._notify = bool(config.get("notify", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._back_path = str(config.get("back_path") or self.get_data_path())

        self._webdav_enabled = bool(config.get("webdav_enabled", False))
        self._webdav_hostname = str(config.get("webdav_hostname") or "")
        self._webdav_login = str(config.get("webdav_login") or "")
        self._webdav_password = str(config.get("webdav_password") or "")
        self._webdav_digest_auth = bool(config.get("webdav_digest_auth", False))
        self._webdav_max_count = config.get("webdav_max_count", 0)
        self._webdav_notify = bool(config.get("webdav_notify", False))
        self._webdav_disable_check = bool(config.get("webdav_disable_check", False))

        if self._webdav_enabled:
            self.__init_webdav_client()

        if not self._onlyonce:
            return

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info("自动备份服务启动，立即运行一次")
        self._scheduler.add_job(
            func=self.backup,
            trigger="date",
            run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
            name="自动备份",
        )
        self._onlyonce = False
        self.__update_config()
        if self._scheduler.get_jobs():
            self._scheduler.start()

    def get_state(self) -> bool:
        """返回插件是否已启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册需要登录态的手动备份接口。"""
        return [
            {
                "path": "/backup",
                "endpoint": self.backup,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "MoviePilot备份",
                "description": "创建数据库一致性快照并归档配置文件",
                "response_model": schemas.Response[None],
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册按配置周期执行的自动备份服务。"""
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "AutoBackup",
                "name": "自动备份定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.backup,
                "kwargs": {},
            }
        ]

    def backup(self) -> schemas.Response[None]:
        """执行一次数据库和配置文件备份，并返回统一响应模型。"""
        success, message = self.__backup()
        return schemas.Response(success=success, message=message)

    def __backup(self) -> Tuple[bool, str]:
        """创建本地归档、清理旧文件，并按需上传 WebDAV。"""
        logger.info(
            "当前时间 %s 开始备份",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        )
        backup_path = Path(self._back_path).expanduser()
        archive = self.backup_file(backup_path)
        if archive:
            success = True
            message = f"备份完成 备份文件 {archive}"
            logger.info(message)
        else:
            success = False
            message = "创建备份失败"
            logger.error(message)

        backup_count, deleted_count = self.__clean_local_backups(backup_path)
        webdav_success = True
        webdav_message = ""
        if self._webdav_enabled and archive and success:
            webdav_success, webdav_message = self.__upload_to_webdav(archive)
            if webdav_success:
                self.__clean_old_webdav_backups(self._webdav_max_count)

        if self._notify or (self._webdav_enabled and self._webdav_notify):
            notification = f"创建备份{'成功' if success else '失败'}\n"
            if success:
                notification += (
                    f"清理备份数量 {deleted_count}\n"
                    f"剩余备份数量 {backup_count - deleted_count}\n"
                )
            if self._webdav_enabled:
                notification += f"\nWebDAV上传{'成功' if webdav_success else '失败'}"
                if webdav_message:
                    notification += f"\n{webdav_message}"
            self.post_message(
                mtype=MessageType.SiteMessage,
                title="【自动备份任务完成】",
                text=notification,
            )

        return success and webdav_success, f"{message}{(' ' + webdav_message) if webdav_message else ''}"

    @staticmethod
    def backup_file(bk_path: Path | None = None) -> str | None:
        """把宿主一致性数据库备份与配置文件打包为一个本地归档。"""
        root = Path(bk_path) if bk_path else Path(settings.PLUGIN_DATA_PATH) / "AutoBackup"
        archive_dir: Path | None = None
        try:
            archive_dir = AutoBackup.__new_backup_dir(root)
            artifact = create_backup()
            source = Path(artifact.path)
            artifact_name = str(artifact.name)
            if (
                Path(artifact_name).name != artifact_name
                or source.name != artifact_name
                or source.suffix not in {".db", ".dump"}
                or not source.is_file()
            ):
                raise ValueError("主程序返回的数据库备份制品无效")

            shutil.copy2(source, archive_dir / artifact_name)
            config_path = Path(settings.CONFIG_PATH)
            for filename in ("category.yaml", "app.env"):
                config_file = config_path / filename
                if config_file.is_file():
                    shutil.copy2(config_file, archive_dir / filename)

            cookies_path = config_path / "cookies"
            if cookies_path.is_dir():
                shutil.copytree(cookies_path, archive_dir / "cookies")

            archive = Path(
                shutil.make_archive(
                    str(archive_dir),
                    "zip",
                    root_dir=str(archive_dir),
                )
            )
            logger.info("本地备份归档完成：%s", archive)
            return str(archive)
        except Exception as error:
            logger.error("创建本地备份归档失败：%s", error)
            return None
        finally:
            if archive_dir is not None:
                shutil.rmtree(archive_dir, ignore_errors=True)

    @staticmethod
    def __new_backup_dir(root: Path) -> Path:
        """为同一秒内的多次备份生成不冲突的临时目录。"""
        root.mkdir(parents=True, exist_ok=True)
        prefix = f"bk_{time.strftime('%Y%m%d%H%M%S', time.localtime())}"
        candidate = root / prefix
        sequence = 1
        while candidate.exists() or candidate.with_name(f"{candidate.name}.zip").exists():
            candidate = root / f"{prefix}_{sequence}"
            sequence += 1
        candidate.mkdir()
        return candidate

    def __clean_local_backups(self, root: Path) -> Tuple[int, int]:
        """按旧版配置的 cnt 值清理本地归档，并返回清理统计。"""
        if not root.is_dir():
            return 0, 0
        files = sorted(
            (
                path
                for path in root.iterdir()
                if path.is_file() and _LOCAL_BACKUP_NAME.fullmatch(path.name)
            ),
            key=lambda path: path.stat().st_mtime,
        )
        backup_count = len(files)
        keep_count = self.__parse_count(self._cnt, "cnt")
        if keep_count is None:
            return backup_count, 0

        deleted_count = 0
        for path in files[: max(0, backup_count - keep_count)]:
            try:
                path.unlink()
                deleted_count += 1
                logger.debug("删除备份 %s 成功", path)
            except OSError as error:
                logger.error("删除备份 %s 失败：%s", path, error)
        return backup_count, deleted_count

    def __init_webdav_client(self) -> bool:
        """按配置初始化可选的 WebDAV 客户端。"""
        try:
            from webdav3.client import Client

            if not self._webdav_hostname or not self._webdav_login or not self._webdav_password:
                raise ValueError("WebDAV配置不完整，请检查服务器地址、登录名和密码")
            webdav_config = {
                "webdav_hostname": self._webdav_hostname,
                "webdav_login": self._webdav_login,
                "webdav_password": self._webdav_password,
                "webdav_digest_auth": self._webdav_digest_auth,
            }
            if self._webdav_disable_check:
                webdav_config["disable_check"] = True
            self._webdav_client = Client(webdav_config)
            logger.info("WebDAV客户端初始化成功")
            return True
        except Exception as error:
            self._webdav_client = None
            logger.error("WebDAV客户端初始化失败：%s", error)
            return False

    def __connect_to_webdav(self) -> bool:
        """列出 WebDAV 根目录，确认远程连接可用。"""
        try:
            if not self._webdav_client:
                if not self.__init_webdav_client():
                    return False
            self._webdav_client.list("/")
            return True
        except Exception as error:
            logger.error("连接到WebDAV服务器失败：%s", error)
            return False

    def __upload_to_webdav(self, local_file_path: str) -> Tuple[bool, str]:
        """上传本地归档并验证远程文件。"""
        if not self.__connect_to_webdav():
            return False, "连接到WebDAV服务器失败"
        source = Path(local_file_path)
        try:
            if not source.is_file() or not _LOCAL_BACKUP_NAME.fullmatch(source.name):
                raise ValueError("本地备份归档无效")
            remote_file_path = urljoin(
                f"{self._webdav_hostname.rstrip('/')}/",
                source.name,
            )
            self._webdav_client.upload_sync(
                remote_path=source.name,
                local_path=str(source),
            )
            if not self._webdav_client.check(source.name):
                logger.error("上传完成但未找到远程备份：%s", source.name)
                return False, f"上传完成但未找到远程备份：{source.name}"
            return True, remote_file_path
        except Exception as error:
            message = f"上传到WebDAV服务器失败：{error}"
            logger.error(message)
            return False, message

    def __clean_old_webdav_backups(self, max_count: Any) -> None:
        """按配置保留 WebDAV 上最新的本插件归档。"""
        keep_count = self.__parse_count(max_count, "webdav_max_count")
        if keep_count is None or not self._webdav_client:
            return
        try:
            names = [Path(str(name)).name for name in self._webdav_client.list("/")]
            backups = [name for name in names if _REMOTE_BACKUP_NAME.fullmatch(name)]
            backups.sort(key=self.__backup_created_at)
            for name in backups[:-keep_count]:
                try:
                    self._webdav_client.clean(f"/{name}")
                    logger.info("WebDAV上的备份文件 %s 已删除", name)
                except Exception as error:
                    logger.error("删除WebDAV备份文件 %s 失败：%s", name, error)
        except Exception as error:
            logger.error("获取WebDAV备份文件列表失败：%s", error)

    @staticmethod
    def __parse_count(value: Any, name: str) -> int | None:
        """解析正整数保留数；空值或零表示不执行该项清理。"""
        if value is None or value == "":
            return None
        try:
            count = int(value)
        except (TypeError, ValueError):
            logger.error("配置错误：%s 必须是整数", name)
            return None
        if count <= 0:
            return None
        return count

    @staticmethod
    def __backup_created_at(name: str) -> datetime:
        """从本插件归档文件名读取创建时间。"""
        matched = _REMOTE_BACKUP_NAME.fullmatch(name)
        if matched is None:
            raise ValueError(f"无效的备份文件名：{name}")
        timestamp = re.search(r"\d{14}", name)
        if timestamp is None:
            raise ValueError(f"无效的备份文件名：{name}")
        return datetime.strptime(timestamp.group(), "%Y%m%d%H%M%S")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回本地归档、WebDAV 与调度配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "开启通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {"model": "cron", "label": "备份周期"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cnt",
                                            "label": "最大保留备份数",
                                            "type": "number",
                                            "min": 0,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "back_path", "label": "备份保存路径"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "数据库快照由 MoviePilot V3 数据库治理服务创建，配置文件会与快照一起归档。",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSubheader",
                                        "props": {"text": "WebDAV备份配置"},
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "webdav_enabled",
                                            "label": "启用WebDAV备份",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "webdav_notify",
                                            "label": "WebDAV通知",
                                            "show": "{{webdav_enabled}}",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "webdav_digest_auth",
                                            "label": "启用Digest认证",
                                            "show": "{{webdav_enabled}}",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "webdav_disable_check",
                                            "label": "忽略校验",
                                            "show": "{{webdav_enabled}}",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"show": "{{webdav_enabled}}"},
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webdav_hostname",
                                            "label": "WebDAV服务器地址",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "webdav_login", "label": "登录名"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webdav_password",
                                            "label": "登录密码",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webdav_max_count",
                                            "label": "WebDAV最大保留备份数",
                                            "type": "number",
                                            "min": 0,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "",
            "cnt": "",
            "back_path": str(self.get_data_path()),
            "webdav_enabled": False,
            "webdav_notify": False,
            "webdav_hostname": "",
            "webdav_login": "",
            "webdav_password": "",
            "webdav_digest_auth": False,
            "webdav_max_count": 0,
            "webdav_disable_check": False,
        }

    def get_page(self) -> None:
        """本插件没有详情页。"""
        return None

    def stop_service(self) -> None:
        """停止插件创建的一次性调度器。"""
        if not self._scheduler:
            return
        try:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown()
        finally:
            self._scheduler = None

    def __update_config(self) -> None:
        """保存关闭一次性开关后的完整配置，避免丢失 WebDAV 设置。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "cnt": self._cnt,
                "back_path": self._back_path,
                "webdav_enabled": self._webdav_enabled,
                "webdav_notify": self._webdav_notify,
                "webdav_hostname": self._webdav_hostname,
                "webdav_login": self._webdav_login,
                "webdav_password": self._webdav_password,
                "webdav_digest_auth": self._webdav_digest_auth,
                "webdav_max_count": self._webdav_max_count,
                "webdav_disable_check": self._webdav_disable_check,
            }
        )
