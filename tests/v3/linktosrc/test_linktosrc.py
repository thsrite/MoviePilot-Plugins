from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import linktosrc as linktosrc_module
from app.plugins.linktosrc import LinkToSrc


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "linktosrc" / "__init__.py"

def _history(src: str, dest: str, *, status: bool = True, mode: str = "link"):
    """构造整理历史替身，字段与公开 Oper 返回对象保持一致。"""
    return SimpleNamespace(src=src, dest=dest, status=status, mode=mode)


def test_manifest_and_strict_v3_import_contract() -> None:
    """V3 索引、旧代回退和稳定导入必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["LinkToSrc"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["LinkToSrc"]

    assert manifest["version"] == LinkToSrc.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy["v3"] is False

    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"app.db.oper.transferhistory", "app.sdk.logging"}.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db.models",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "sqlite3" not in source
    assert "Settings" not in source
    assert "app.sdk._legacy" not in source


def test_directory_filter_uses_public_pagination_and_deduplicates_dirs() -> None:
    """指定目录在公开分页结果中筛选硬链接，并合并重复配置。"""
    plugin = LinkToSrc()
    plugin._link_dirs = "/library/one\n/library/one\n/library/two\n"
    first = _history("/source/one.mkv", "/library/one/one.mkv")
    second = _history("/source/two.mkv", "/library/two/two.mkv")
    outside = _history("/source/outside.mkv", "/other/outside.mkv")
    plugin._query_all_successful_links = Mock(return_value=[first, outside, second])

    assert plugin._query_histories() == [first, second]
    plugin._query_all_successful_links.assert_called_once_with()


def test_no_directory_filter_uses_controlled_public_pagination() -> None:
    """未配置目录时通过公开异步分页读取全部成功移动记录。"""

    class FakeTransferHistoryOper:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, bool]] = []

        async def async_list_by_page(self, *, page: int, count: int, status: bool):
            self.calls.append((page, count, status))
            if page == 1:
                return [
                    _history("/source/one.mkv", "/library/one.mkv"),
                    _history("/source/two.mkv", "/library/two.mkv", mode="copy"),
                ]
            if page == 2:
                return [_history("/source/three.mkv", "/library/three.mkv")]
            return []

    plugin = LinkToSrc()
    plugin._PAGE_SIZE = 2
    fake_oper = FakeTransferHistoryOper()
    plugin._transfer_history = fake_oper

    histories = plugin._query_histories()

    assert [(item.src, item.dest) for item in histories] == [
        ("/source/one.mkv", "/library/one.mkv"),
        ("/source/three.mkv", "/library/three.mkv"),
    ]
    assert fake_oper.calls == [(1, 2, True), (2, 2, True)]


def test_duplicate_history_records_are_deduplicated() -> None:
    """重复的源目标对只能进入一次恢复管线。"""
    duplicate = _history("/source/movie.mkv", "/library/movie.mkv")
    another = _history("/source/episode.mkv", "/library/episode.mkv")

    unique = LinkToSrc._deduplicate_histories([duplicate, duplicate, another, duplicate])

    assert unique == [duplicate, another]


@pytest.mark.parametrize("case", ["relative_src", "relative_dest", "missing_dest", "dest_dir", "existing_src"])
def test_invalid_paths_are_rejected(tmp_path: Path, case: str) -> None:
    """非绝对路径、无效目标或已存在源文件不得被硬链接。"""
    source = tmp_path / "source" / "movie.mkv"
    dest = tmp_path / "library" / "movie.mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"movie")

    if case == "relative_src":
        history = _history("relative/movie.mkv", str(dest))
    elif case == "relative_dest":
        history = _history(str(source), "relative/movie.mkv")
    elif case == "missing_dest":
        dest.unlink()
        history = _history(str(source), str(dest))
    elif case == "dest_dir":
        dest.unlink()
        dest.mkdir()
        history = _history(str(source), str(dest))
    else:
        source.parent.mkdir(parents=True)
        source.write_bytes(b"existing")
        history = _history(str(source), str(dest))

    assert LinkToSrc()._restore_history(history) is False
    assert not (source.is_file() and source.stat().st_size == len(b"movie"))


def test_symlink_components_and_symlink_target_are_rejected(tmp_path: Path) -> None:
    """源父目录或目标本身含符号链接时必须安全拒绝。"""
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    source_parent = tmp_path / "source-link"
    source_parent.symlink_to(real_parent, target_is_directory=True)
    dest = tmp_path / "library" / "movie.mkv"
    dest.parent.mkdir()
    dest.write_bytes(b"movie")

    assert LinkToSrc()._restore_history(
        _history(str(source_parent / "movie.mkv"), str(dest))
    ) is False
    assert not (real_parent / "movie.mkv").exists()

    source = tmp_path / "safe-source" / "movie.mkv"
    symlink_dest = tmp_path / "library-link" / "movie.mkv"
    symlink_dest.parent.mkdir()
    symlink_dest.symlink_to(dest)
    assert LinkToSrc()._restore_history(_history(str(source), str(symlink_dest))) is False
    assert not source.exists()


def test_successful_restore_creates_hardlink(tmp_path: Path) -> None:
    """有效记录应创建源文件硬链接并与目标共享 inode。"""
    source = tmp_path / "source" / "movie.mkv"
    dest = tmp_path / "library" / "movie.mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"movie")

    assert LinkToSrc()._restore_history(_history(str(source), str(dest))) is True
    assert source.read_bytes() == b"movie"
    assert source.stat().st_ino == dest.stat().st_ino


def test_partial_failure_does_not_stop_remaining_records(tmp_path: Path) -> None:
    """单条恢复失败不能阻断后续有效记录。"""
    source_one = tmp_path / "source-one" / "movie.mkv"
    dest_one = tmp_path / "library" / "movie.mkv"
    source_two = tmp_path / "source-two" / "episode.mkv"
    dest_two = tmp_path / "library" / "episode.mkv"
    dest_one.parent.mkdir(parents=True)
    dest_one.write_bytes(b"movie")
    dest_two.write_bytes(b"episode")
    histories = [
        _history(str(source_one), str(dest_one)),
        _history(str(tmp_path / "source-bad" / "bad.mkv"), str(tmp_path / "missing.mkv")),
        _history(str(source_two), str(dest_two)),
    ]

    plugin = LinkToSrc()
    plugin._query_all_successful_links = Mock(return_value=histories)
    plugin._link_dirs = str(tmp_path / "library")

    plugin._task()

    assert source_one.stat().st_ino == dest_one.stat().st_ino
    assert source_two.stat().st_ino == dest_two.stat().st_ino


def test_onlyonce_is_consumed_before_task_execution() -> None:
    """一次性开关必须在恢复任务启动前持久化为关闭。"""
    events: list[str] = []
    plugin = LinkToSrc()
    plugin.update_config = Mock(side_effect=lambda config: events.append(f"config:{config['onlyonce']}"))
    plugin._task = Mock(side_effect=lambda: events.append("task"))

    plugin.init_plugin({"onlyonce": True, "link_dirs": "/library"})

    assert events == ["config:False", "task"]
    assert plugin.get_state() is False


def test_onlyonce_stays_pending_when_persistence_is_rejected() -> None:
    """宿主拒绝消费开关时不得启动恢复任务。"""
    plugin = LinkToSrc()
    plugin.update_config = Mock(return_value=False)
    plugin._task = Mock()

    plugin.init_plugin({"onlyonce": True})

    assert plugin.get_state() is True
    plugin._task.assert_not_called()


def test_plugin_lifecycle_surface_is_empty() -> None:
    """插件不注册额外 API、命令或后台服务。"""
    plugin = LinkToSrc()

    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []
    assert plugin.stop_service() is None


def test_module_does_not_expose_legacy_database_symbols() -> None:
    """模块只能暴露当前整理历史 Oper，不能回落到旧数据库连接实现。"""
    assert hasattr(linktosrc_module, "TransferHistoryOper")
    assert not hasattr(linktosrc_module, "sqlite3")
    assert not hasattr(linktosrc_module, "Settings")
