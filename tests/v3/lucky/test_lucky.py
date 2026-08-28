from __future__ import annotations

import ast
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(os.environ["MOVIEPILOT_BACKEND_PATH"])
sys.path.insert(0, str(BACKEND_ROOT))

from app.testing.bootstrap import prepare_v3_backend

prepare_v3_backend(REPOSITORY_ROOT)

from app.plugins.lucky import Lucky, LuckyStatusData
from app.application.chain.context import (
    ChainRuntimeContext,
    configure_chain_runtime_context_provider,
)
from app.application.chain.data import (
    configure_chain_data_ports,
    get_chain_data_ports,
)


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "lucky" / "__init__.py"

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
    """为插件基类提供隔离的链运行时上下文。"""
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
    """返回插件源码使用的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 元数据、旧代路由和公开 SDK 导入必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["Lucky"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["Lucky"]

    assert manifest["version"] == Lucky.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy_manifest["v2"] is True
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.logging",
        "app.sdk.network",
        "app.sdk.utilities",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.core",
        "app.domain",
        "app.foundation",
        "app.helper",
        "app.runtime",
        "app.sdk._legacy",
        "app.utils",
    )
    assert not any(module.startswith(forbidden_prefixes) for module in imports)


def test_init_resets_hot_reload_state_and_declares_api_model() -> None:
    """重复初始化不得保留旧地址或凭据，API 必须声明真实响应模型。"""
    plugin = Lucky()
    plugin.init_plugin(
        {"enabled": True, "baseUrl": "https://lucky.example/", "openToken": "secret"}
    )

    assert plugin.get_state() is True
    assert plugin._base_url == "https://lucky.example"
    assert plugin._open_token == "secret"
    assert plugin._request is not None
    assert plugin.get_command() == []
    api = plugin.get_api()[0]
    assert api["auth"] == "apikey"
    assert api["response_model"] is LuckyStatusData

    plugin.init_plugin({})

    assert plugin.get_state() is False
    assert plugin._base_url is None
    assert plugin._open_token is None
    assert plugin._request is None
    _, defaults = plugin.get_form()
    assert defaults == {"enabled": False, "baseUrl": "", "openToken": ""}


def test_request_keeps_token_out_of_url_and_validates_payload() -> None:
    """OpenToken 只能作为请求参数传递，非对象或失败响应应被拒绝。"""

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"ret": 0, "data": []}

    class FakeRequest:
        def __init__(self) -> None:
            self.call = None

        @contextmanager
        def response_manager(self, method, url, **kwargs):
            self.call = (method, url, kwargs)
            yield FakeResponse()

    request = FakeRequest()
    plugin = Lucky()
    plugin._base_url = "https://lucky.example"
    plugin._open_token = "secret"
    plugin._request = request

    assert plugin._request_json("/api/ddnstasklist") == {"ret": 0, "data": []}
    method, url, kwargs = request.call
    assert method == "GET"
    assert url == "https://lucky.example/api/ddnstasklist"
    assert "secret" not in url
    assert kwargs["params"]["openToken"] == "secret"
    assert kwargs["raise_exception"] is True

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = ["unexpected"]

    @contextmanager
    def invalid_response(*_args, **_kwargs):
        yield response

    plugin._request.response_manager = invalid_response
    assert plugin._request_json("/api/ddnstasklist") is None


def test_lucky_aggregates_external_payload_into_declared_model(monkeypatch) -> None:
    """规则、流量、DDNS 与证书响应应稳定映射为声明的数据模型。"""
    payloads = {
        "/api/webservice/rules": {
            "ret": 0,
            "ruleList": [
                {"ProxyList": [{"Enable": True}, {"Enable": False}]},
                {"ProxyList": None},
            ],
            "statistics": {
                "one": {"Connections": 2, "TrafficIn": 1024, "TrafficOut": 2048},
                "two": {"Connections": "3", "TrafficIn": None, "TrafficOut": "1024"},
            },
        },
        "/api/ddnstasklist": {"ret": 0, "data": [{"Ipv4Addr": "192.0.2.1"}]},
        "/api/ssl": {
            "ret": 0,
            "list": [{"CertsInfo": [{"NotAfterTime": "2030-04-05 00:00:00"}]}],
        },
    }
    plugin = Lucky()
    monkeypatch.setattr(plugin, "_request_json", lambda path: payloads[path])

    result = plugin.lucky()
    model = LuckyStatusData.model_validate(result)

    assert model.total_cnt == 2
    assert model.enabled_cnt == 1
    assert model.closed_cnt == 1
    assert model.connections == 5
    assert model.ipaddr == "192.0.2.1"
    assert model.expire_time == "20300405"
    assert model.traffic_in
    assert model.traffic_out
    assert {"trafficIn", "trafficOut"}.issubset(model.model_dump(by_alias=True))


def test_external_shape_failures_return_empty_status(monkeypatch) -> None:
    """Lucky 响应缺字段时应返回稳定空状态，而不是抛出索引或类型异常。"""
    plugin = Lucky()
    monkeypatch.setattr(plugin, "_request_json", lambda _path: None)

    result = LuckyStatusData.model_validate(plugin.lucky())

    assert result.total_cnt == 0
    assert result.connections == 0
    assert result.ipaddr is None
    assert result.expire_time is None
