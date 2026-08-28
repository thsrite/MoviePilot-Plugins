"""pytest 全局引导：按目标路径准备 V3 插件测试或非运行时合同测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ._bootstrap import (
    block_real_network,  # noqa: F401  导入主程序共享的 autouse 网络守卫
    isolate_config_dir,
    prepare_v1_backend,
    prepare_v2_backend,
    prepare_v3_backend,
)


_TESTS_DIR = Path(__file__).resolve().parent


def _path_generation(path: Path) -> str | None:
    """返回单个测试路径所属代际；静态合同测试不绑定插件运行时。"""
    normalized = path.resolve().as_posix().replace("\\", "/")
    for generation in ("v1", "v2", "v3", "ci"):
        if f"tests/{generation}" in normalized:
            return generation
    return None


def _contained_runtime_generations(path: Path) -> set[str]:
    """整仓或 tests 目录目标按实际存在的代际目录推导运行时。"""
    resolved = path.resolve()
    if not resolved.is_dir():
        return set()
    try:
        _TESTS_DIR.relative_to(resolved)
    except ValueError:
        if resolved != _TESTS_DIR:
            return set()
    return {
        generation
        for generation in ("v1", "v2", "v3")
        if (_TESTS_DIR / generation).is_dir()
    }


def _selected_generation(config) -> str:
    """根据 pytest 目标选择唯一运行时代际，静态测试可与其同会话执行。"""
    runtime_generations = set()
    static_generations = set()
    for arg in config.args:
        file_part = arg.split("::", 1)[0]
        path = Path(file_part).resolve()
        generation = _path_generation(path)
        if generation in {"v1", "v2", "v3"}:
            runtime_generations.add(generation)
        elif generation == "ci":
            static_generations.add(generation)
        else:
            runtime_generations.update(_contained_runtime_generations(path))

    if len(runtime_generations) == 1:
        return next(iter(runtime_generations))
    if not runtime_generations:
        return "ci" if static_generations else "meta"
    raise RuntimeError(
        "插件仓单测必须按 tests/v1、tests/v2、tests/v3 独立会话运行，"
        "避免同名插件包冲突；tests/ci 和根合同测试不加载插件运行时"
    )


def pytest_configure(config) -> None:
    """在收集测试模块前隔离配置并准备对应的测试运行时。"""
    generation = _selected_generation(config)
    if generation in {"ci", "meta"}:
        isolate_config_dir()
        return
    if generation == "v3":
        prepare_v3_backend()
        return
    if generation == "v2":
        prepare_v2_backend()
        return
    prepare_v1_backend()


@pytest.fixture(autouse=True)
def configure_plugin_test_services(request):
    """为插件逻辑测试装配隔离数据库上的 Chain 与系统配置。"""
    if _path_generation(Path(str(request.node.path))) not in {"v1", "v2", "v3"}:
        yield
        return

    from app.application.chain.context import (
        ChainRuntimeContext,
        configure_chain_runtime_context_provider,
    )
    from app.application.chain.data import configure_chain_data_ports
    from app.application.configuration import SystemConfigService, configure_system_config
    from app.db.oper.systemconfig import SystemConfigOper

    port_names = (
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
    configure_chain_data_ports(**{name: MagicMock for name in port_names})
    context = ChainRuntimeContext(
        module_manager=MagicMock(),
        plugin_manager=MagicMock(),
        event_manager=MagicMock(),
        message_oper=MagicMock(),
        message_helper=MagicMock(),
        file_cache=MagicMock(),
        async_file_cache=MagicMock(),
        message_queue_factory=lambda _callback: MagicMock(),
        module_dispatcher_factory=lambda **_kwargs: MagicMock(),
    )
    configure_system_config(SystemConfigService(repository=SystemConfigOper()))
    configure_chain_runtime_context_provider(lambda: context)
    try:
        yield
    finally:
        configure_chain_runtime_context_provider(None)


def _report_session_cleanup_error(session, name: str, err: Exception) -> None:
    """记录收尾错误；原测试绿色时将会话标记为失败。"""
    sys.stderr.write(f"\npytest session cleanup failed: {name}: {err!r}\n")
    if session.exitstatus == 0:
        session.exitstatus = 1


def pytest_sessionfinish(session, exitstatus) -> None:
    """释放插件测试过程中创建的消息队列与日志后台线程。"""
    if _selected_generation(session.config) in {"ci", "meta"}:
        return

    try:
        from app.helper.message import stop_message

        stop_message()
    except Exception as err:
        _report_session_cleanup_error(session, "message service", err)

    try:
        from app.log import LoggerManager

        LoggerManager.shutdown()
    except Exception as err:
        _report_session_cleanup_error(session, "logger manager", err)
