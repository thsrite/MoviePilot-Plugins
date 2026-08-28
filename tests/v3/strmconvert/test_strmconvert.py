from __future__ import annotations

import ast
import json
import os
import stat
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import quote

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import strmconvert as strmconvert_module
from app.plugins.strmconvert import StrmConvert

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "strmconvert" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _plugin() -> StrmConvert:
    """构造不触碰真实插件配置的测试实例。"""
    plugin = StrmConvert()
    plugin.update_config = Mock()
    return plugin


def _run_once(plugin: StrmConvert) -> bool:
    """执行插件登记的宿主 date 服务。"""
    service = plugin.get_service()[0]
    assert service["trigger"] == "date"
    assert isinstance(service["kwargs"]["run_date"], datetime)
    return service["func"]()


def test_v3_manifest_and_strict_import_contract() -> None:
    """V3 索引、旧代回退和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["StrmConvert"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["StrmConvert"]
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert manifest["version"] == StrmConvert.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False
    assert "app.sdk.logging" in _imports()
    assert "app.log" not in source
    assert "relative_to(" in source
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "replace"
        and not (
            isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
        )
        for node in ast.walk(tree)
    )
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in _imports())


def test_local_mode_maps_only_relative_regular_strm_files(tmp_path: Path) -> None:
    """本地模式应只转换源目录内的常规 STRM 文件，并保留相对目录。"""
    source = tmp_path / "source"
    library = tmp_path / "library"
    nested = source / "剧集 01"
    nested.mkdir(parents=True)
    strm_file = nested / "示例.strm"
    strm_file.write_text(
        "https://media.example/video%20name.mkv?token=ignored\n",
        encoding="utf-8",
    )
    ignored = source / "示例.strm.bak"
    ignored.write_text("keep", encoding="utf-8")
    directory_named_strm = source / "not-a-file.strm"
    directory_named_strm.mkdir()
    sibling = tmp_path / "source-extra"
    sibling.mkdir()
    sibling_file = sibling / "outside.strm"
    sibling_file.write_text("keep", encoding="utf-8")

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "to_api": False,
            "convert_confs": f"{source}#{library}",
        }
    )

    assert strm_file.read_text(encoding="utf-8").startswith("https://")
    assert _run_once(plugin) is True
    assert strm_file.read_text(encoding="utf-8") == str(library / "剧集 01" / "示例.mkv")
    assert ignored.read_text(encoding="utf-8") == "keep"
    assert sibling_file.read_text(encoding="utf-8") == "keep"
    plugin.update_config.assert_called_once_with(
        {"to_local": False, "to_api": False, "convert_confs": f"{source}#{library}"}
    )


def test_local_mode_skips_unchanged_content_without_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标内容未变化时不得创建临时文件或替换源文件。"""
    source = tmp_path / "source"
    library = tmp_path / "library"
    source.mkdir()
    strm_file = source / "movie.strm"
    expected = library / "movie.mkv"
    strm_file.write_text(str(expected), encoding="utf-8")
    replace = Mock(wraps=strmconvert_module.os.replace)
    monkeypatch.setattr(strmconvert_module.os, "replace", replace)

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "convert_confs": f"{source}#{library}",
        }
    )

    assert _run_once(plugin) is True
    replace.assert_not_called()
    assert strm_file.read_text(encoding="utf-8") == str(expected)
    assert list(source.glob(".*.tmp")) == []


