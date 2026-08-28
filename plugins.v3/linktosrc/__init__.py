"""从成功的整理历史中恢复缺失的源文件硬链接。"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.db.oper.transferhistory import TransferHistoryOper
from app.plugins import _PluginBase
from app.sdk.logging import logger


class LinkToSrc(_PluginBase):
    """根据成功移动记录，将媒体库文件硬链接回原始路径。"""

    plugin_name = "源文件恢复"
    plugin_desc = "根据MoviePilot的转移记录中的硬链文件恢复源文件"
    plugin_icon = "Time_machine_A.png"
    plugin_version = "2.0.0"
    plugin_author = "thsrite"
    author_url = "https://github.com/thsrite"
    plugin_config_prefix = "linktosrc_"
    plugin_order = 32
    auth_level = 1

    _PAGE_SIZE = 200
    _MAX_PAGES = 10000

    def __init__(self) -> None:
        """初始化插件状态和整理历史访问端口。"""
        super().__init__()
        self._onlyonce = False
        self._link_dirs = ""
        self._transfer_history = TransferHistoryOper()

    def init_plugin(self, config: Optional[dict] = None) -> None:
        """读取配置，并在一次性开关消费后执行恢复任务。"""
        config = dict(config or {})
        self._onlyonce = bool(config.get("onlyonce"))
        self._link_dirs = str(config.get("link_dirs") or "")

        if not self._onlyonce:
            return

        # 先持久化消费开关，再执行任务；配置写入失败时保留待执行状态，避免意图丢失。
        try:
            persisted = self.update_config(self._config_payload() | {"onlyonce": False})
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("保存一次性恢复状态失败：%s", error)
            return
        if persisted is False:
            logger.error("保存一次性恢复状态失败：宿主拒绝更新配置")
            return
        self._onlyonce = False
        self._task()

    def _config_payload(self) -> Dict[str, Any]:
        """返回当前配置的持久化投影。"""
        return {
            "onlyonce": self._onlyonce,
            "link_dirs": self._link_dirs,
        }

    def _task(self) -> None:
        """查询整理历史并逐条执行源文件硬链接恢复。"""
        try:
            histories = self._query_histories()
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("查询转移历史失败：%s", error)
            return

        unique_histories = self._deduplicate_histories(histories)
        logger.info("查询到历史记录%s条，去重后%s条", len(histories), len(unique_histories))
        if not unique_histories:
            logger.error("未获取到历史记录，停止处理")
            return

        restored = 0
        for history in unique_histories:
            try:
                if self._restore_history(history):
                    restored += 1
            except Exception as error:  # pylint: disable=broad-exception-caught
                # 单条数据异常只能跳过当前记录，不能中断剩余源文件恢复。
                logger.error("恢复源文件失败：%s -> %s，原因：%s", history.src, history.dest, error)

        logger.info("源文件恢复处理完成，成功%s条，共%s条", restored, len(unique_histories))

    def _query_histories(self) -> List[Any]:
        """通过公开分页接口读取成功硬链接记录，并按可选目标目录过滤。"""
        histories = self._query_all_successful_links()
        link_dirs = tuple(
            Path(value).resolve(strict=False)
            for value in self._configured_link_dirs()
            if Path(value).is_absolute()
        )
        if not link_dirs:
            return histories
        return [
            history
            for history in histories
            if self._history_matches_destinations(history, link_dirs)
        ]

    def _configured_link_dirs(self) -> Tuple[str, ...]:
        """按配置顺序返回非空目录，并合并重复项。"""
        return tuple(dict.fromkeys(
            value.strip()
            for value in self._link_dirs.splitlines()
            if value.strip()
        ))

    def _query_all_successful_links(self) -> List[Any]:
        """通过整理历史 Oper 的异步分页接口读取全部成功硬链接记录。"""
        async def query() -> List[Any]:
            histories: List[Any] = []
            for page in range(1, self._MAX_PAGES + 1):
                page_histories = await self._transfer_history.async_list_by_page(
                    page=page,
                    count=self._PAGE_SIZE,
                    status=True,
                )
                if not page_histories:
                    break
                histories.extend(
                    history
                    for history in page_histories
                    if self._is_successful_link(history)
                )
                if len(page_histories) < self._PAGE_SIZE:
                    break
            else:
                logger.error("转移历史分页超过上限%s页，停止继续查询", self._MAX_PAGES)
            return histories

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(query())

        # 插件生命周期接口是同步的；若宿主正处于事件循环，使用独立线程执行异步 Oper，
        # 避免在当前线程嵌套 asyncio.run。数据库事务仍由宿主的公开 Oper 接口管理。
        result: List[Any] = []
        error: List[BaseException] = []

        def run_query() -> None:
            try:
                result.extend(asyncio.run(query()))
            except BaseException as caught:  # pylint: disable=broad-exception-caught
                error.append(caught)

        worker = threading.Thread(target=run_query, name="LinkToSrcHistoryQuery")
        worker.start()
        worker.join()
        if error:
            raise error[0]
        return result

    @staticmethod
    def _is_successful_link(history: Any) -> bool:
        """过滤分页接口返回的成功硬链接记录。"""
        return bool(history.status) and str(history.mode or "").lower() == "link"

    @staticmethod
    def _history_matches_destinations(
            history: Any,
            link_dirs: Tuple[Path, ...],
    ) -> bool:
        """按规范化目标路径判断记录是否落在任一配置目录内。"""
        if not isinstance(history.dest, str) or not Path(history.dest).is_absolute():
            return False
        destination = Path(history.dest).resolve(strict=False)
        return any(destination.is_relative_to(link_dir) for link_dir in link_dirs)

    @staticmethod
    def _deduplicate_histories(histories: List[Any]) -> List[Any]:
        """按源、目标路径去重，避免目录重叠或历史重复导致重复操作。"""
        unique: List[Any] = []
        seen: set[tuple[str, str]] = set()
        for history in histories:
            key = (str(history.src), str(history.dest))
            if key in seen:
                continue
            seen.add(key)
            unique.append(history)
        return unique

    def _restore_history(self, history: Any) -> bool:
        """校验单条整理记录的路径安全性并创建硬链接。"""
        paths = self._validated_paths(history)
        if paths is None:
            return False
        src, dest = paths

        src.parent.mkdir(parents=True, exist_ok=True)
        if not self._paths_are_ready(src, dest):
            logger.warning("恢复前路径状态已变化，跳过处理：%s -> %s", src, dest)
            return False

        src.hardlink_to(dest)
        logger.info("硬链文件%s重新链接回源文件%s", dest, src)
        return True

    @classmethod
    def _validated_paths(cls, history: Any) -> Optional[Tuple[Path, Path]]:
        """校验记录路径及其父级组件，并返回可继续处理的路径。"""
        src = cls._path_value(history.src)
        dest = cls._path_value(history.dest)
        message: Optional[str] = None
        if src is None or dest is None:
            message = "整理记录路径为空或不是文本，跳过恢复"
        elif not src.is_absolute() or not dest.is_absolute():
            message = f"源文件和硬链接目标必须是绝对路径：{src} -> {dest}"
        elif cls._same_path(src, dest):
            message = f"源文件和硬链接目标不能相同：{src}"
        elif src.exists() or src.is_symlink() or os.path.lexists(src):
            message = f"源文件{src}已存在，跳过处理"
        elif dest.is_symlink() or not dest.exists() or not dest.is_file():
            message = f"硬链接目标{dest}不存在或不是普通文件，跳过处理"
        elif not cls._parents_are_safe(src.parent) or not cls._parents_are_safe(dest.parent):
            message = f"源文件或硬链接目标的父目录包含符号链接，跳过处理：{src} -> {dest}"

        if message:
            logger.warning(message)
            return None
        return src, dest

    @classmethod
    def _paths_are_ready(cls, src: Path, dest: Path) -> bool:
        """在创建目录后再次确认源、目标和父级组件未发生危险变化。"""
        return (
            cls._parents_are_safe(src.parent)
            and cls._parents_are_safe(dest.parent)
            and not src.exists()
            and not src.is_symlink()
            and not os.path.lexists(src)
            and not dest.is_symlink()
            and dest.exists()
            and dest.is_file()
        )

    @staticmethod
    def _path_value(value: Any) -> Optional[Path]:
        """只接受非空字符串路径，避免把异常数据库值隐式转换为路径。"""
        if not isinstance(value, str) or not value:
            return None
        return Path(value)

    @staticmethod
    def _same_path(src: Path, dest: Path) -> bool:
        """识别字面相同及规范化后指向同一路径的记录。"""
        return src == dest or src.resolve(strict=False) == dest.resolve(strict=False)

    @staticmethod
    def _parents_are_safe(path: Path) -> bool:
        """确认路径所有已存在的父级组件均为真实目录且不是符号链接。"""
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            if current.is_symlink():
                return False
            if current.exists() and not current.is_dir():
                return False
        return True

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """插件不注册额外 API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """插件不注册后台服务，一次性任务由配置生效时立即执行。"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
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
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "link_dirs",
                                            "label": "需要恢复的硬链接目录",
                                            "rows": 5,
                                            "placeholder": "硬链接目录 （一行一个）",
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "根据转移记录中的硬链接恢复源文件",
                                            "style": "white-space: pre-line;",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {"onlyonce": False, "link_dirs": ""}

    def get_page(self) -> List[dict]:
        """插件没有详情页。"""
        return []

    def get_state(self) -> bool:
        """返回一次性任务的当前状态。"""
        return self._onlyonce

    def stop_service(self) -> None:
        """插件没有需要停止的后台服务。"""
        return None
