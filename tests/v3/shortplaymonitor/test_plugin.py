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

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports, get_chain_data_ports
from app.plugins import shortplaymonitor as shortplaymonitor_module
from app.plugins.shortplaymonitor import ShortPlayMonitor
from app.schemas.types import MediaSource, MediaType

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

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "shortplaymonitor" / "__init__.py"


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
    """V3 插件应能导入，并在隔离宿主资源后完成生命周期初始化。"""
    monkeypatch.setattr(shortplaymonitor_module, "MediaChain", Mock)
    monkeypatch.setattr(shortplaymonitor_module, "ScrapingChain", Mock)
    monkeypatch.setattr(shortplaymonitor_module, "StorageChain", Mock)
    monkeypatch.setattr(shortplaymonitor_module, "TmdbChain", Mock)
    monkeypatch.setattr(shortplaymonitor_module, "SiteOper", Mock)

    plugin = ShortPlayMonitor()
    previous_observer = Mock()
    plugin._observer = [previous_observer]
    plugin.init_plugin({})

    assert plugin.plugin_version == "5.0.0"
    previous_observer.stop.assert_called_once_with()
    previous_observer.join.assert_called_once_with()
    assert plugin._observer == []
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    plugin.stop_service()


def test_tmdb_episode_lookup_converts_complete_non_tmdb_identity() -> None:
    """来源专用 TMDB 查询前必须从完整通用身份转换。"""
    plugin = ShortPlayMonitor()
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


def test_transfer_uses_v3_fileitem_and_target_directory_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """识别成功后整理调用应传递 FileItem 和显式目标目录配置。"""
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    media_file = source_dir / "Example.S01E01.mp4"
    media_file.write_bytes(b"video")
    monkeypatch.setattr(
        shortplaymonitor_module,
        "MetaInfoPath",
        lambda _path: SimpleNamespace(name="Example", begin_season=1),
    )

    plugin = ShortPlayMonitor()
    plugin._dirconf = {str(source_dir): str(target_dir)}
    plugin._renameconf = {str(source_dir): "false"}
    plugin._coverconf = {str(source_dir): "2:3"}
    plugin._transfer_type = "link"
    plugin._notify = False
    plugin.storagechain = Mock()
    plugin.storagechain.get_file_item.return_value = file_item = SimpleNamespace(
        storage="local",
        path=str(media_file),
    )
    plugin.chain = Mock()
    plugin.chain.recognize_media.return_value = mediainfo = SimpleNamespace(
        media_source=MediaSource.TMDB,
        media_id="1399",
        type=MediaType.TV,
        category="短剧",
        title_year="Example (2026)",
    )
    plugin.chain.transfer.return_value = SimpleNamespace(
        success=True,
        message=None,
        target_diritem=SimpleNamespace(storage="local", path=str(target_dir)),
    )
    plugin.scrapingchain = Mock()
    plugin._get_tmdb_episodes = Mock(return_value=[])

    plugin._ShortPlayMonitor__handle_file(
        is_directory=False,
        event_path=str(media_file),
        source_dir=str(source_dir),
    )

    transfer_kwargs = plugin.chain.transfer.call_args.kwargs
    assert transfer_kwargs["fileitem"] is file_item
    assert transfer_kwargs["mediainfo"] is mediainfo
    assert transfer_kwargs["target_directory"].library_path == target_dir
    assert transfer_kwargs["target_directory"].transfer_type == "link"
    assert "path" not in transfer_kwargs
    assert "target" not in transfer_kwargs
    plugin.scrapingchain.scrape_metadata.assert_called_once()


def test_site_detail_parser_and_http_response_cleanup(monkeypatch, tmp_path: Path) -> None:
    """固定站点解析不依赖宿主 Spider，HTTP 响应在读取后必须关闭。"""
    detail_url = ShortPlayMonitor._extract_detail_url(
        '<html><a href="details.php?id=42">item</a></html>',
        "https://tracker.example/torrents.php",
    )
    assert detail_url == "https://tracker.example/details.php?id=42"

    response = SimpleNamespace(content=b"image", close=Mock())
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(shortplaymonitor_module, "RequestUtils", lambda **_kwargs: request)
    image_path = tmp_path / "poster.jpg"

    plugin = ShortPlayMonitor()
    assert plugin._ShortPlayMonitor__save_image("https://example/image.jpg", image_path)
    assert image_path.read_bytes() == b"image"
    response.close.assert_called_once_with()


def test_v3_manifest_import_and_chain_contracts() -> None:
    """V3 索引、SDK 导入、媒体身份和 Chain 调用应满足严格迁移合同。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["ShortPlayMonitor"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["ShortPlayMonitor"]

    assert manifest["version"] == ShortPlayMonitor.plugin_version == "5.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v5.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.network",
        "app.sdk.utilities",
    }.issubset(imports)
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
    assert "app.modules.indexer.spider" not in imports

    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "mediainfo.tmdb_id" not in source
    assert "NotificationType" not in source

    tree = ast.parse(source)
    transfer_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transfer"
    ]
    assert len(transfer_calls) == 1
    keywords = {keyword.arg for keyword in transfer_calls[0].keywords}
    assert {"fileitem", "meta", "mediainfo", "target_directory"}.issubset(keywords)
    assert {"path", "target"}.isdisjoint(keywords)
