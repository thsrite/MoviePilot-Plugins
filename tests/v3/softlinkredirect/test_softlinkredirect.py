from __future__ import annotations

import ast
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app.plugins.softlinkredirect import SoftLinkRedirect


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "softlinkredirect" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码显式声明的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def test_v3_manifest_and_sdk_contract() -> None:
    """V3 索引、旧代回退开关和公开 SDK 导入应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["SoftLinkRedirect"]
    legacy = json.loads(
        (REPOSITORY_ROOT / "package.json").read_text(encoding="utf-8")
    )["SoftLinkRedirect"]
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert manifest["version"] == SoftLinkRedirect.plugin_version == "2.0.0"
    assert manifest["release"] is True
    assert manifest["system_version"] == ">=3.0.0"
    assert list(manifest["history"]) == ["v2.0.0"]
    assert legacy["v3"] is False
    assert {"app.sdk.config", "app.sdk.logging"}.issubset(_imports())
    assert "app.log" not in _imports()
    assert "subprocess" not in source
    assert "ln -sf" not in source
    assert "os.link" in source


def test_update_symlink_uses_component_prefix_and_does_not_follow_files(
    tmp_path: Path,
) -> None:
    """扫描只改真实软链接，并且不能把 ``origin-old`` 误判为 ``origin``。"""
    root = tmp_path / "links"
    root.mkdir()
    origin = tmp_path / "origin"
    redirect = tmp_path / "redirect"
    origin.mkdir()
    redirect.mkdir()
    (origin / "movie.mkv").write_text("movie", encoding="utf-8")
    (origin / "nested").mkdir()

    matching = root / "matching"
    matching.symlink_to(origin / "movie.mkv")
    prefix_collision = root / "prefix-collision"
    prefix_collision.symlink_to(tmp_path / "origin-old" / "movie.mkv")
    ordinary = root / "ordinary"
    ordinary.write_text(str(origin / "movie.mkv"), encoding="utf-8")

    updated = SoftLinkRedirect.update_symlink(origin, redirect, root)

    assert updated == 1
    assert matching.is_symlink()
    assert os.readlink(matching) == str(redirect / "movie.mkv")
    assert os.readlink(prefix_collision) == str(tmp_path / "origin-old" / "movie.mkv")
    assert ordinary.read_text(encoding="utf-8") == str(origin / "movie.mkv")


def test_update_symlink_supports_relative_and_broken_targets(tmp_path: Path) -> None:
    """相对软链接和断链仍按链接文本重写，不要求先解析目标文件。"""
    root = tmp_path / "links"
    root.mkdir()
    relative = root / "relative"
    relative.symlink_to("../origin/movie.mkv")
    broken = root / "broken"
    broken.symlink_to("../origin/missing.mkv")

    updated = SoftLinkRedirect.update_symlink(
        "../origin", "../redirect", root
    )

    assert updated == 2
    assert os.readlink(relative) == "../redirect/movie.mkv"
    assert os.readlink(broken) == "../redirect/missing.mkv"


def test_absolute_source_can_rewrite_to_relative_target(tmp_path: Path) -> None:
    """相对重定向配置保持为链接文本，不能锚定到宿主进程工作目录。"""
    root = tmp_path / "links"
    root.mkdir()
    origin = tmp_path / "origin"
    origin.mkdir()
    link = root / "movie"
    link.symlink_to(origin / "movie.mkv")

    assert SoftLinkRedirect.update_symlink(origin, "../redirect", root) == 1
    assert os.readlink(link) == "../redirect/movie.mkv"


def test_update_symlink_checks_symlink_directory_without_recursing(
    tmp_path: Path,
) -> None:
    """软链接目录项可以被重写，但其外部目录内容不能被扫描。"""
    root = tmp_path / "links"
    root.mkdir()
    origin = tmp_path / "origin"
    redirect = tmp_path / "redirect"
    origin.mkdir()
    redirect.mkdir()
    external = origin / "external"
    external.mkdir()
    external_link = external / "nested"
    external_link.symlink_to(origin / "movie.mkv")
    link_dir = root / "directory-link"
    link_dir.symlink_to(external, target_is_directory=True)

    updated = SoftLinkRedirect.update_symlink(origin, redirect, root)

    assert updated == 1
    assert os.readlink(link_dir) == str(redirect / "external")
    assert os.readlink(external_link) == str(origin / "movie.mkv")


def test_atomic_replacement_uses_same_directory_and_cleans_transaction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功替换后只保留新软链接，不遗留事务文件。"""
    root = tmp_path / "links"
    root.mkdir()
    origin = tmp_path / "origin"
    redirect = tmp_path / "redirect"
    origin.mkdir()
    redirect.mkdir()
    link = root / "movie"
    link.symlink_to(origin / "movie.mkv")

    assert SoftLinkRedirect.update_symlink(origin, redirect, root) == 1
    assert link.is_symlink()
    assert os.readlink(link) == str(redirect / "movie.mkv")
    assert list(root.iterdir()) == [link]

    second_link = root / "second"
    second_link.symlink_to(origin / "second.mkv")
    original_stat = os.lstat(second_link)
    original_target = os.readlink(second_link)
    assert SoftLinkRedirect._replace_symlink(
        second_link,
        original_target,
        str(redirect / "second.mkv"),
        original_stat,
    ) is True


