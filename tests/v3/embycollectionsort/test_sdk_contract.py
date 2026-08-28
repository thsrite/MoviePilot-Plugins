from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import embycollectionsort as embycollectionsort_module
from app.plugins.embycollectionsort import EmbyCollectionSort
from app.schemas.types import EventType


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embycollectionsort" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中显式声明的 from-import 模块。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_matches_source_and_disables_legacy_fallback() -> None:
    """V3 索引应与源码版本一致，并阻止旧代索引回退加载。"""
    v3_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyCollectionSort"]
    assert v3_manifest["version"] == EmbyCollectionSort.plugin_version == "2.0.0"
    assert v3_manifest["release"] is True
    assert v3_manifest["system_version"] == ">=3.0.0"
    assert list(v3_manifest["history"]) == ["v2.0.0"]

    for package_name in ("package.json", "package.v2.json"):
        legacy_manifest = json.loads(
            (REPOSITORY_ROOT / package_name).read_text(encoding="utf-8")
        )["EmbyCollectionSort"]
        assert legacy_manifest["v3"] is False


def test_v3_source_uses_public_sdk_and_host_services() -> None:
    """V3 源码不得回退到旧代宿主模块或插件自有调度器。"""
    imports = _imports()
    assert {
        "app.plugins",
        "app.schemas.types",
        "app.sdk.config",
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
        "app.modules",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)

    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "BackgroundScheduler" not in source
    assert "settings.EMBY_HOST" not in source
    assert "settings.EMBY_API_KEY" not in source
    assert "logger.warn(" not in source


def test_datetime_parser_normalizes_emby_variants() -> None:
    """Emby 时间应统一为无时区 UTC，且截断服务端可能返回的七位小数。"""
    parse = EmbyCollectionSort._EmbyCollectionSort__parse_emby_datetime

    assert parse("2024-01-02T03:04:05.1234567Z") == datetime(
        2024, 1, 2, 3, 4, 5, 123456
    )
    assert parse("2024-01-02T11:04:05+08:00") == datetime(2024, 1, 2, 3, 4, 5)
    assert parse("not-an-emby-date") is None


def test_sorted_items_filters_invalid_dates_and_sorts_by_premiere() -> None:
    """排序只处理有完整日期的条目，并按发布日期升序排列。"""
    plugin = EmbyCollectionSort()
    item_info = {
        "item-a": {
            "Id": "item-a",
            "Name": "较新电影",
            "PremiereDate": "2024-02-01T00:00:00Z",
            "DateCreated": "2024-01-01T00:00:00Z",
        },
        "item-b": {
            "Id": "item-b",
            "Name": "较早电影",
            "PremiereDate": "2024-01-01T00:00:00Z",
            "DateCreated": "2024-01-02T00:00:00Z",
        },
        "item-invalid": {
            "Id": "item-invalid",
            "PremiereDate": "2024-03-01T00:00:00Z",
            "DateCreated": "",
        },
    }
    plugin._EmbyCollectionSort__get_item_info = Mock(
        side_effect=lambda item_id: item_info[item_id]
    )

    result = plugin._EmbyCollectionSort__sorted_items(
        [
            {"Id": "item-a", "Name": "较新电影"},
            {"Id": "item-b", "Name": "较早电影"},
            {"Id": "item-invalid", "Name": "无效电影"},
        ]
    )

    assert [item["Id"] for item in result] == ["item-b", "item-a"]


