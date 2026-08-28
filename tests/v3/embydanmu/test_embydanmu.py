from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import embydanmu as embydanmu_module
from app.plugins.embydanmu import EmbyDanmu
from app.sdk.events import Event
from app.schemas.types import EventType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embydanmu" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _service(
    *,
    host: str = "emby.example:8096",
    api_key: str = "api-key",
    user: str = "user-1",
) -> SimpleNamespace:
    """构造符合 V3 MediaServerHelper 返回结构的最小 Emby 服务。"""
    return SimpleNamespace(
        config=SimpleNamespace(config={"host": host, "apikey": api_key}),
        instance=SimpleNamespace(get_user=Mock(return_value=user)),
    )


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引应与源码版本一致，并阻止旧代实现回退加载。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyDanmu"]
    legacy_v1 = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["EmbyDanmu"]
    legacy_v2 = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyDanmu"]

    assert manifest["version"] == EmbyDanmu.plugin_version == "3.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["history"]["v3.0.0"]
    assert legacy_v1["v3"] is False
    assert legacy_v2["v3"] is False


def test_v3_source_uses_sdk_and_has_no_legacy_imports() -> None:
    """V3 源码应只依赖公开 SDK，不应回退到宿主旧代导入路径。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = _imports()

    assert {
        "app.plugins",
        "app.schemas.types",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.services",
    }.issubset(imports)
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
    assert not any(module.startswith(forbidden_prefixes) for module in imports)
    assert "import requests" not in source
    assert "MediaServerChain" not in source


def test_plugin_initializes_without_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    """插件初始化应只重建配置状态，不启动后台服务或外部请求。"""
    helper = Mock()
    monkeypatch.setattr(embydanmu_module, "MediaServerHelper", lambda: helper)

    plugin = EmbyDanmu()
    plugin.init_plugin(
        {
            "enabled": True,
            "disabled": True,
            "mediaservers": ["主 Emby"],
            "dirs": "/emby/media:/movie/media\ninvalid\n:/ignored",
        }
    )

    assert plugin.get_state() is True
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page() == []
    assert plugin._paths == {"/emby/media": "/movie/media"}
    assert plugin._mediaservers == ["主 Emby"]
    assert plugin._library_task == {}
    assert plugin.mediaserver_helper is helper


def test_server_context_normalizes_host_and_requires_complete_credentials() -> None:
    """服务发现结果应转换成统一连接上下文，缺任一凭据都不能执行请求。"""
    plugin = EmbyDanmu()

    assert plugin._EmbyDanmu__set_server_context(_service()) is True
    assert plugin._emby_host == "http://emby.example:8096/"
    assert plugin._emby_api_key == "api-key"
    assert plugin._emby_user == "user-1"

    assert plugin._EmbyDanmu__set_server_context(
        _service(api_key="", user="user-1")
    ) is False
    assert plugin._EmbyDanmu__set_server_context(
        _service(api_key="api-key", user="")
    ) is False


def test_http_helpers_close_falsey_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误响应即使布尔值为假也必须释放连接，避免网络连接池泄漏。"""

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
    monkeypatch.setattr(embydanmu_module, "RequestUtils", lambda **_kwargs: request)

    plugin = EmbyDanmu()
    plugin._emby_host = "https://emby.example/"
    plugin._emby_api_key = "secret"

    assert plugin._EmbyDanmu__get_json("emby/items") is None
    assert response.closed is True