def test_atomic_replacement_preserves_object_appearing_after_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最终校验后出现的普通文件不能被事务替换覆盖。"""
    root = tmp_path / "links"
    root.mkdir()
    origin = tmp_path / "origin"
    redirect = tmp_path / "redirect"
    origin.mkdir()
    redirect.mkdir()
    link = root / "movie"
    link.symlink_to(origin / "movie.mkv")

    real_rename = os.rename
    injected = False

    def inject_file_before_displacement(
        source: str | os.PathLike[str], target: str | os.PathLike[str]
    ) -> None:
        nonlocal injected
        if not injected and os.fspath(source) == os.fspath(link):
            injected = True
            link.unlink()
            link.write_text("new object", encoding="utf-8")
        real_rename(source, target)

    monkeypatch.setattr(
        "app.plugins.softlinkredirect.os.rename", inject_file_before_displacement
    )

    assert SoftLinkRedirect.update_symlink(origin, redirect, root) == 0
    assert link.is_file()
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "new object"
    assert list(root.iterdir()) == [link]


def test_services_use_host_date_and_cron_and_consume_once(monkeypatch) -> None:
    """一次性服务把时间放在触发参数中，执行入口只消费一次标记。"""
    plugin = SoftLinkRedirect()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "cron": "*/5 * * * *",
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "/tmp/redirect",
        }
    )

    services = plugin.get_service()
    once_service = next(item for item in services if item["trigger"] == "date")
    cron_service = next(item for item in services if item["trigger"] != "date")
    assert "run_date" in once_service["kwargs"]
    assert once_service["kwargs"]["run_date"] > datetime.now(
        once_service["kwargs"]["run_date"].tzinfo
    )
    assert cron_service["kwargs"] == {}
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin.update_config.assert_not_called()

    monkeypatch.setattr(plugin, "redirect", Mock(return_value=1))
    assert once_service["func"]() == 1
    assert plugin._onlyonce is False
    plugin.update_config.assert_called_once_with(
        {
            "enabled": True,
            "onlyonce": False,
            "cron": "*/5 * * * *",
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "/tmp/redirect",
        }
    )
    assert once_service["func"]() == 0
    plugin.redirect.assert_called_once_with()


def test_once_keeps_pending_flag_when_configuration_is_incomplete(monkeypatch) -> None:
    """配置不完整时不能消费一次性标记或执行重定向。"""
    plugin = SoftLinkRedirect()
    plugin.update_config = Mock(return_value=True)
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "",
        }
    )
    monkeypatch.setattr(plugin, "redirect", Mock(return_value=1))

    assert plugin._run_once_redirect() == 0
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin.update_config.assert_not_called()
    plugin.redirect.assert_not_called()


def test_once_keeps_pending_flag_when_state_persistence_returns_false() -> None:
    """一次性状态写回返回 False 时应保留待执行状态并允许重试。"""
    plugin = SoftLinkRedirect()
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "/tmp/redirect",
        }
    )
    plugin.update_config = Mock(return_value=False)
    plugin.redirect = Mock(return_value=1)

    assert plugin._run_once_redirect() == 0
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin.redirect.assert_not_called()


def test_once_keeps_pending_flag_when_state_persistence_raises() -> None:
    """一次性状态写回异常时应保留待执行状态并允许重试。"""
    plugin = SoftLinkRedirect()
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "/tmp/redirect",
        }
    )
    plugin.update_config = Mock(side_effect=RuntimeError("write failed"))
    plugin.redirect = Mock(return_value=1)

    assert plugin._run_once_redirect() == 0
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    plugin.redirect.assert_not_called()


def test_once_callback_can_retry_after_persistence_failure() -> None:
    """失败回调不应阻止后续回调成功消费一次性标记。"""
    plugin = SoftLinkRedirect()
    plugin.init_plugin(
        {
            "enabled": True,
            "onlyonce": True,
            "soft_path": "/tmp/links",
            "origin_path": "/tmp/origin",
            "redirect_path": "/tmp/redirect",
        }
    )
    save_results = iter((False, True))
    plugin.update_config = Mock(side_effect=lambda _config: next(save_results))
    plugin.redirect = Mock(return_value=1)

    assert plugin._run_once_redirect() == 0
    assert plugin._run_once is True
    assert plugin._onlyonce is True
    assert plugin._run_once_redirect() == 1
    assert plugin._run_once is False
    assert plugin._onlyonce is False
    assert plugin._run_once_redirect() == 0
    plugin.redirect.assert_called_once_with()


def test_redirect_is_single_flight(tmp_path: Path, monkeypatch) -> None:
    """周期或手动重复触发时，同一实例只允许一个扫描任务进入执行体。"""
    plugin = SoftLinkRedirect()
    plugin._soft_path = str(tmp_path)
    plugin._origin_path = str(tmp_path / "origin")
    plugin._redirect_path = str(tmp_path / "redirect")
    entered = threading.Event()
    release = threading.Event()

    def blocked_update(*_args) -> int:
        entered.set()
        release.wait(timeout=2)
        return 1

    monkeypatch.setattr(plugin, "update_symlink", blocked_update)
    first_result: list[int] = []
    first = threading.Thread(target=lambda: first_result.append(plugin.redirect()))
    first.start()
    assert entered.wait(timeout=2)
    assert plugin.redirect() == 0
    release.set()
    first.join(timeout=2)

    assert first_result == [1]
