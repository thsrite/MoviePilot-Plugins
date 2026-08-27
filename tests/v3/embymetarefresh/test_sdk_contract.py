import ast
import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "embymetarefresh" / "__init__.py"


def _imports() -> set[str]:
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_matches_plugin_source() -> None:
    """V3 索引应指向独立源码，并与插件版本保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["EmbyMetaRefresh"]
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    version = next(
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "plugin_version"
            for target in node.targets
        )
        and isinstance(node.value, ast.Constant)
    )

    assert manifest["version"] == version
    assert manifest["system_version"] == ">=3.0.0"
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["EmbyMetaRefresh"]
    assert legacy_manifest["v3"] is False


def test_v3_source_uses_supported_plugin_apis() -> None:
    """V3 源码应使用稳定 SDK，且不得回退到内部兼容路径或转换分发包。"""
    imports = _imports()
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert {
        "app.sdk.cache",
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
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
    assert "app.db.transferhistory_oper" not in imports
    assert "app.log" not in imports
    assert "zhconv_rs" not in source
    assert "from zhconv" not in source
    assert ".tmdbid" not in source
    assert "self.get_data_path() / \"tmdb_cache\"" in source
