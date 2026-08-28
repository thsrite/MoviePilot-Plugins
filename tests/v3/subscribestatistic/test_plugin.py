from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from apscheduler.triggers.cron import CronTrigger

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import subscribestatistic as subscribestatistic_module
from app.plugins.subscribestatistic import SubscribeStatistic
from app.schemas.types import MessageType, SystemConfigKey


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "subscribestatistic" / "__init__.py"

def _imports() -> set[str]:
    """返回 V3 插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _walk_dicts(value):
    """递归遍历 Vuetify 页面描述中的字典节点。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和稳定数据访问入口应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SubscribeStatistic"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["SubscribeStatistic"]
    v2_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )

    assert manifest["version"] == SubscribeStatistic.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v3"] is False
    assert "SubscribeStatistic" not in v2_manifest

    imports = _imports()
    assert {
        "app.db.oper.downloadhistory",
        "app.db.oper.subscribe",
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.network",
        "app.schemas.types",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.core",
        "app.db.downloadhistory_oper",
        "app.db.site_oper",
        "app.db.subscribe_oper",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "NotificationType" not in PLUGIN_PATH.read_text(encoding="utf-8")
    assert SubscribeStatistic().get_command() == []
    assert SubscribeStatistic().get_api() == []


def test_subscribe_statistics_use_v3_opers_and_rss_site_fallback() -> None:
    """订阅统计应使用 V3 Oper，并对未指定站点的订阅使用 RSS 站点配置。"""
    plugin = SubscribeStatistic()
    plugin._movie_subscribe_days = 30
    plugin.subscribe = Mock()
    plugin._sites_helper = Mock()
    plugin.systemconfig = Mock()
    plugin.subscribe.list_by_type.return_value = [
        SimpleNamespace(sites=[1, 2]),
        SimpleNamespace(sites="[2]"),
        SimpleNamespace(sites=None),
    ]
    plugin.systemconfig.get.return_value = [2, 3]
    plugin._sites_helper.get_indexers.return_value = [
        {"id": 1, "name": "站点一"},
        {"id": 2, "name": "站点二"},
        {"id": 3, "name": "站点三"},
    ]

    subscribes, labels, values = plugin._SubscribeStatistic__get_movie_subscribes()

    assert len(subscribes) == 3
    assert labels == ["站点一：1", "站点二：3", "站点三：1"]
    assert values == [1, 3, 1]
    plugin.subscribe.list_by_type.assert_called_once_with(mtype="电影", days=30)
    plugin.systemconfig.get.assert_called_once_with(SystemConfigKey.RssSites)
    plugin._sites_helper.get_indexers.assert_called_once_with()


def test_download_statistics_group_by_torrent_site() -> None:
    """下载统计应通过 V3 下载历史 Oper 按种子站点聚合。"""
    plugin = SubscribeStatistic()
    plugin._movie_download_days = 7
    plugin.downloadhis = Mock()
    plugin.downloadhis.list_by_type.return_value = [
        SimpleNamespace(torrent_site="站点一"),
        SimpleNamespace(torrent_site="站点二"),
        SimpleNamespace(torrent_site="站点一"),
        SimpleNamespace(torrent_site=None),
    ]

    downloads, labels, values = plugin._SubscribeStatistic__get_movie_downloads()

    assert len(downloads) == 4
    assert labels == ["站点一：2", "站点二：1"]
    assert values == [2, 1]
    plugin.downloadhis.list_by_type.assert_called_once_with(mtype="电影", days=7)


def test_notify_uses_message_type_and_sorted_counts(monkeypatch) -> None:
    """通知应将配置中的枚举名称映射为 V3 MessageType，并按数量排序。"""
    plugin = SubscribeStatistic()
    plugin._notify_type = ["movie_downloads"]
    plugin._movie_download_days = 7
    plugin._msgtype = "SiteMessage"
    plugin.post_message = Mock()
    monkeypatch.setattr(
        plugin,
        "_SubscribeStatistic__get_movie_downloads",
        lambda: (
            [SimpleNamespace(), SimpleNamespace(), SimpleNamespace()],
            ["站点一：1", "站点二：2"],
            [1, 2],
        ),
    )

    plugin.notify()

    plugin.post_message.assert_called_once()
    kwargs = plugin.post_message.call_args.kwargs
    assert kwargs["mtype"] is MessageType.SiteMessage
    assert kwargs["title"] == "【订阅下载统计】"
    assert kwargs["text"] == "【电影7天内下载 共3】\n站点二：2\n站点一：1\n"


def test_notify_falls_back_for_invalid_message_type(monkeypatch) -> None:
    """未知消息类型配置不应阻断统计通知。"""
    plugin = SubscribeStatistic()
    plugin._notify_type = []
    plugin._msgtype = "UnknownMessageType"
    plugin.post_message = Mock()

    plugin.notify()

    assert plugin.post_message.call_args.kwargs["mtype"] is MessageType.Manual


def test_init_plugin_projects_host_services_after_consuming_once(monkeypatch) -> None:
    """一次性开关应先持久化消费，再投影宿主 date 与 cron 服务。"""
    subscribe_oper = Mock()
    download_history_oper = Mock()
    sites_helper = Mock()
    monkeypatch.setattr(subscribestatistic_module, "SubscribeOper", lambda: subscribe_oper)
    monkeypatch.setattr(
        subscribestatistic_module,
        "DownloadHistoryOper",
        lambda: download_history_oper,
    )
    monkeypatch.setattr(subscribestatistic_module, "SitesHelper", lambda: sites_helper)

    plugin = SubscribeStatistic()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin(
        {
            "enabled": True,
            "notify": True,
            "onlyonce": True,
            "cron": "5 1 * * *",
            "movie_subscribe_days": 30,
            "tv_subscribe_days": 30,
            "movie_download_days": 7,
            "tv_download_days": 7,
            "notify_type": ["movie_downloads"],
            "msgtype": "Manual",
        }
    )

    assert plugin.subscribe is subscribe_oper
    assert plugin.downloadhis is download_history_oper
    assert plugin._sites_helper is sites_helper
    assert plugin._onlyonce is False
    plugin.update_config.assert_called_once()
    assert plugin.update_config.call_args.args[0]["onlyonce"] is False
    services = plugin.get_service()
    assert [service["id"] for service in services] == [
        "SubscribeStatistic.Once",
        "SubscribeStatistic.Cron",
    ]
    assert services[0]["trigger"] == "date"
    assert services[0]["func"] == plugin.notify
    assert services[0]["kwargs"]["run_date"].tzinfo is not None
    assert isinstance(services[1]["trigger"], CronTrigger)
    assert services[1]["func"] == plugin.notify
    assert plugin.stop_service() is None


@pytest.mark.parametrize("save_result", [False, RuntimeError("write failed")])
def test_once_service_is_not_projected_when_config_persistence_fails(
    save_result, monkeypatch
) -> None:
    """一次性开关持久化失败时不得投影可重复执行的副作用。"""
    plugin = SubscribeStatistic()
    if isinstance(save_result, Exception):
        plugin.update_config = Mock(side_effect=save_result)
    else:
        plugin.update_config = Mock(return_value=save_result)

    plugin.init_plugin(
        {
            "enabled": True,
            "notify": True,
            "onlyonce": True,
            "notify_type": ["movie_downloads"],
            "msgtype": "Manual",
        }
    )

    assert plugin._run_once is False
    assert plugin.get_service() == []


def test_init_plugin_normalizes_legacy_notify_type() -> None:
    """旧配置中的单字符串推送类型应转换为 V3 多选字段格式。"""
    plugin = SubscribeStatistic()
    plugin.init_plugin({"notify_type": "movie_downloads"})

    assert plugin._notify_type == ["movie_downloads"]

    plugin.init_plugin({"notify_type": None})
    assert plugin._notify_type == []


def test_form_defaults_and_page_charts() -> None:
    """配置默认值和详情页图表应保留旧插件的四类统计选择。"""
    plugin = SubscribeStatistic()
    form, defaults = plugin.get_form()

    assert defaults == {
        "enabled": False,
        "notify": False,
        "onlyonce": False,
        "cron": "5 1 * * *",
        "movie_subscribe_days": 30,
        "tv_subscribe_days": 30,
        "movie_download_days": 7,
        "tv_download_days": 7,
        "notify_type": ["movie_downloads"],
        "msgtype": [],
    }
    labels = {
        node["props"]["label"]
        for node in _walk_dicts(form)
        if node.get("component") in {"VSwitch", "VTextField", "VSelect"}
    }
    assert {
        "启用插件",
        "发送通知",
        "立即运行一次",
        "电影订阅天数",
        "电视剧订阅天数",
        "电影下载天数",
        "电视剧下载天数",
        "执行周期",
        "消息类型",
        "推送类型",
    }.issubset(labels)

    plugin._enabled = True
    plugin._notify_type = [
        "movie_subscribes",
        "tv_subscribes",
        "movie_downloads",
        "tv_downloads",
    ]
    plugin._movie_subscribe_days = 30
    plugin._tv_subscribe_days = 30
    plugin._movie_download_days = 7
    plugin._tv_download_days = 7
    plugin._SubscribeStatistic__get_movie_subscribes = Mock(
        return_value=([SimpleNamespace()], ["站点一：1"], [1])
    )
    plugin._SubscribeStatistic__get_tv_subscribes = Mock(
        return_value=([SimpleNamespace()], ["站点二：1"], [1])
    )
    plugin._SubscribeStatistic__get_movie_downloads = Mock(
        return_value=([SimpleNamespace()], ["站点三：1"], [1])
    )
    plugin._SubscribeStatistic__get_tv_downloads = Mock(
        return_value=([SimpleNamespace()], ["站点四：1"], [1])
    )

    charts = [node for node in _walk_dicts(plugin.get_page()) if node.get("component") == "VApexChart"]

    assert len(charts) == 4
    assert [chart["props"]["series"] for chart in charts] == [[1], [1], [1], [1]]
