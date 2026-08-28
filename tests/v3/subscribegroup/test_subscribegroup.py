from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import subscribegroup as subscribegroup_module
from app.plugins.subscribegroup import SubscribeGroup
from app.schemas.types import EventType, MediaSource
from app.sdk.events import Event

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "subscribegroup" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SubscribeGroup"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["SubscribeGroup"]

    assert manifest["version"] == SubscribeGroup.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v3.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {"app.sdk.events", "app.sdk.logging", "app.sdk.media"}.issubset(imports)
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
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "tmdbid" not in source
    assert "list_by_tmdbid" not in source
    assert '"vale"' not in source


def test_category_config_is_case_insensitive(monkeypatch) -> None:
    """配置键和分类名称应忽略大小写，并通过当前 SubscribeOper 更新订阅。"""
    download_history_oper = Mock()
    subscribe_oper = Mock()
    subscribe_oper.get.return_value = SimpleNamespace(name="示例剧", year="2026")
    site_oper = Mock()
    site_oper.list_active.return_value = []
    monkeypatch.setattr(subscribegroup_module, "DownloadHistoryOper", lambda: download_history_oper)
    monkeypatch.setattr(subscribegroup_module, "SubscribeOper", lambda: subscribe_oper)
    monkeypatch.setattr(subscribegroup_module, "SiteOper", lambda: site_oper)

    plugin = SubscribeGroup()
    plugin.get_data = Mock(return_value=[])
    plugin.save_data = Mock()
    plugin.init_plugin(
        {
            "category": True,
            "update_confs": "CATEGORY:Anime#INCLUDE:ReleaseGroup",
        }
    )

    plugin.subscribe_notice(
        Event(
            EventType.SubscribeAdded,
            {
                "subscribe_id": 7,
                "mediainfo": {"category": "anime"},
            },
        )
    )

    subscribe_oper.update.assert_called_once_with(7, {"include": "ReleaseGroup"})
    assert plugin.get_command() == []
    assert plugin.get_api() == []


def test_download_fill_uses_media_identity_and_single_season() -> None:
    """下载填充应按完整媒体身份查询，并只更新下载记录对应的单季订阅。"""
    plugin = SubscribeGroup()
    plugin._enabled = True
    plugin._update_details = ["制作组"]
    plugin._downloadhistoryoper = Mock()
    plugin._subscribeoper = Mock()
    plugin.get_data = Mock(return_value=[])
    plugin.save_data = Mock()

    plugin._downloadhistoryoper.get_by_hash.return_value = SimpleNamespace(
        type="电视剧",
        title="示例剧",
        media_source=MediaSource.Douban,
        media_id="34912145",
        seasons="S02",
    )
    season_one = SimpleNamespace(
        id=1,
        name="示例剧",
        type="电视剧",
        season=1,
        resolution=None,
        quality=None,
        effect=None,
        include=None,
        sites=[],
    )
    season_two = SimpleNamespace(
        id=2,
        name="示例剧",
        type="电视剧",
        season=2,
        resolution=None,
        quality=None,
        effect=None,
        include=None,
        sites=[],
    )
    plugin._subscribeoper.list_by_media_identity.return_value = [season_one, season_two]
    context = SimpleNamespace(
        torrent_info=SimpleNamespace(site=7),
        meta_info=SimpleNamespace(resource_team="Group", customization=None),
    )

    plugin.download_notice(
        Event(EventType.DownloadAdded, {"hash": "hash-1", "context": context})
    )

    plugin._subscribeoper.list_by_media_identity.assert_called_once_with(
        media_source=MediaSource.Douban,
        media_id="34912145",
    )
    plugin._subscribeoper.update.assert_called_once_with(2, {"include": "Group"})
    saved_values = [
        call.kwargs.get("value", call.args[1] if len(call.args) > 1 else None)
        for call in plugin.save_data.call_args_list
    ]
    assert any(value == ["电视剧:douban:34912145"] for value in saved_values)