def test_http_responses_are_closed_and_api_key_stays_in_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 返回的响应无论成功与否都应关闭，API key 不应拼入 URL。"""

    class Response:
        def __init__(self, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}
            self.closed = False

        def json(self) -> dict:
            return self.payload

        def close(self) -> None:
            self.closed = True

    get_response = Response(200, {"Items": []})
    post_response = Response(204)
    class Request:
        def __init__(self) -> None:
            self.get_call: tuple[tuple, dict] | None = None
            self.post_call: tuple[tuple, dict] | None = None

        def get_res(self, *args, **kwargs):
            self.get_call = args, kwargs
            return get_response

        def post_res(self, *args, **kwargs):
            self.post_call = args, kwargs
            return post_response

        @contextmanager
        def response_manager(self, *args, **kwargs):
            """兼容旧实现的上下文入口，同时记录其请求参数供迁移门禁断言。"""
            if args and str(args[0]).upper() == "GET":
                self.get_call = args, kwargs
                response = get_response
            else:
                self.post_call = args, kwargs
                response = post_response
            try:
                yield response
            finally:
                response.close()

    request = Request()
    monkeypatch.setattr(embycollectionsort_module, "RequestUtils", lambda **_kwargs: request)

    plugin = EmbyCollectionSort()
    plugin._EMBY_HOST = "https://emby.example"
    plugin._EMBY_USER = "user-id"
    plugin._EMBY_APIKEY = "secret"

    assert plugin._EmbyCollectionSort__get_items("library-id") == []
    assert get_response.closed is True
    assert request.get_call is not None
    get_args, get_kwargs = request.get_call
    get_url = get_kwargs.get("url") or (get_args[1] if len(get_args) > 1 else "")
    get_params = get_kwargs.get("params")
    assert get_url == "https://emby.example/emby/Users/user-id/Items"
    assert get_params == {"ParentId": "library-id", "api_key": "secret"}
    assert "api_key" not in get_url

    assert plugin._EmbyCollectionSort__update_item_info("item-id", {"Id": "item-id"})
    assert post_response.closed is True
    assert request.post_call is not None
    post_args, post_kwargs = request.post_call
    if post_kwargs.get("url"):
        post_url = post_kwargs["url"]
    elif len(post_args) >= 3:
        post_url = post_args[2]
    else:
        post_url = post_args[1] if len(post_args) > 1 else ""
    assert post_url == "https://emby.example/emby/Items/item-id"
    assert post_kwargs.get("params") == {"api_key": "secret"}
    assert "api_key" not in post_url


def test_sort_collection_writes_descending_created_dates_without_mutating_input() -> None:
    """合集排序应从现有最大入库时间倒序分配，并保持读取结果不可变。"""
    plugin = EmbyCollectionSort()
    plugin._sort_type = "升序"
    item_info = {
        "item-a": {
            "Id": "item-a",
            "Name": "较新电影",
            "PremiereDate": "2024-02-01T00:00:00Z",
            "DateCreated": "2024-01-01T00:00:00Z",
        },
        "item-b": {
            "Id": "item-b",
            "Name": "较早电影",
            "PremiereDate": "2024-01-01T00:00:00Z",
            "DateCreated": "2024-01-02T00:00:00Z",
        },
    }
    plugin._EmbyCollectionSort__get_items = Mock(
        return_value=[
            {"Id": "item-a", "Name": "较新电影"},
            {"Id": "item-b", "Name": "较早电影"},
        ]
    )
    plugin._EmbyCollectionSort__get_item_info = Mock(
        side_effect=lambda item_id: item_info[item_id]
    )
    plugin._EmbyCollectionSort__update_item_info = Mock(return_value=True)

    plugin._EmbyCollectionSort__sort_collection(
        {"Id": "collection-id", "Name": "示例合集"}, set()
    )

    updates = plugin._EmbyCollectionSort__update_item_info.call_args_list
    assert [call.args[0] for call in updates] == ["item-b", "item-a"]
    assert updates[0].args[1]["DateCreated"] == "2024-01-02T00:00:00.0000000Z"
    assert updates[1].args[1]["DateCreated"] == "2024-01-01T23:59:59.0000000Z"
    assert item_info["item-a"]["DateCreated"] == "2024-01-01T00:00:00Z"
    assert item_info["item-b"]["DateCreated"] == "2024-01-02T00:00:00Z"


def test_server_selection_normalizes_connection_and_stop_clears_projection() -> None:
    """服务投影应从 V3 服务对象读取配置，并在任务结束后清理凭据。"""
    plugin = EmbyCollectionSort()
    service = SimpleNamespace(
        config=SimpleNamespace(config={"host": "emby.example/", "apikey": " secret "}),
        instance=SimpleNamespace(get_user=Mock(return_value="user-id")),
    )

    connection = plugin._EmbyCollectionSort__select_server(service)

    assert connection is not None
    assert connection.host == "http://emby.example"
    assert connection.user_id == "user-id"
    assert connection.api_key == "secret"
    assert plugin._EMBY_HOST == connection.host
    plugin.stop_service()
    assert plugin._connection is None
    assert plugin._EMBY_HOST is None
    assert plugin._EMBY_USER is None
    assert plugin._EMBY_APIKEY is None


def test_collection_sort_isolates_each_media_server_context() -> None:
    """多媒体服务器处理不得串用上一台服务的地址、用户或 API key。"""
    plugin = EmbyCollectionSort()
    plugin._collection_library_id = "library-id"
    servers = {
        "主 Emby": SimpleNamespace(
            config=SimpleNamespace(config={"host": "emby-one", "apikey": "key-one"}),
            instance=SimpleNamespace(get_user=Mock(return_value="user-one")),
        ),
        "备用 Emby": SimpleNamespace(
            config=SimpleNamespace(config={"host": "emby-two", "apikey": "key-two"}),
            instance=SimpleNamespace(get_user=Mock(return_value="user-two")),
        ),
    }
    helper = Mock()
    helper.get_services.return_value = servers
    plugin._mediaserver_helper = helper
    contexts: list[tuple[str | None, str | None, str | None]] = []

    def get_items(_parent_id: str) -> list[dict]:
        contexts.append((plugin._EMBY_HOST, plugin._EMBY_USER, plugin._EMBY_APIKEY))
        return []

    plugin._EmbyCollectionSort__get_items = Mock(side_effect=get_items)

    assert plugin.collection_sort() is True
    assert contexts == [
        ("http://emby-one", "user-one", "key-one"),
        ("http://emby-two", "user-two", "key-two"),
    ]
    assert plugin._connection is None


def test_init_plugin_registers_host_services_without_private_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置初始化应只生成宿主服务描述，不创建插件私有调度器。"""

    class Scheduler:
        """防止旧实现的私有调度器在合同测试中启动后台线程。"""

        running = False

        def __init__(self, *_args, **_kwargs) -> None:
            return None

        def remove_all_jobs(self) -> None:
            return None

        def shutdown(self) -> None:
            self.running = False

        def add_job(self, *_args, **_kwargs) -> None:
            return None

        def get_jobs(self) -> list:
            return []

        def print_jobs(self) -> None:
            return None

        def start(self) -> None:
            self.running = True

    monkeypatch.setattr(
        embycollectionsort_module,
        "BackgroundScheduler",
        Scheduler,
        raising=False,
    )
    plugin = EmbyCollectionSort()
    plugin.update_config = Mock()

    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "cron": "*/5 * * * *",
            "sort_type": "降序",
            "collection_library_id": "library-id",
            "mediaservers": ["主 Emby"],
            "black_collection": "忽略",
        }
    )

    services = plugin.get_service()
    assert {service["id"] for service in services} == {
        "EmbyCollectionSort.Once",
        "EmbyCollectionSort",
    }
    once_service = next(
        service
        for service in services
        if service["id"] == "EmbyCollectionSort.Once"
    )
    assert once_service["trigger"] == "date"
    assert "run_date" not in once_service
    assert isinstance(once_service["kwargs"]["run_date"], datetime)
    assert {
        service["id"] for service in plugin.get_service()
    } == {
        "EmbyCollectionSort.Once",
        "EmbyCollectionSort",
    }

    plugin.collection_sort = Mock(return_value=True)
    assert once_service["func"]() is True
    plugin.collection_sort.assert_called_once_with()
    assert {
        service["id"] for service in plugin.get_service()
    } == {"EmbyCollectionSort"}
    assert plugin.get_state() is True
    assert not hasattr(plugin, "_scheduler")
    plugin.update_config.assert_called_once_with(
        {
            "onlyonce": False,
            "cron": "*/5 * * * *",
            "enabled": True,
            "sort_type": "降序",
            "collection_library_id": "library-id",
            "mediaservers": ["主 Emby"],
            "black_collection": "忽略",
        }
    )
    plugin.stop_service()
