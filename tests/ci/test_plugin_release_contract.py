"""验证 Release 版本检查不会固化一次性 V3 迁移约束。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / ".github/scripts/check_plugin_versions.py"


def _write_v3_plugin(repo: Path, metadata: dict) -> None:
    """构造仅含原生 V3 实现的最小插件仓。"""
    plugin_dir = repo / "plugins.v3/example"
    plugin_dir.mkdir(parents=True)
    (repo / "package.json").write_text("{}\n", encoding="utf-8")
    (repo / "package.v2.json").write_text("{}\n", encoding="utf-8")
    (repo / "package.v3.json").write_text(
        json.dumps({"Example": metadata}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        f'class Example:\n    plugin_version = "{metadata["version"]}"\n',
        encoding="utf-8",
    )


def _run_checker(repo: Path) -> subprocess.CompletedProcess[str]:
    """在隔离 fixture 中执行仓库版本检查器。"""
    return subprocess.run(
        [
            "python3",
            str(CHECKER),
            str(repo / "package.json"),
            str(repo / "package.v2.json"),
            str(repo / "package.v3.json"),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_accepts_native_v3_plugin_with_history_and_higher_host_floor(tmp_path: Path) -> None:
    """原生 V3 插件可保留历史，并可要求高于 3.0.0 的宿主版本。"""
    _write_v3_plugin(
        tmp_path,
        {
            "version": "4.1.0",
            "release": True,
            "system_version": ">=3.3.0",
            "history": {"v4.1.0": "当前版本", "v4.0.0": "先前版本"},
        },
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_requires_current_version_as_first_history_entry(tmp_path: Path) -> None:
    """当前版本必须位于 history 首项，确保 Release 说明对应当前资产。"""
    _write_v3_plugin(
        tmp_path,
        {
            "version": "3.1.0",
            "release": True,
            "system_version": ">=3.0.0",
            "history": {"v3.0.0": "旧版本", "v3.1.0": "当前版本"},
        },
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert "history 首项必须为当前版本 v3.1.0" in result.stdout


def test_requires_legacy_entry_to_opt_out_when_it_exists(tmp_path: Path) -> None:
    """同名旧代实现存在时必须退出 V3 回退，避免两个实现同时进入市场。"""
    _write_v3_plugin(
        tmp_path,
        {
            "version": "3.0.0",
            "release": True,
            "system_version": ">=3.0.0",
            "history": {"v3.0.0": "当前版本"},
        },
    )
    (tmp_path / "package.v2.json").write_text(
        json.dumps({"Example": {"version": "2.0.0", "release": True, "v3": True}}),
        encoding="utf-8",
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert "必须声明 v3=false" in result.stdout


def test_rejects_unsorted_history_and_missing_major_migration(tmp_path: Path) -> None:
    """旧代迁移必须跃迁主版本，且多版本 history 必须按语义版本降序排列。"""
    _write_v3_plugin(
        tmp_path,
        {
            "version": "2.6.2",
            "release": True,
            "system_version": ">=3.0.0",
            "history": {"v2.6.2": "错误补丁版本", "v2.7.0": "错误排序"},
        },
    )
    (tmp_path / "package.v2.json").write_text(
        json.dumps({"Example": {"version": "2.6.1", "release": True, "v3": False}}),
        encoding="utf-8",
    )

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert "history 未按语义版本降序排列" in result.stdout
    assert "V3 版本应与旧代 2.6.1 保持大版本跃迁" in result.stdout
