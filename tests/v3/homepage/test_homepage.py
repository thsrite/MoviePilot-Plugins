from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import homepage as homepage_module
from app.plugins.homepage import HomePage, HomePageStatistic
from app.schemas.types import MediaType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "homepage" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_import_contract() -> None:
    """V3 索引、旧代回退和公开接口导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["HomePage"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["HomePage"]

    assert manifest["version"] == HomePage.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.logging",
        "app.sdk.services",
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
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "DirectoryHelper" not in source
    assert "API_TOKEN" not in source
    assert "apikey" not in source

    documentation = (REPOSITORY_ROOT / "docs" / "HomePage.md").read_text(
        encoding="utf-8"
    )
    assert "statistic?token=API_TOKEN" in documentation
    assert "Authorization: Bearer <登录令牌>" in documentation
    assert "V1/V2 旧版插件仍使用 `?apikey=API_TOKEN`" in documentation


def test_api_uses_bearer_auth_and_bare_response_model() -> None:
    """统计接口由宿主认证依赖保护，并直接返回精确模型。"""
    plugin = HomePage()
    api = plugin.get_api()[0]

    assert api["auth"] == "bear"
    assert api["response_model"] is HomePageStatistic
    assert inspect.iscoroutinefunction(api["endpoint"])
    assert "apikey" not in inspect.signature(api["endpoint"]).parameters
    assert plugin.get_command() == []


def test_statistic_aggregates_media_subscribe_and_storage_data(monkeypatch) -> None:
    """统计应汇总媒体库、仅计入电影和电视剧订阅，并去重存储类型。"""
    dashboard = Mock()
    dashboard.media_statistic.return_value = [
        SimpleNamespace(movie_count=2, tv_count=3, episode_count=None, user_count=1),
        SimpleNamespace(movie_count=None, tv_count=1, episode_count=4, user_count=2),
    ]
    subscribe_oper = Mock()
    subscribe_oper.list.return_value = [
        SimpleNamespace(type=MediaType.MOVIE.value),
        SimpleNamespace(type=MediaType.TV.value),
        SimpleNamespace(type=MediaType.TV.value),
        SimpleNamespace(type="音乐"),
    ]
    storage_chain = Mock()
    usage_results = {
        "local": {"success": True, "data": {"total": 1_000, "available": 400}},
        "u115": {"success": True, "data": {"total": 500, "available": 100}},
    }
    storage_chain.manage_storage.side_effect = (
        lambda *, storage, action: usage_results[storage]
    )

    monkeypatch.setattr(homepage_module, "DashboardChain", lambda: dashboard)
    monkeypatch.setattr(homepage_module, "SubscribeOper", lambda: subscribe_oper)
    monkeypatch.setattr(
        homepage_module,
        "StorageHelper",
        SimpleNamespace(
            get_storagies=lambda: [
                SimpleNamespace(type="local"),
                SimpleNamespace(type="u115"),
                SimpleNamespace(type="local"),
            ]
        ),
    )
    monkeypatch.setattr(homepage_module, "StorageChain", lambda: storage_chain)

    result = asyncio.run(HomePage().statistic())

    assert result == HomePageStatistic(
        movie_count=2,
        tv_count=4,
        episode_count=4,
        user_count=3,
        total_storage="1.46K",
        free_storage="500.0B",
        used_storage="1000.0B",
        movie_subscribes=1,
        tv_subscribes=2,
    )
    assert storage_chain.manage_storage.call_count == 2
    assert {
        call.kwargs["storage"]
        for call in storage_chain.manage_storage.call_args_list
    } == {"local", "u115"}
    assert all(
        call.kwargs["action"] == "usage"
        for call in storage_chain.manage_storage.call_args_list
    )


def test_storage_failures_are_isolated(monkeypatch) -> None:
    """存储配置或单个提供方失败时，容量统计应安全回退为零。"""
    monkeypatch.setattr(
        homepage_module.StorageHelper,
        "get_storagies",
        Mock(side_effect=RuntimeError("configuration unavailable")),
    )

    assert HomePage._storage_usage() == (0.0, 0.0)


def test_page_renders_all_statistic_cards(monkeypatch) -> None:
    """详情页应使用同一统计模型渲染八个卡片。"""
    plugin = HomePage()
    monkeypatch.setattr(
        plugin,
        "_collect_statistics",
        lambda: HomePageStatistic(
            movie_count=1,
            tv_count=2,
            episode_count=3,
            user_count=4,
            total_storage="5G",
            free_storage="4G",
            movie_subscribes=5,
            tv_subscribes=6,
        ),
    )

    page = plugin.get_page()

    assert len(page) == 1
    assert [
        card["content"][0]["content"][0]["content"][0]["content"][0]["text"]
        for card in page[0]["content"]
    ] == [
        "电影订阅",
        "电视剧订阅",
        "总空间",
        "剩余空间",
        "电影数量",
        "电视剧数量",
        "电影剧集数量",
        "用户数量",
    ]
