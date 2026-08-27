from __future__ import annotations

import ast
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins import strmredirect as strmredirect_module
from app.plugins.strmredirect import StrmRedirect

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "strmredirect" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
            "workflow",
            "download_history",
            "transfer_history",
            "transfer_pending",
            "transfer_execution",
            "media_server",
            "download_failure",
            "user",
        )
    }
)


@pytest.fixture(autouse=True)
def _chain_runtime_context():
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复未配置状态。"""
    configure_chain_runtime_context_provider(
        lambda: ChainRuntimeContext(
            module_manager=Mock(),
            plugin_manager=Mock(),
            event_manager=Mock(),
            message_oper=Mock(),
            message_helper=Mock(),
            file_cache=Mock(),
            async_file_cache=Mock(),
            message_queue_factory=lambda _callback: Mock(),
            module_dispatcher_factory=lambda **_kwargs: Mock(),
            data_ports=get_chain_data_ports(),
        )
    )
    yield
    configure_chain_runtime_context_provider(None)


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_import_contracts() -> None:
    """V3 索引、旧代回退开关和公开日志 SDK 应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["StrmRedirect"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["StrmRedirect"]
    package_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )

    assert manifest["version"] == StrmRedirect.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert legacy_manifest["v3"] is False
    assert "StrmRedirect" not in package_manifest

    imports = _imports()
    assert "app.sdk.logging" in imports
    assert "app.log" not in imports


def test_service_uses_host_date_scheduler_and_preserves_config() -> None:
    """一次性执行应通过宿主服务注册，并保留 V2 配置字段。"""
    plugin = StrmRedirect()
    plugin.init_plugin(
        {
            "onlyonce": True,
            "unquote": True,
            "strm_path": "/tmp/strm",
            "origin_path": "/source",
            "redirect_path": "/target",
        }
    )

    assert plugin.get_state() is True
    service = plugin.get_service()[0]
    assert service["id"] == "StrmRedirect"
    assert service["trigger"] == "date"
    assert service["func_kwargs"] == {}
    assert service["kwargs"]["run_date"] > datetime.now()

    form, defaults = plugin.get_form()
    assert form
    assert defaults == {
        "onlyonce": False,
        "unquote": False,
        "strm_path": "",
        "origin_path": "",
        "redirect_path": "",
    }


def test_rewrite_obeys_path_and_url_boundaries() -> None:
    """相似路径不得误替换，URL 只在 authority 和路径边界完整时替换。"""
    assert StrmRedirect._replace_prefix("/media/movie", "/media", "/library") == "/library/movie"
    assert StrmRedirect._replace_prefix("/mediacenter/movie", "/media", "/library") == "/mediacenter/movie"
    assert StrmRedirect._replace_prefix(
        "https://example.test/media/movie?x=1",
        "https://example.test/media",
        "https://cdn.test/library",
    ) == "https://cdn.test/library/movie?x=1"
    assert StrmRedirect._replace_prefix(
        "https://example.test/mediacenter/movie",
        "https://example.test/media",
        "https://cdn.test/library",
    ) == "https://example.test/mediacenter/movie"
    assert StrmRedirect._replace_prefix(
        "https://example.test/media2/movie",
        "https://example.test/media",
        "https://cdn.test/library",
    ) == "https://example.test/media2/movie"


def test_rewrite_normalizes_trailing_separators_and_root_paths() -> None:
    """源前缀和目标前缀带分隔符或位于根路径时，连接处只保留一个分隔符。"""
    assert StrmRedirect._replace_prefix(
        "/source/movie", "/source/", "/target/"
    ) == "/target/movie"
    assert StrmRedirect._replace_prefix("/source/movie", "/source", "/") == "/movie"
    assert StrmRedirect._replace_prefix("/movie", "/", "/target/") == "/target/movie"
    assert StrmRedirect._replace_prefix(
        "https://example.test/media/movie",
        "https://example.test/media/",
        "https://cdn.test/library/",
    ) == "https://cdn.test/library/movie"
    assert StrmRedirect._replace_prefix(
        "https://example.test/movie",
        "https://example.test/",
        "https://cdn.test/",
    ) == "https://cdn.test/movie"