def test_changed_content_uses_same_directory_atomic_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容变化时应先写同目录临时文件，再原子替换源文件。"""
    source = tmp_path / "source"
    library = tmp_path / "library"
    source.mkdir()
    strm_file = source / "movie.strm"
    strm_file.write_text("https://media.example/movie.mkv", encoding="utf-8")
    real_replace = strmconvert_module.os.replace
    calls: list[tuple[str, str]] = []

    def atomic_replace(source_path: str, target_path: str) -> None:
        calls.append((source_path, target_path))
        real_replace(source_path, target_path)

    monkeypatch.setattr(strmconvert_module.os, "replace", atomic_replace)
    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "convert_confs": f"{source}#{library}",
        }
    )

    assert _run_once(plugin) is True
    assert len(calls) == 1
    assert Path(calls[0][0]).parent == strm_file.parent
    assert Path(calls[0][1]) == strm_file
    assert strm_file.read_text(encoding="utf-8") == str(library / "movie.mkv")
    assert list(source.glob(".*.tmp")) == []


def test_changed_content_preserves_existing_permission_mode(tmp_path: Path) -> None:
    """原子替换产生新 inode 时应保留源 STRM 文件的权限位。"""
    source = tmp_path / "source"
    library = tmp_path / "library"
    source.mkdir()
    strm_file = source / "movie.strm"
    strm_file.write_text("https://media.example/movie.mkv", encoding="utf-8")
    os.chmod(strm_file, 0o644)
    original_mode = stat.S_IMODE(strm_file.stat().st_mode)

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "convert_confs": f"{source}#{library}",
        }
    )

    assert _run_once(plugin) is True
    assert stat.S_IMODE(strm_file.stat().st_mode) == original_mode


@pytest.mark.parametrize("cloud_type", ["alist", "cd2"])
def test_api_mode_url_encodes_relative_target_path(
    tmp_path: Path,
    cloud_type: str,
) -> None:
    """API 模式应编码完整映射路径，且保持 cd2/alist 的旧 URL 形状。"""
    source = tmp_path / "source"
    library = tmp_path / "云盘 根目录"
    nested = source / "剧集 01"
    nested.mkdir(parents=True)
    strm_file = nested / "示例.strm"
    strm_file.write_text("old", encoding="utf-8")

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_api": True,
            "convert_confs": f"{source}#{library}#{cloud_type}#127.0.0.1:5244",
        }
    )

    assert _run_once(plugin) is True
    mapped_path = library / "剧集 01" / "示例.strm"
    encoded_path = quote(str(mapped_path), safe="")
    if cloud_type == "cd2":
        expected = f"http://127.0.0.1:5244/static/http/127.0.0.1:5244/False/{encoded_path}"
    else:
        expected = f"http://127.0.0.1:5244/d/{encoded_path}"
    assert strm_file.read_text(encoding="utf-8") == expected


def test_invalid_configuration_and_invalid_utf8_fail_closed(tmp_path: Path) -> None:
    """异常配置或非 UTF-8 文件不得中断任务，也不得破坏源文件。"""
    source = tmp_path / "source"
    library = tmp_path / "library"
    source.mkdir()
    malformed = source / "malformed.strm"
    malformed.write_text("keep", encoding="utf-8")
    invalid_utf8 = source / "invalid.strm"
    invalid_utf8.write_bytes(b"\xff\xfe")

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "convert_confs": (
                f"not-a-valid-line\n{source}#{library}#too-many-fields\n"
            ),
        }
    )
    assert _run_once(plugin) is True
    assert malformed.read_text(encoding="utf-8") == "keep"
    assert invalid_utf8.read_bytes() == b"\xff\xfe"

    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_api": True,
            "convert_confs": f"{source}#{library}#unknown#127.0.0.1:5244",
        }
    )
    assert _run_once(plugin) is True
    assert malformed.read_text(encoding="utf-8") == "keep"


def test_both_modes_are_rejected_and_lifecycle_contract_is_empty() -> None:
    """两种模式互斥，空服务接口应符合 V3 插件基类契约。"""
    plugin = _plugin()
    plugin.init_plugin(
        {
            "to_local": True,
            "to_api": True,
            "convert_confs": "source#library",
        }
    )

    plugin.update_config.assert_not_called()
    assert plugin.get_state() is False
    assert plugin.get_service() == []
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_page() == []
    assert plugin.stop_service() is None
    _, defaults = plugin.get_form()
    assert defaults == {"to_local": False, "to_api": False, "convert_confs": ""}


def test_once_is_consumed_only_when_host_service_runs() -> None:
    """初始化只登记服务，执行入口消费开关并拒绝重复回调。"""
    plugin = _plugin()
    plugin.init_plugin({"to_local": True, "convert_confs": "invalid"})

    service = plugin.get_service()[0]
    assert plugin.get_state() is True
    plugin.update_config.assert_not_called()

    assert service["func"]() is True
    assert plugin.get_state() is False
    plugin.update_config.assert_called_once_with(
        {"to_local": False, "to_api": False, "convert_confs": "invalid"}
    )
    assert service["func"]() is False
