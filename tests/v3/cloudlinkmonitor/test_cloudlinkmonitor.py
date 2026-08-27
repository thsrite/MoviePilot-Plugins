from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app import schemas
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import (
    configure_chain_data_ports,
    get_chain_data_ports,
)
from app.plugins import cloudlinkmonitor as cloudlinkmonitor_module
from app.plugins.cloudlinkmonitor import CloudLinkMonitor
from app.schemas.types import MediaSource, MediaType, MessageType

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
            "media_server",
            "download_failure",
            "user",
        )
    }
)

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cloudlinkmonitor" / "__init__.py"


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


def test_v3_plugin_imports_and_initializes(monkeypatch) -> None:
    """V3 插件应能导入，并在隔离外部链资源后完成生命周期初始化。"""
    monkeypatch.setattr(cloudlinkmonitor_module, "TransferChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "MediaChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "TmdbChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "StorageChain", Mock)

    plugin = CloudLinkMonitor()
    plugin.init_plugin({})

    assert plugin.plugin_version == "3.0.0"
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]

    plugin.stop_service()


def test_previous_title_query_uses_complete_media_identity() -> None:
    """历史标题查询必须传递来源与来源原生 ID，非法身份不得降级为裸 ID。"""
    plugin = CloudLinkMonitor()
    plugin.transferhis = Mock()
    plugin.transferhis.get_by_media_identity.return_value = SimpleNamespace(
        title="统一身份标题"
    )
    mediainfo = SimpleNamespace(
        media_source=MediaSource.Douban,
        media_id="1295644",
        type=MediaType.MOVIE,
    )

    assert plugin._get_previous_media_title(mediainfo) == "统一身份标题"
    plugin.transferhis.get_by_media_identity.assert_called_once_with(
        media_source=MediaSource.Douban,
        media_id="1295644",
        mtype=MediaType.MOVIE.value,
    )

    plugin.transferhis.reset_mock()
    mediainfo.media_id = "0"
    assert plugin._get_previous_media_title(mediainfo) is None
    plugin.transferhis.get_by_media_identity.assert_not_called()


def test_tmdb_episodes_converts_non_tmdb_identity() -> None:
    """来源链只接收 TMDB ID，非 TMDB 媒体必须先走统一身份转换。"""
    plugin = CloudLinkMonitor()
    plugin.mediachain = Mock()
    plugin.tmdbchain = Mock()
    plugin.mediachain.convert_media_identity.return_value = {"id": 1399}
    plugin.tmdbchain.tmdb_episodes.return_value = [SimpleNamespace(episode_number=1)]
    mediainfo = SimpleNamespace(
        media_source=MediaSource.Douban,
        media_id="34912145",
        type=MediaType.TV,
    )

    result = plugin._get_tmdb_episodes(mediainfo=mediainfo, season=2)

    assert len(result) == 1
    plugin.mediachain.convert_media_identity.assert_called_once_with(
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="34912145",
        mtype=MediaType.TV,
        season=2,
    )
    plugin.tmdbchain.tmdb_episodes.assert_called_once_with(tmdbid=1399, season=2)


def test_redo_hint_uses_complete_media_identity() -> None:
    """手动整理提示必须与 V3 `/redo` 的三段身份参数一致。"""
    assert CloudLinkMonitor._redo_hint(7) == (
        "/redo 7 [media_source]|[media_id]|[类型]"
    )
    assert MessageType.Manual.value


def test_v3_manifest_and_import_contracts() -> None:
    """V3 索引、旧代回退开关与严格导入边界应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudLinkMonitor"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["CloudLinkMonitor"]

    assert manifest["version"] == CloudLinkMonitor.plugin_version
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["release"] is True
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.utilities",
    }.issubset(imports)
    assert "app.application.history" in imports
    assert "app.application.directory" not in imports
    assert "app.modules.filemanager" not in imports
    forbidden_prefixes = (
        "app.adapters",
        "app.core",
        "app.db.models",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "app.log" not in imports
    assert "app.db.transferhistory_oper" not in imports
