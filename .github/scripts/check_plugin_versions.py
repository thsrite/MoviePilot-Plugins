#!/usr/bin/env python3
"""校验可发布到 MoviePilot V3 的插件市场版本与源码版本一致。

Release workflow 同时处理 V1、V2 兼容实现和 V3 专用实现。旧索引中显式声明
``v3: false`` 的实现不会再发布；V3 专用实现还必须满足迁移版本和元数据合同。
"""

from __future__ import annotations

import ast
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)


def _load_package(path: Path) -> dict:
    """读取 package 文件；文件不存在时返回空字典。"""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file_obj:
        return json.load(file_obj)


def _plugin_dir(package_file: Path, plugin_id: str) -> Path | None:
    """按 package 文件定位对应插件目录，避免不同代际同名插件互相串线。"""
    plugin_id_lc = plugin_id.lower()
    base_dirs = {
        "package.json": Path("plugins"),
        "package.v2.json": Path("plugins.v2"),
        "package.v3.json": Path("plugins.v3"),
    }
    base_dir = base_dirs.get(package_file.name)
    if base_dir is None:
        return None
    candidate = package_file.parent / base_dir / plugin_id_lc
    return candidate if candidate.is_dir() else None


def _expected_plugin_dir(package_file: Path, plugin_id: str) -> Path:
    """返回 package 条目对应的插件目录，用于缺失目录时输出可定位错误。"""
    plugin_id_lc = plugin_id.lower()
    base_dirs = {
        "package.json": Path("plugins"),
        "package.v2.json": Path("plugins.v2"),
        "package.v3.json": Path("plugins.v3"),
    }
    base_dir = base_dirs.get(package_file.name, Path("plugins.v3"))
    return package_file.parent / base_dir / plugin_id_lc


def _version_parts(value: object) -> tuple[int, ...] | None:
    """解析只含数字段的插件版本。"""
    match = re.fullmatch(r"\d+(?:\.\d+)*", str(value or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group().split("."))


def _check_v3_metadata(path: Path, plugin_id: str, metadata: dict) -> list[str]:
    """校验 V3 专用实现长期有效的发布元数据约束。"""
    errors: list[str] = []
    version = str(metadata.get("version") or "").strip()
    version_parts = _version_parts(version)
    if version_parts is None or len(version_parts) < 2:
        errors.append(f"{path}: {plugin_id} V3 版本必须至少包含主版本和小版本：{version}")

    if metadata.get("system_version") != ">=3.0.0":
        errors.append(f'{path}: {plugin_id} system_version 必须为 ">=3.0.0"')

    history = metadata.get("history")
    expected_history_key = f"v{version}"
    if not isinstance(history, dict) or list(history) != [expected_history_key]:
        errors.append(f"{path}: {plugin_id} history 必须只保留当前版本 {expected_history_key}")
    elif not isinstance(history[expected_history_key], str) or not history[expected_history_key].strip():
        errors.append(f"{path}: {plugin_id} history 当前版本说明不能为空")

    legacy = _legacy_metadata(path, plugin_id)
    if legacy is None:
        errors.append(
            f"{path}: {plugin_id} 在 package.v2.json 或 package.json 中没有对应旧版本条目"
        )
        return errors
    legacy_path, legacy_metadata = legacy
    if legacy_metadata.get("v3") is not False:
        errors.append(f"{legacy_path}: {plugin_id} 必须声明 v3=false")

    return errors


def _plugin_version(init_file: Path) -> str | None:
    """从 __init__.py 类级属性中提取 plugin_version 字面量。"""
    tree = ast.parse(init_file.read_text(encoding="utf-8"), filename=str(init_file))
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for node in class_node.body:
            value_node = None
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "plugin_version" for target in node.targets):
                    value_node = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "plugin_version"
            ):
                value_node = node.value
            if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
                return value_node.value
    return None


def _legacy_metadata(path: Path, plugin_id: str) -> tuple[Path, dict] | None:
    """查找 V3 专用实现对应的旧索引条目。

    旧实现可能位于 ``plugins.v2``，也可能仍由默认索引的 ``plugins`` 提供；
    两种目录拓扑都需要通过同名条目的 ``v3: false`` 阻止 V3 回退加载。
    """
    for legacy_name in ("package.v2.json", "package.json"):
        legacy_path = path.with_name(legacy_name)
        legacy_metadata = _load_package(legacy_path).get(plugin_id)
        if isinstance(legacy_metadata, dict):
            return legacy_path, legacy_metadata
    return None


def _is_v3_release_entry(path: Path, metadata: dict) -> bool:
    """按主程序索引回退规则判断旧代条目是否仍面向 V3 发布。"""
    if metadata.get("release") is not True or metadata.get("v3") is False:
        return False
    if path.name == "package.v2.json":
        return True
    if path.name == "package.json":
        return metadata.get("v3") is True or metadata.get("v2") is True
    return False


def check_package(path: Path) -> list[str]:
    """校验单个 package 文件中仍面向 V3 发布的条目。"""
    errors: list[str] = []
    package = _load_package(path)
    for plugin_id, meta in package.items():
        if not isinstance(meta, dict):
            errors.append(f"{path}: {plugin_id} 元数据必须为对象")
            continue
        if path.name != "package.v3.json" and not _is_v3_release_entry(path, meta):
            continue
        package_version = str(meta.get("version") or "").strip()
        plugin_dir = _plugin_dir(path, plugin_id)
        if not plugin_dir:
            errors.append(f"{path}: {plugin_id} 缺少插件目录 {_expected_plugin_dir(path, plugin_id)}")
            continue
        init_file = plugin_dir / "__init__.py"
        if not init_file.exists():
            errors.append(f"{path}: {plugin_id} 缺少 {init_file}")
            continue
        source_version = _plugin_version(init_file)
        if not source_version:
            errors.append(f"{path}: {plugin_id} 未在 {init_file} 中声明类级 plugin_version")
            continue
        if package_version != source_version:
            errors.append(
                f"{path}: {plugin_id} 版本不一致，package={package_version}, "
                f"plugin_version={source_version} ({init_file})"
            )
        if path.name == "package.v3.json":
            errors.extend(_check_v3_metadata(path, plugin_id, meta))
    return errors


def main() -> int:
    """命令入口：所有 package 均通过时返回 0，否则打印错误并返回 1。"""
    package_files = [Path(arg) for arg in sys.argv[1:]] or [
        Path("package.json"),
        Path("package.v2.json"),
        Path("package.v3.json"),
    ]
    errors: list[str] = []
    for package_file in package_files:
        errors.extend(check_package(package_file))
    if errors:
        print("插件版本门禁失败：")
        for error in errors:
            print(f"- {error}")
        return 1
    print("插件版本门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