def test_http_helpers_close_successful_text_and_post_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功的文本读取和 JSON 写入也必须在消费后关闭响应。"""
    text_response = Mock(status_code=200, text="ok")
    post_response = Mock(status_code=204)
    request = Mock()
    request.get_res.return_value = text_response
    request.post_res.return_value = post_response
    monkeypatch.setattr(embydanmu_module, "RequestUtils", lambda **_kwargs: request)

    plugin = EmbyDanmu()
    plugin._emby_host = "https://emby.example/"
    plugin._emby_api_key = "secret"

    assert plugin._EmbyDanmu__get_text("api/danmu/item", {"option": "Refresh"}) == "ok"
    assert plugin._EmbyDanmu__post_json("emby/library", {"Id": "library"}) is True
    text_response.close.assert_called_once_with()
    post_response.close.assert_called_once_with()
    request.get_res.assert_called_once_with(
        url="https://emby.example/api/danmu/item",
        params={"option": "Refresh", "api_key": "secret"},
    )
    request.post_res.assert_called_once_with(
        url="https://emby.example/emby/library",
        params={"api_key": "secret"},
        json={"Id": "library"},
    )


def test_danmu_rejects_invalid_season_before_service_lookup() -> None:
    """非法季数应立即通知，不得触发媒体服务器发现。"""
    plugin = EmbyDanmu()
    plugin._enabled = True
    plugin.mediaserver_helper = Mock()
    plugin.post_message = Mock()

    plugin.danmu(
        Event(
            EventType.PluginAction,
            {"action": "embydanmu", "arg_str": "电影库 示例电影 0"},
        )
    )

    plugin.post_message.assert_called_once_with(
        channel=None,
        title="季数必须大于 0",
        userid=None,
    )
    plugin.mediaserver_helper.get_services.assert_not_called()


def test_danmu_dispatches_selected_server_and_media() -> None:
    """远程命令应通过 V3 服务门面筛选 Emby，并传递媒体与季数。"""
    plugin = EmbyDanmu()
    plugin._enabled = True
    helper = Mock()
    helper.get_services.return_value = {"主 Emby": _service()}
    plugin.mediaserver_helper = helper
    plugin._EmbyDanmu__get_danmu_source = Mock(return_value=["弹幕源"])
    plugin._EmbyDanmu__process_server = Mock()

    plugin.danmu(
        Event(
            EventType.PluginAction,
            {
                "action": "embydanmu",
                "arg_str": "电影库 示例电影 2",
                "channel": "telegram",
                "user": "42",
            },
        )
    )

    helper.get_services.assert_called_once_with(
        name_filters=[],
        type_filter="emby",
    )
    plugin._EmbyDanmu__process_server.assert_called_once_with(
        server_name="主 Emby",
        event_data={
            "action": "embydanmu",
            "arg_str": "电影库 示例电影 2",
            "channel": "telegram",
            "user": "42",
        },
        library_name="电影库",
        item_name="示例电影",
        season=2,
    )


def test_danmu_rejects_concurrent_commands_before_switching_server_context() -> None:
    """长任务运行期间应拒绝新命令，避免共享 Emby 凭据被并发覆盖。"""
    plugin = EmbyDanmu()
    plugin._enabled = True
    plugin.mediaserver_helper = Mock()
    plugin.post_message = Mock()
    assert plugin._run_lock.acquire(blocking=False) is True

    try:
        plugin.danmu(
            Event(
                EventType.PluginAction,
                {
                    "action": "embydanmu",
                    "arg_str": "电影库 示例电影",
                    "channel": "telegram",
                    "user": "42",
                },
            )
        )
    finally:
        plugin._run_lock.release()

    plugin.mediaserver_helper.get_services.assert_not_called()
    plugin.post_message.assert_called_once_with(
        channel="telegram",
        title="已有 Emby 弹幕下载任务正在执行，请稍后重试",
        userid="42",
    )


def test_process_server_reenables_then_restores_danmu_and_clears_task() -> None:
    """一次任务结束后，临时启用的 Danmu 开关应恢复且任务锁不能残留。"""
    plugin = EmbyDanmu()
    plugin._disabled = True
    plugin._EmbyDanmu__get_librarys = Mock(
        return_value=[
            {
                "Name": "电影库",
                "Id": "library-1",
                "CollectionType": "movies",
                "LibraryOptions": {"DisabledSubtitleFetchers": ["Danmu", "Other"]},
            }
        ]
    )
    plugin._EmbyDanmu__get_items = Mock(return_value=[])
    plugin._EmbyDanmu__update_library = Mock(return_value=True)
    plugin._EmbyDanmu__notify = Mock()

    plugin._EmbyDanmu__process_server(
        server_name="主 Emby",
        event_data={"channel": "telegram", "user": "42"},
        library_name="电影库",
        item_name=None,
        season=None,
    )

    updates = plugin._EmbyDanmu__update_library.call_args_list
    assert len(updates) == 2
    assert updates[0].args[0] == "library-1"
    assert updates[0].args[1]["DisabledSubtitleFetchers"] == ["Other"]
    assert updates[1].args[1]["DisabledSubtitleFetchers"] == [
        "Danmu",
        "Other",
    ]
    assert plugin._library_task == {}
    plugin._EmbyDanmu__notify.assert_called_once_with(
        {"channel": "telegram", "user": "42"},
        "主 Emby 获取媒体库：电影库的媒体列表失败",
    )


def test_process_movie_reports_existing_danmu_without_remote_call(tmp_path: Path) -> None:
    """电影目录已有弹幕文件时应直接返回，避免重复通知 Emby 下载。"""
    media_dir = tmp_path / "movie"
    media_dir.mkdir()
    (media_dir / "movie.xml").write_text("<d>", encoding="utf-8")

    plugin = EmbyDanmu()
    plugin._EmbyDanmu__get_item_info = Mock(
        return_value={"Path": str(media_dir / "movie.mkv")}
    )
    plugin._EmbyDanmu__download_danmu = Mock()
    plugin._EmbyDanmu__notify = Mock()

    plugin._EmbyDanmu__process_movie(
        server_name="主 Emby",
        event_data={"channel": "telegram", "user": "42"},
        library_name="电影库",
        item={"Id": "movie-1", "Name": "示例电影"},
        special_library=False,
    )

    plugin._EmbyDanmu__download_danmu.assert_not_called()
    plugin._EmbyDanmu__notify.assert_called_once_with(
        {"channel": "telegram", "user": "42"},
        "主 Emby 电影库 示例电影 弹幕已下载完成",
    )


def test_process_movie_waits_after_successful_download(tmp_path: Path) -> None:
    """通知 Emby 成功后应等待文件落盘并发送成功结果。"""
    media_dir = tmp_path / "movie"
    media_dir.mkdir()
    plugin = EmbyDanmu()
    plugin._EmbyDanmu__get_item_info = Mock(
        return_value={
            "Path": str(media_dir / "movie.mkv"),
            "Name": "示例电影",
            "ProductionYear": 2024,
        }
    )
    plugin._EmbyDanmu__download_danmu = Mock(return_value=True)
    plugin._EmbyDanmu__wait_for_movie = Mock(return_value=True)
    plugin._EmbyDanmu__notify = Mock()

    plugin._EmbyDanmu__process_movie(
        server_name="主 Emby",
        event_data={"channel": "telegram", "user": "42"},
        library_name="电影库",
        item={"Id": "movie-1", "Name": "示例电影"},
        special_library=False,
    )

    plugin._EmbyDanmu__download_danmu.assert_called_once_with("movie-1")
    plugin._EmbyDanmu__wait_for_movie.assert_called_once_with(
        media_dir,
        "*.xml",
        {
            "Path": str(media_dir / "movie.mkv"),
            "Name": "示例电影",
            "ProductionYear": 2024,
        },
    )
    assert plugin._EmbyDanmu__notify.call_args_list[-1].args[1] == (
        "主 Emby 电影库 示例电影 下载弹幕文件成功"
    )


def test_process_series_filters_requested_season() -> None:
    """电视剧命令指定季数时只应处理对应季度。"""
    plugin = EmbyDanmu()
    plugin._EmbyDanmu__get_items = Mock(
        return_value=[
            {"Id": "season-1", "IndexNumber": 1},
            {"Id": "season-2", "IndexNumber": 2},
        ]
    )
    plugin._EmbyDanmu__process_season = Mock()

    plugin._EmbyDanmu__process_series(
        server_name="主 Emby",
        event_data={},
        library_name="剧集库",
        series={"Id": "series-1", "Name": "示例剧集"},
        season=2,
    )

    plugin._EmbyDanmu__get_items.assert_called_once_with("series-1")
    plugin._EmbyDanmu__process_season.assert_called_once_with(
        server_name="主 Emby",
        event_data={},
        library_name="剧集库",
        series_name="示例剧集",
        season_item={"Id": "season-2", "IndexNumber": 2},
    )


def test_get_path_uses_path_boundaries() -> None:
    """目录映射必须区分 `/media` 与 `/media2`，避免替换错误路径。"""
    plugin = EmbyDanmu()
    plugin._paths = {"/media": "/mnt/media"}

    assert plugin._EmbyDanmu__get_path("/media/movie/movie.mkv") == (
        "/mnt/media/movie/movie.mkv"
    )
    assert plugin._EmbyDanmu__get_path("/media2/movie.mkv") == "/media2/movie.mkv"