def test_path_redirect_matches_decoded_content_without_unquote_flag(tmp_path) -> None:
    """路径重定向即使未启用单独解码，也必须按 V2 的解码文本匹配。"""
    strm_file = tmp_path / "encoded.strm"
    strm_file.write_text("/source%2Fmovie", encoding="utf-8")
    plugin = StrmRedirect()
    plugin.init_plugin({"strm_path": str(tmp_path)})

    assert plugin.update_strm("/source", "/target", tmp_path) == 1
    assert strm_file.read_text(encoding="utf-8") == "/target/movie"


def test_only_regular_strm_files_are_decoded_and_atomically_replaced(tmp_path, monkeypatch) -> None:
    """只处理普通 STRM，UTF-8 内容变化时原子替换且重复执行不写盘。"""
    changed = tmp_path / "changed.strm"
    changed.write_text("https://example.test/a%20b", encoding="utf-8")
    unchanged = tmp_path / "unchanged.STRM"
    unchanged.write_text("https://example.test/a b", encoding="utf-8")
    ignored = tmp_path / "ignored.txt"
    ignored.write_text("https://example.test/a%20b", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_file = nested / "nested.strm"
    nested_file.write_text("/source/movie", encoding="utf-8")
    symlink = tmp_path / "symlink.strm"
    symlink.symlink_to(changed)

    replacements = []
    original_replace = strmredirect_module.os.replace

    def record_replace(source, target):
        replacements.append((Path(source), Path(target)))
        original_replace(source, target)

    monkeypatch.setattr(strmredirect_module.os, "replace", record_replace)
    plugin = StrmRedirect()
    plugin.init_plugin({"unquote": True, "strm_path": str(tmp_path)})

    assert plugin.update_strm("", "", tmp_path) == 1
    assert changed.read_text(encoding="utf-8") == "https://example.test/a b"
    assert nested_file.read_text(encoding="utf-8") == "/source/movie"
    assert ignored.read_text(encoding="utf-8") == "https://example.test/a%20b"
    assert len(replacements) == 1
    assert replacements[0][1] == changed

    assert plugin.update_strm("", "", tmp_path) == 0
    assert len(replacements) == 1


def test_service_is_single_flight(monkeypatch, tmp_path) -> None:
    """宿主重复触发时只允许一个扫描任务进入执行区。"""
    plugin = StrmRedirect()
    plugin.init_plugin({"onlyonce": True, "strm_path": str(tmp_path), "unquote": True})
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_update(*_args):
        calls.append(True)
        entered.set()
        assert release.wait(timeout=2)
        return 1

    monkeypatch.setattr(plugin, "_update_strm", blocked_update)
    monkeypatch.setattr(plugin, "update_config", Mock())
    result = []
    first = threading.Thread(target=lambda: result.append(plugin._run_service()))
    first.start()
    assert entered.wait(timeout=2)

    assert plugin._run_service() == 0
    release.set()
    first.join(timeout=2)

    assert result == [1]
    assert calls == [True]


def test_service_consumes_onlyonce_before_duplicate_callback(monkeypatch, tmp_path) -> None:
    """date 回调结束后再次到达时，不得因重复回调再次扫描。"""
    plugin = StrmRedirect()
    plugin.init_plugin({"onlyonce": True, "strm_path": str(tmp_path), "unquote": True})
    calls = []
    monkeypatch.setattr(plugin, "_update_strm", lambda *_args: calls.append(True) or 1)
    update_config = Mock(return_value=True)
    monkeypatch.setattr(plugin, "update_config", update_config)

    assert plugin._run_service() == 1
    assert plugin._run_service() == 0
    assert calls == [True]
    assert plugin._onlyonce is False
    assert update_config.call_count == 1
    assert update_config.call_args.args[0]["onlyonce"] is False


@pytest.mark.parametrize("return_value", [False, RuntimeError("save failed")])
def test_service_does_not_run_when_onlyonce_consumption_cannot_persist(
    monkeypatch, tmp_path, return_value
) -> None:
    """消费标记持久化失败时保留待执行状态，避免重载后重复执行。"""
    plugin = StrmRedirect()
    plugin.init_plugin({"onlyonce": True, "strm_path": str(tmp_path), "unquote": True})
    calls = []
    monkeypatch.setattr(plugin, "_update_strm", lambda *_args: calls.append(True) or 1)
    if isinstance(return_value, Exception):
        update_config = Mock(side_effect=return_value)
    else:
        update_config = Mock(return_value=return_value)
    monkeypatch.setattr(plugin, "update_config", update_config)

    assert plugin._run_service() == 0
    assert calls == []
    assert plugin._onlyonce is True
