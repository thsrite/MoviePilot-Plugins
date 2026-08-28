from __future__ import annotations

import ast
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from apscheduler.triggers.cron import CronTrigger
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins import dockermanager as dockermanager_module
from app.plugins.dockermanager import ContainerResult, DockerManager, DockerTask
from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.types import MessageType

PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "dockermanager" / "__init__.py"


@pytest.fixture
def plugin() -> DockerManager:
    """构造不访问真实配置、数据和 Docker daemon 的插件实例。"""
    instance = DockerManager()
    instance.systemmessage = Mock()
    instance.get_data = Mock(return_value=[])
    instance.save_data = Mock()
    return instance


def _container(
    name: str | None,
    *,
    host_name: str | None = None,
    icon: str | None = None,
) -> SimpleNamespace:
    """构造满足 Docker SDK 动态边界的容器替身。"""
    env = [f"HOST_CONTAINERNAME={host_name}"] if host_name else None
    labels = {"net.unraid.docker.icon": icon} if icon else None
    attrs = {"Config": {"Env": env, "Labels": labels}}
    return SimpleNamespace(
        name=name,
        attrs=attrs,
        restart=Mock(),
        start=Mock(),
        stop=Mock(),
        pause=Mock(),
        unpause=Mock(),
        update=Mock(),
    )


