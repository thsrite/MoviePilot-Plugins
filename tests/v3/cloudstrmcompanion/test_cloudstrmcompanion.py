from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import cloudstrmcompanion as cloudstrmcompanion_module
from app.plugins.cloudstrmcompanion import CloudStrmCompanion

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cloudstrmcompanion" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _plugin_version() -> str:
    """读取插件类声明的版本，避免测试复制版本常量。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return next(
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "plugin_version"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def test_v3_manifest_matches_plugin_source() -> None:
    """V3 索引应与独立源码版本及旧代回退开关一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudStrmCompanion"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["CloudStrmCompanion"]

    assert manifest["version"] == _plugin_version()
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["release"] is True
    assert manifest["history"]["v2.0.0"]
    assert legacy_manifest["v3"] is False


def test_v3_source_uses_supported_sdk_and_media_identity() -> None:
    """V3 源码应使用稳定 SDK，并以媒体身份对调用识别链。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    imports = _imports()

    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.network",
        "app.sdk.services",
        "app.sdk.utilities",
    }.issubset(imports)
    forbidden_prefixes = (
        "app.adapters",
        "app.application",
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
    assert "import requests" not in source
    assert "tmdbid=" not in source
    assert "resolve_media_identity(media=file_meta)" in source
    assert "media_source=media_source" in source
    assert "media_id=media_id" in source


def test_export_dir_closes_falsey_error_response(monkeypatch) -> None:
    """HTTP 错误响应即使布尔值为假也必须释放连接。"""

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
    request.post_res.return_value = response
    monkeypatch.setattr(
        cloudstrmcompanion_module,
        "RequestUtils",
        lambda **_kwargs: request,
    )
    plugin = CloudStrmCompanion()

    assert plugin.export_dir("123") is None
    assert response.closed is True
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert source.count("if response is not None:") >= 2


def test_v3_plugin_imports_and_initializes(monkeypatch) -> None:
    """V3 插件应在隔离外部服务后完成正常构造和无任务初始化。"""
    monkeypatch.setattr(cloudstrmcompanion_module, "MediaServerHelper", Mock)

    plugin = CloudStrmCompanion()
    plugin.init_plugin({})

    assert plugin.plugin_version == "2.0.0"
    assert plugin.get_api() == []
    assert plugin.get_page() == []

    plugin.stop_service()


def test_export_dir_uses_sdk_client_and_closes_responses(monkeypatch) -> None:
    """115 目录导出应复用 SDK 客户端，并关闭每个 HTTP 响应。"""
    responses = [
        Mock(
            status_code=200,
            json=Mock(return_value={"state": True, "data": {"export_id": "7"}}),
        ),
        Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "state": True,
                    "data": {"export_id": "7", "pick_code": "pick", "file_id": "9"},
                }
            ),
        ),
    ]
    request = Mock()
    request.post_res.return_value = responses[0]
    request.get_res.return_value = responses[1]
    monkeypatch.setattr(
        cloudstrmcompanion_module,
        "RequestUtils",
        lambda headers: request,
    )

    plugin = CloudStrmCompanion()

    assert plugin.export_dir(fid="1", destination_id="2") == ("pick", "9")
    request.post_res.assert_called_once_with(
        url="https://webapi.115.com/files/export_dir",
        data={"file_ids": "1", "target": "U_1_2"},
    )
    request.get_res.assert_called_once_with(
        url="https://webapi.115.com/files/export_dir",
        data={"export_id": "7"},
    )
    for response in responses:
        response.close.assert_called_once_with()
