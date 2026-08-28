"""插件仓测试引导薄壳。

本模块只负责定位同级 MoviePilot 后端并委托主程序共享测试引导；CONFIG_DIR 隔离、数据库建表、
插件目录注入和网络守卫均由 ``app.testing`` 维护，确保主程序与插件仓使用同一份测试合同。
所有引导函数必须在首次导入 ``app.*`` 或插件包之前可用。
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGINS_REPO = _TESTS_DIR.parent
_WORKSPACE_ROOT = _PLUGINS_REPO.parent


def _resolve_backend_path() -> Path:
    """定位 MoviePilot 后端目录，并校验其包含 ``app/``。"""
    candidates = []
    env = os.environ.get("MOVIEPILOT_BACKEND_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(_WORKSPACE_ROOT / "MoviePilot")
    for path in candidates:
        if (path / "app").is_dir():
            return path
    raise RuntimeError(
        "未找到 MoviePilot 后端（app/ 不存在）。请设置 MOVIEPILOT_BACKEND_PATH，"
        f"或将后端置于插件仓同级目录。已尝试: {[str(candidate) for candidate in candidates]}"
    )


_BACKEND_PATH = _resolve_backend_path()
if str(_BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(_BACKEND_PATH))

_bootstrap = import_module("app.testing.bootstrap")
block_real_network = import_module("app.testing.network_guard").block_real_network


def isolate_config_dir() -> str:
    """隔离 CONFIG_DIR 到进程私有临时目录。"""
    return _bootstrap.isolate_config_dir()


def prepare_backend() -> None:
    """准备隔离数据库和主程序测试依赖。"""
    _bootstrap.prepare_backend()


def prepare_v2_backend() -> None:
    """准备 V2 兼容插件测试后端并暴露本仓插件目录。"""
    _bootstrap.prepare_v2_backend(_PLUGINS_REPO)


def prepare_v3_backend() -> None:
    """准备 V3 插件测试后端并暴露本仓插件目录。"""
    _bootstrap.prepare_v3_backend(_PLUGINS_REPO)


def prepare_v1_backend() -> None:
    """准备 V1 历史插件测试后端并暴露本仓插件目录。"""
    _bootstrap.prepare_v1_backend(_PLUGINS_REPO)