def test_manifest_and_strict_v3_contract() -> None:
    """版本、代际路由、SDK 导入和宿主 Docker 配置必须保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["DockerManager"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["DockerManager"]

    assert manifest["version"] == DockerManager.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy["v3"] is False

    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert {"app.sdk.config", "app.sdk.logging"}.issubset(imports)
    forbidden = (
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
    assert not any(module.startswith(forbidden) for module in imports)
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "BackgroundScheduler" not in source
    assert "tcp://127.0.0.1:38379" not in source
    assert "settings.DOCKER_CLIENT_API" in source


def test_services_isolate_invalid_lines_and_project_once(
    plugin: DockerManager,
) -> None:
    """错误行与命令应隔离，once 和合法 cron 同时交由宿主管理。"""
    plugin.init_plugin({
        "enabled": True,
        "onlyonce": True,
        "time_confs": (
            "app,db,app#0 1 * * *#restart\n"
            "错误行\n"
            "bad#0 2 * * *#remove\n"
            "cron#not cron#start"
        ),
    })

    services = PluginProjection({"DockerManager": plugin}).services()

    assert [service["id"] for service in services] == [
        "DockerManager.Once",
        "DockerManager.1",
    ]
    assert services[0]["trigger"] == "date"
    assert isinstance(services[0]["kwargs"]["run_date"], datetime)
    periodic = services[1]
    assert isinstance(periodic["trigger"], CronTrigger)
    assert str(periodic["trigger"].timezone) == "Asia/Shanghai"
    assert periodic["func_kwargs"]["task"].container_names == ("app", "db")
    assert plugin.systemmessage.put.call_count == 3


def test_client_uses_host_setting_and_is_reused(
    monkeypatch: pytest.MonkeyPatch,
    plugin: DockerManager,
) -> None:
    """Docker client 必须使用宿主地址，并在插件生命周期内复用。"""
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(dockermanager_module.docker, "DockerClient", factory)
    monkeypatch.setattr(
        dockermanager_module,
        "settings",
        SimpleNamespace(TZ="Asia/Shanghai", DOCKER_CLIENT_API="unix:///docker.sock"),
    )

    assert plugin._get_client() is client
    assert plugin._get_client() is client

    factory.assert_called_once_with(base_url="unix:///docker.sock")


def test_container_identity_handles_missing_dynamic_fields() -> None:
    """HOST_CONTAINERNAME 缺失时应回退标准名称，空 attrs 不得抛异常。"""
    unraid = _container("docker-name", host_name="unraid-name")
    standard = _container("docker-name")
    raw = SimpleNamespace(attrs={"Name": "/raw-name"})
    empty = SimpleNamespace(attrs=None)

    assert DockerManager._container_name(unraid) == "unraid-name"
    assert DockerManager._container_name(standard) == "docker-name"
    assert DockerManager._container_name(raw) == "raw-name"
    assert DockerManager._container_name(empty) is None
    assert DockerManager._container_icon(empty) is None


def test_task_executes_matches_reports_missing_and_notifies(
    plugin: DockerManager,
) -> None:
    """已匹配与缺失容器都应形成历史和聚合通知结果。"""
    app = _container(
        "docker-app",
        host_name="app",
        icon="https://example.com/app.png",
    )
    client = Mock()
    client.containers.list.return_value = [app, _container("other")]
    plugin._docker_client = client
    plugin.post_message = Mock()
    plugin.init_plugin({
        "notify": True,
        "msgtype": "Plugin",
        "history_days": 7,
    })
    # init_plugin 会释放旧 client；执行阶段显式注入隔离替身。
    plugin._docker_client = client
    task = DockerTask(1, ("app", "missing"), "0 1 * * *", "restart")

    results = plugin._run_task(task)

    assert [result.success for result in results] == [True, False]
    assert results[1].error == "未找到容器"
    app.restart.assert_called_once_with()
    saved = plugin.save_data.call_args.kwargs["value"]
    assert [(item["name"], item["result"]) for item in saved] == [
        ("app", "success"),
        ("missing", "fail"),
    ]
    plugin.post_message.assert_called_once_with(
        title="docker任务通知",
        mtype=MessageType.Plugin,
        text=(
            "容器：app restart success\n"
            "容器：missing restart fail：未找到容器"
        ),
        image=None,
    )


def test_single_container_notification_uses_optional_http_icon(
    plugin: DockerManager,
) -> None:
    """单容器任务可使用 HTTP 图标，多容器或本地图标不透传。"""
    plugin.init_plugin({"notify": True, "msgtype": "Manual"})
    plugin.post_message = Mock()
    task = DockerTask(1, ("app",), "0 1 * * *", "start")
    result = ContainerResult(
        name="app",
        command="start",
        success=True,
        icon="https://example.com/app.png",
    )

    plugin._notify_results(task, [result])

    assert plugin.post_message.call_args.kwargs["image"] == "https://example.com/app.png"


def test_container_failure_isolated_from_other_targets(plugin: DockerManager) -> None:
    """单个 Docker SDK 操作失败不得阻断同任务中的其它容器。"""
    broken = _container("broken")
    broken.stop.side_effect = RuntimeError("denied")
    healthy = _container("healthy")
    client = Mock()
    client.containers.list.return_value = [broken, healthy]
    plugin._docker_client = client
    task = DockerTask(1, ("broken", "healthy"), "0 1 * * *", "stop")

    results = plugin._run_task(task)

    assert [result.success for result in results] == [False, True]
    assert results[0].error == "denied"
    healthy.stop.assert_called_once_with()


def test_periodic_single_flight_skips_overlap(plugin: DockerManager) -> None:
    """周期任务重叠时不得访问 Docker client。"""
    task = DockerTask(1, ("app",), "0 1 * * *", "restart")
    plugin._run_task = Mock()
    assert plugin._run_lock.acquire(blocking=False) is True
    try:
        result = plugin._run_periodic_task(task)
    finally:
        plugin._run_lock.release()

    assert result[0] is False
    assert "已跳过" in result[1]
    plugin._run_task.assert_not_called()


def test_once_waits_then_consumes_full_config(plugin: DockerManager) -> None:
    """一次性任务应等待执行槽，并在完整配置回写成功后执行。"""
    plugin.init_plugin({
        "enabled": True,
        "onlyonce": True,
        "notify": True,
        "msgtype": "Manual",
        "history_days": 5,
        "time_confs": "app#0 1 * * *#restart",
    })
    plugin.update_config = Mock(return_value=True)
    plugin._run_task = Mock(
        return_value=[ContainerResult("app", "restart", True)]
    )
    plugin._run_lock.acquire()
    result: dict[str, object] = {}
    started = threading.Event()

    def invoke() -> None:
        started.set()
        result["value"] = plugin._run_once_tasks()

    worker = threading.Thread(target=invoke)
    worker.start()
    assert started.wait(timeout=1)
    time.sleep(0.05)
    plugin.update_config.assert_not_called()

    plugin._run_lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["value"] is True
    plugin.update_config.assert_called_once_with({
        "enabled": True,
        "onlyonce": False,
        "notify": True,
        "msgtype": "Manual",
        "time_confs": "app#0 1 * * *#restart",
        "history_days": 5,
        "clear": False,
    })
    assert plugin._run_once is False
    assert plugin._onlyonce is False


@pytest.mark.parametrize("persisted", [False, RuntimeError("storage unavailable")])
def test_once_persistence_failure_keeps_pending_state(
    plugin: DockerManager,
    persisted: object,
) -> None:
    """配置回写失败时不得访问 Docker 或消费一次性任务。"""
    plugin.init_plugin({
        "onlyonce": True,
        "time_confs": "app#0 1 * * *#restart",
    })
    if isinstance(persisted, Exception):
        plugin.update_config = Mock(side_effect=persisted)
    else:
        plugin.update_config = Mock(return_value=persisted)
    plugin._run_task = Mock()

    result = plugin._run_once_tasks()

    assert result[0] is False
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin._run_task.assert_not_called()


def test_stop_service_waits_for_run_and_closes_client(plugin: DockerManager) -> None:
    """停止插件必须等待任务释放执行槽，再关闭并清空 client。"""
    client = Mock()
    plugin._docker_client = client
    plugin._run_lock.acquire()
    completed = threading.Event()

    def stop() -> None:
        plugin.stop_service()
        completed.set()

    worker = threading.Thread(target=stop)
    worker.start()
    time.sleep(0.05)
    assert not completed.is_set()
    client.close.assert_not_called()

    plugin._run_lock.release()
    worker.join(timeout=2)

    assert completed.is_set()
    client.close.assert_called_once_with()
    assert plugin._docker_client is None


def test_clear_history_and_static_surfaces(plugin: DockerManager) -> None:
    """清理开关立即消费，静态接口和页面保持明确结构。"""
    plugin.del_data = Mock()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin({"clear": True, "history_days": "invalid"})

    plugin.del_data.assert_called_once_with("history")
    assert plugin.update_config.call_args.args[0]["clear"] is False
    assert plugin.update_config.call_args.args[0]["history_days"] == 30
    assert plugin.get_command() == []
    assert plugin.get_api() == []
    assert plugin.get_service() == []
    assert plugin.get_page()[0]["text"] == "暂无数据"
    assert plugin.get_form()[1]["history_days"] == 30
    assert plugin.stop_service() is None
