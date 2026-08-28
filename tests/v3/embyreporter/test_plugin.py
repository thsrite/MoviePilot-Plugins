from __future__ import annotations

import ast
import json
import os
import sys
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import configure_chain_data_ports
from app.plugins import embyreporter as embyreporter_module
from app.plugins.embyreporter import EmbyReporter
from app.schemas.types import MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embyreporter" / "__init__.py"

configure_chain_data_ports(
    **{
        name: lambda: Mock()
        for name in (
            "site",
            "subscribe",
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
    """为插件基类提供隔离 Chain 上下文，并在用例后恢复全局提供器。"""
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
        )
    )
    yield
    configure_chain_runtime_context_provider(None)


def _imports() -> set[str]:
    """返回插件源码中显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _find_props(form: object, model: str) -> dict:
    """从 Vuetify 表单树中查找指定 model 的 props。"""
    if isinstance(form, dict):
        props = form.get("props")
        if isinstance(props, dict) and props.get("model") == model:
            return props
        for value in form.values():
            found = _find_props(value, model)
            if found:
                return found
    elif isinstance(form, list):
        for value in form:
            found = _find_props(value, model)
            if found:
                return found
    return {}


def test_v3_manifest_and_strict_sdk_contract() -> None:
    """V3 索引、旧代回退标记和稳定 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyReporter"]
    legacy_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["EmbyReporter"]
    legacy_v2 = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyReporter"]

    assert manifest["version"] == EmbyReporter.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v3.0.0"]
    assert legacy_v1["v3"] is False
    assert legacy_v2["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.services",
        "app.sdk.utilities",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
        "app.core",
        "app.db",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.log",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "BackgroundScheduler" not in source
    assert "NotificationType" not in source


def test_init_exposes_host_services_for_periodic_and_one_shot_runs() -> None:
    """周期和一次性任务都应通过宿主服务目录注册，而非自建调度器。"""
    plugin = EmbyReporter()
    plugin.update_config = Mock(return_value=True)

    plugin.init_plugin({"enabled": False, "onlyonce": True})
    assert plugin.get_state() is True
    once_services = plugin.get_service()
    assert len(once_services) == 1
    assert once_services[0]["id"] == "EmbyReporter.Once"
    assert once_services[0]["trigger"] == "date"
    assert isinstance(once_services[0]["kwargs"]["run_date"], datetime)

    plugin.report = Mock()
    once_services[0]["func"]()
    once_services[0]["func"]()
    plugin.report.assert_called_once_with()
    assert plugin.get_state() is False
    assert plugin.get_service() == []

    plugin.init_plugin({"enabled": True, "cron": "5 1 * * *", "type": "Manual"})
    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["id"] == "EmbyReporter"
    assert services[0]["func"] == plugin.report
    assert services[0]["kwargs"] == {}


def test_form_reads_only_emby_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置页的媒体服务器选项应来自 SDK 服务配置门面。"""
    helper = Mock()
    helper.get_configs.return_value = {
        "主 Emby": SimpleNamespace(name="主 Emby", type="emby"),
        "Jellyfin": SimpleNamespace(name="Jellyfin", type="jellyfin"),
    }
    monkeypatch.setattr(embyreporter_module, "MediaServerHelper", lambda: helper)

    plugin = EmbyReporter()
    plugin.init_plugin({})
    form, defaults = plugin.get_form()

    assert plugin.get_page() == []
    assert plugin.get_api() == []
    assert plugin.get_command() == []
    assert defaults["mediaservers"] == []
    assert _find_props(form, "mediaservers")["items"] == [
        {"title": "主 Emby", "value": "主 Emby"}
    ]


def test_connections_prefer_complete_custom_legacy_configuration() -> None:
    """完整的旧自定义 Emby 配置应覆盖服务发现结果。"""
    helper = Mock()
    plugin = EmbyReporter()
    plugin._mediaserver_helper = helper
    plugin._emby_host = "emby.example/"
    plugin._emby_api_key = "custom-key"

    assert plugin._connections() == [
        embyreporter_module._EmbyConnection(
            name="Emby",
            host="http://emby.example",
            api_key="custom-key",
        )
    ]
    helper.get_services.assert_not_called()


def test_connections_project_selected_service_and_normalize_host() -> None:
    """没有自定义配置时应通过 SDK 服务发现并保留 Emby 用户 ID。"""
    service = SimpleNamespace(
        config=SimpleNamespace(config={"host": "https://emby.example/", "apikey": "key"}),
        instance=SimpleNamespace(get_user=lambda: "user-1"),
    )
    helper = Mock()
    helper.get_services.return_value = {"主 Emby": service}
    plugin = EmbyReporter()
    plugin._mediaserver_helper = helper
    plugin._mediaservers = ["主 Emby"]

    assert plugin._connections() == [
        embyreporter_module._EmbyConnection(
            name="主 Emby",
            host="https://emby.example",
            api_key="key",
            user_id="user-1",
        )
    ]
    helper.get_services.assert_called_once_with(
        name_filters=["主 Emby"],
        type_filter="emby",
    )


def test_http_responses_are_closed_even_when_falsey(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK HTTP 返回的错误或假值响应也必须释放连接。"""

    class FalseyResponse:
        status_code = 500

        def __init__(self) -> None:
            self.closed = False

        def __bool__(self) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    response = FalseyResponse()
    request = Mock()
    request.get_res.return_value = response
    monkeypatch.setattr(embyreporter_module, "RequestUtils", lambda: request)

    plugin = EmbyReporter()
    plugin._connection = embyreporter_module._EmbyConnection(
        name="Emby", host="https://emby.example", api_key="secret"
    )

    assert plugin.primary("item-1") == (False, "🤕Emby 服务器连接失败!")
    assert response.closed is True


def test_get_report_sanitizes_user_id_caps_limit_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Playback Report 查询应限制结果数量、转义用户 ID 并释放响应。"""

    class Response:
        status_code = 200

        def __init__(self) -> None:
            self.closed = False

        def json(self) -> dict:
            return {"colums": ["name"], "results": [["u", "i", "Movie", "片名", 1, 60]]}

        def close(self) -> None:
            self.closed = True

    response = Response()
    request = Mock()
    request.post_res.return_value = response
    monkeypatch.setattr(embyreporter_module, "RequestUtils", lambda: request)

    plugin = EmbyReporter()
    plugin._connection = embyreporter_module._EmbyConnection(
        name="Emby", host="https://emby.example", api_key="secret"
    )

    success, results = plugin.get_report(
        days=7,
        types=plugin.PLAYBACK_REPORTING_TYPE_TVSHOWS,
        user_id="user'1",
        limit=1000,
    )

    assert success is True
    assert results
    assert response.closed is True
    request.post_res.assert_called_once()
    query = request.post_res.call_args.kwargs["data"]["CustomQueryString"]
    assert "ItemType = 'Episode'" in query
    assert "UserId = 'user''1'" in query
    assert "LIMIT 100" in query
    assert request.post_res.call_args.kwargs["params"] == {"api_key": "secret"}


def test_report_sends_two_images_per_server_and_uses_safe_public_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """报告执行应按媒体服务器隔离文件名，并发送电影和电视剧两张切片。"""
    plugin = EmbyReporter()
    plugin._mp_host = "https://moviepilot.example/"
    plugin._type = MessageType.MediaServer.name
    plugin._days = 7
    plugin._show_time = True
    plugin._connections = Mock(
        return_value=[
            embyreporter_module._EmbyConnection(
                name="主/Emby", host="https://emby.example", api_key="secret"
            )
        ]
    )
    plugin.get_report = Mock(
        side_effect=[(True, [["u", "m", "Movie", "电影", 1, 60]]), (True, [])]
    )
    report_path = tmp_path / "report.jpg"
    report_path.write_bytes(b"placeholder")
    plugin.draw = Mock(return_value=report_path)
    split = Mock()
    monkeypatch.setattr(
        EmbyReporter,
        "_public_dir",
        staticmethod(lambda: tmp_path),
    )
    monkeypatch.setattr(
        EmbyReporter,
        "_EmbyReporter__split_image_by_height",
        staticmethod(split),
    )
    plugin.post_message = Mock()

    plugin.report()

    split.assert_called_once_with(
        report_path,
        tmp_path / "report_主Emby",
        plugin._REPORT_PART_HEIGHTS,
    )
    calls = plugin.post_message.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["title"] == "Movies 近7日观影排行"
    assert calls[1].kwargs["title"] == "TV Shows 近7日观影排行"
    assert all(call.kwargs["mtype"] is MessageType.MediaServer for call in calls)
    assert calls[0].kwargs["image"].startswith(
        "https://moviepilot.example/report_主Emby_part_2.jpg?_timestamp="
    )
    assert calls[1].kwargs["image"].startswith(
        "https://moviepilot.example/report_主Emby_part_3.jpg?_timestamp="
    )
    assert calls[0].kwargs["image"].split("?_timestamp=")[-1]
    assert calls[0].kwargs["image"].split("?_timestamp=")[-1] == calls[1].kwargs[
        "image"
    ].split("?_timestamp=")[-1]


def test_layout_keeps_tv_entries_on_the_second_row_when_movies_are_sparse() -> None:
    """电影不足五条时，电视剧仍必须从电视剧行的第一列开始绘制。"""
    layout = EmbyReporter._layout_entries(
        [("电影", 60, b"movie")],
        [("剧集", 120, b"tv")],
    )

    assert [(entry[0], column, offset_y) for entry, column, offset_y in layout] == [
        ("电影", 0, 0),
        ("剧集", 0, 331),
    ]


def test_draw_and_split_report_with_packaged_reference_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """绘图链路应能使用仓库提供的报告素材并生成可切分的 JPEG。"""
    cover = BytesIO()
    Image.new("RGB", (108, 159), "#336699").save(cover, format="PNG")

    plugin = EmbyReporter()
    monkeypatch.setattr(
        EmbyReporter,
        "_public_dir",
        staticmethod(lambda: tmp_path),
    )
    plugin.primary = Mock(return_value=(True, cover.getvalue()))
    plugin.items = Mock(return_value=(True, {"SeriesId": "series-1"}))

    report_path = plugin.draw(
        REPOSITORY_ROOT / "data" / "EmbyReporter" / "res",
        movies=[["user-1", "movie-1", "Movie", "电影", 1, 60]],
        tvshows=[["user-1", "episode-1", "Episode", "剧集", 1, 120]],
        emby_name="主/Emby",
    )

    assert report_path == tmp_path / "report_主Emby.jpg"
    assert report_path.is_file()
    parts = EmbyReporter._EmbyReporter__split_image_by_height(
        report_path,
        tmp_path / "report_主Emby",
        EmbyReporter._REPORT_PART_HEIGHTS,
    )
    assert len(parts) == 3
    assert all(part.is_file() for part in parts)
