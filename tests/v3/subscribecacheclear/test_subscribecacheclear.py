from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import subscribecacheclear as subscribecacheclear_module
from app.plugins.subscribecacheclear import SubscribeCacheClear
from app.schemas.types import MediaType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "subscribecacheclear" / "__init__.py"


def _imports() -> set[str]:
    """返回 V3 插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _find_select(form: list[dict]) -> dict:
    """从 Vuetify 表单描述中取出订阅选择器。"""
    for row in form:
        for child in row.get("content", []):
            for column in child.get("content", []):
                for component in column.get("content", []):
                    if component.get("component") == "VSelect":
                        return component
    raise AssertionError("订阅选择器不存在")


def test_v3_identity_manifest_and_sdk_contract() -> None:
    """V3 应发布独立身份，旧实现必须退出 V3 回退候选。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert "SubscribeCacheClear" in manifest
    assert "SubscribeClear" not in manifest
    assert manifest["SubscribeCacheClear"]["version"] == "2.0.0"
    assert manifest["SubscribeCacheClear"]["release"] is True
    assert manifest["SubscribeCacheClear"]["system_version"] == ">=3.0.0"
    assert manifest["SubscribeCacheClear"]["level"] == 2
    assert list(manifest["SubscribeCacheClear"]["history"]) == ["v2.0.0"]
    assert legacy_manifest["SubscribeClear"]["v3"] is False

    assert SubscribeCacheClear.plugin_name == "清理订阅缓存"
    assert SubscribeCacheClear.plugin_version == "2.0.0"
    assert SubscribeCacheClear.plugin_config_prefix == "subscribecacheclear_"
    assert SubscribeCacheClear.auth_level == manifest["SubscribeCacheClear"]["level"] == 2

    imports = _imports()
    assert {
        "app.db.oper.subscribe",
        "app.plugins",
        "app.schemas.types",
        "app.sdk.logging",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db.models",
        "app.db.subscribe_oper",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "SubscribeClear" not in source
    assert "subscribeclear_" not in source


def test_form_lists_only_active_tv_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置表单只展示启用中的电视剧订阅，并调用当前 V3 Oper 合同。"""
    subscribe_oper = Mock()
    subscribe_oper.list.return_value = [
        SimpleNamespace(id=7, name="示例剧", type=MediaType.TV.value),
        SimpleNamespace(id=8, name="示例电影", type=MediaType.MOVIE.value),
    ]
    monkeypatch.setattr(subscribecacheclear_module, "SubscribeOper", lambda: subscribe_oper)

    plugin = SubscribeCacheClear()
    form, defaults = plugin.get_form()

    subscribe_oper.list.assert_called_once_with("R")
    assert _find_select(form)["props"]["items"] == [{"title": "示例剧", "value": 7}]
    assert defaults == {"subscribe_ids": []}


def test_init_clears_notes_and_resets_one_shot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存选项后应清空订阅 note，并把一次性配置重置为空列表。"""
    subscribe_oper = Mock()
    subscribe_oper.update.side_effect = [SimpleNamespace(id=7), SimpleNamespace(id=8)]
    monkeypatch.setattr(subscribecacheclear_module, "SubscribeOper", lambda: subscribe_oper)

    plugin = SubscribeCacheClear()
    plugin.update_config = Mock()
    plugin.init_plugin({"subscribe_ids": [7, 8]})

    assert subscribe_oper.update.call_args_list == [
        ((7, {"note": ""}),),
        ((8, {"note": ""}),),
    ]
    plugin.update_config.assert_called_once_with({"subscribe_ids": []})
    assert plugin.get_state() is False
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_page() == []


def test_legacy_config_prefix_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """V3 不读取旧代配置前缀，避免两个插件身份共享配置。"""
    subscribe_oper = Mock()
    monkeypatch.setattr(subscribecacheclear_module, "SubscribeOper", lambda: subscribe_oper)

    plugin = SubscribeCacheClear()
    plugin.update_config = Mock()
    plugin.init_plugin({"subscribeclear_subscribe_ids": [7]})

    subscribe_oper.update.assert_not_called()
    plugin.update_config.assert_not_called()
