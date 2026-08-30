from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
from app import schemas
from app.plugins import cloudlinkmonitor as cloudlinkmonitor_module
from app.plugins.cloudlinkmonitor import CloudLinkMonitor
from app.schemas.types import (
    MediaSource,
    MediaType,
    MessageType,
    NotificationChannel,
)


PLUGIN_PATH = REPOSITORY_ROOT / "plugins.v3" / "cloudlinkmonitor" / "__init__.py"


def _imports() -> set[str]:
    """返回插件源码中的 from-import 模块集合。"""
    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    return {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }


def _form_models(value: object) -> set[str]:
    """递归收集插件表单中绑定的配置字段。"""
    if isinstance(value, dict):
        models = {
            model
            for model in [value.get("props", {}).get("model")]
            if isinstance(model, str)
        }
        for child in value.values():
            models.update(_form_models(child))
        return models
    if isinstance(value, list):
        models = set()
        for child in value:
            models.update(_form_models(child))
        return models
    return set()


def test_v3_plugin_imports_and_initializes(monkeypatch) -> None:
    """V3 插件应能导入，并在隔离外部链资源后完成生命周期初始化。"""
    monkeypatch.setattr(cloudlinkmonitor_module, "TransferChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "MediaChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "TmdbChain", Mock)
    monkeypatch.setattr(cloudlinkmonitor_module, "StorageChain", Mock)

    plugin = CloudLinkMonitor()
    plugin.init_plugin({})

    assert plugin.plugin_version == "3.0.1"
    assert plugin.get_api()[0]["response_model"] is schemas.Response[None]

    plugin.stop_service()


def test_previous_title_query_uses_complete_media_identity() -> None:
    """历史标题查询必须传递来源与来源原生 ID，非法身份不得降级为裸 ID。"""
    plugin = CloudLinkMonitor()
    plugin.transferhis = Mock()
    plugin.transferhis.get_by_media_identity.return_value = SimpleNamespace(
        title="统一身份标题"
    )
    mediainfo = SimpleNamespace(
        media_source=MediaSource.Douban,
        media_id="1295644",
        type=MediaType.MOVIE,
    )

    assert plugin._get_previous_media_title(mediainfo) == "统一身份标题"
    plugin.transferhis.get_by_media_identity.assert_called_once_with(
        media_source=MediaSource.Douban,
        media_id="1295644",
        mtype=MediaType.MOVIE.value,
    )

    plugin.transferhis.reset_mock()
    mediainfo.media_id = "0"
    assert plugin._get_previous_media_title(mediainfo) is None
    plugin.transferhis.get_by_media_identity.assert_not_called()


def test_tmdb_episodes_converts_non_tmdb_identity() -> None:
    """来源链只接收 TMDB ID，非 TMDB 媒体必须先走统一身份转换。"""
    plugin = CloudLinkMonitor()
    plugin.mediachain = Mock()
    plugin.tmdbchain = Mock()
    plugin.mediachain.convert_media_identity.return_value = {"id": 1399}
    plugin.tmdbchain.tmdb_episodes.return_value = [SimpleNamespace(episode_number=1)]
    mediainfo = SimpleNamespace(
        media_source=MediaSource.Douban,
        media_id="34912145",
        type=MediaType.TV,
    )

    result = plugin._get_tmdb_episodes(mediainfo=mediainfo, season=2)

    assert len(result) == 1
    plugin.mediachain.convert_media_identity.assert_called_once_with(
        target_source=MediaSource.TMDB,
        media_source=MediaSource.Douban,
        media_id="34912145",
        mtype=MediaType.TV,
        season=2,
    )
    plugin.tmdbchain.tmdb_episodes.assert_called_once_with(tmdbid=1399, season=2)


def test_redo_hint_uses_complete_media_identity() -> None:
    """手动整理提示必须与 V3 `/redo` 的三段身份参数一致。"""
    assert CloudLinkMonitor._redo_hint(7) == (
        "/redo 7 [media_source]|[media_id]|[类型]"
    )
    assert MessageType.Manual.value


def test_unrecognized_media_uses_single_failure_history_entry(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """未识别媒体只经插件的失败历史入口写入，并使用返回的重整 ID。"""
    media_file = tmp_path / "Example.Movie.2026.mkv"
    media_file.write_bytes(b"video")
    file_item = SimpleNamespace(path=media_file)

    plugin = CloudLinkMonitor()
    record_failure = Mock(return_value=SimpleNamespace(id=7))
    monkeypatch.setattr(plugin, "_record_unrecognized_failure", record_failure)
    plugin.transferhis = Mock()
    plugin.transferhis.get_by_src.return_value = None
    plugin.systemconfig = Mock()
    plugin.systemconfig.get.return_value = []
    plugin.storagechain = Mock()
    plugin.storagechain.get_file_item.return_value = file_item
    plugin.chain = Mock()
    plugin.chain.recognize_media.return_value = None
    plugin._exclude_keywords = ""
    plugin._size = 0
    plugin._notify = False
    plugin._dirconf = {str(tmp_path): tmp_path / "library"}
    plugin._transferconf = {str(tmp_path): "copy"}

    plugin._CloudLinkMonitor__handle_file(
        event_path=str(media_file),
        mon_path=str(tmp_path),
    )

    call_kwargs = record_failure.call_args.kwargs
    assert call_kwargs["fileitem"] is file_item
    assert call_kwargs["mode"] == "copy"
    assert call_kwargs["meta"].name == "Example Movie"


def test_unrecognized_failure_persists_history_accepted_by_redo(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """失败入口应通过真实事务仓储返回可由宿主 `/redo` 接受的历史 ID。"""
    from app.application import history as history_module
    from app.chain.transfer import TransferChain
    from app.db.adapters.history.transfer import TransactionalTransferHistoryRepository
    from app.db.session import SessionFactory, async_session_scope

    repository = TransactionalTransferHistoryRepository(
        sync_session=SessionFactory,
        async_session=async_session_scope,
    )
    monkeypatch.setattr(
        history_module,
        "get_transfer_history_repository",
        lambda: repository,
    )
    media_file = tmp_path / "Example.Movie.2026.mkv"
    media_file.write_bytes(b"video")
    file_item = schemas.FileItem(
        path=str(media_file),
        storage="local",
        type="file",
        name=media_file.name,
    )

    history = CloudLinkMonitor._record_unrecognized_failure(
        fileitem=file_item,
        mode="copy",
        meta=cloudlinkmonitor_module.MetaInfoPath(media_file),
    )

    stored = repository.get(history.id)
    assert stored is not None
    assert stored.id == history.id
    assert stored.src == str(media_file)
    assert stored.status is False
    assert stored.errmsg == "未识别到媒体信息"

    transfer_chain = TransferChain()
    redo = Mock(return_value=(True, ""))
    monkeypatch.setattr(transfer_chain, "redo_transfer_history", redo)
    monkeypatch.setattr(transfer_chain, "post_message", Mock())
    transfer_chain.remote_transfer(
        str(history.id),
        channel=NotificationChannel.Telegram,
        userid="10001",
        source="cloudlinkmonitor-test",
    )
    redo.assert_called_once_with(history.id)


def test_v3_form_removes_obsolete_history_setting() -> None:
    """V3 整理历史由宿主 Chain 维护，表单和持久化配置不再暴露旧开关。"""
    plugin = CloudLinkMonitor()
    form, defaults = plugin.get_form()
    plugin.update_config = Mock()

    plugin._CloudLinkMonitor__update_config()

    assert "history" not in _form_models(form)
    assert "history" not in defaults
    assert "history" not in plugin.update_config.call_args.args[0]


def test_v3_manifest_and_import_contracts() -> None:
    """V3 索引、旧代回退开关与单点内部历史写入依赖应保持一致。"""
    manifest = json.loads(
        (REPOSITORY_ROOT / "package.v3.json").read_text(encoding="utf-8")
    )["CloudLinkMonitor"]
    legacy_manifest = json.loads(
        (REPOSITORY_ROOT / "package.v2.json").read_text(encoding="utf-8")
    )["CloudLinkMonitor"]

    assert manifest["version"] == CloudLinkMonitor.plugin_version
    assert manifest["system_version"] == ">=3.0.0"
    assert manifest["release"] is True
    assert legacy_manifest["v3"] is False

    imports = _imports()
    assert {
        "app.sdk.config",
        "app.sdk.events",
        "app.sdk.logging",
        "app.sdk.media",
        "app.sdk.utilities",
    }.issubset(imports)
    assert "app.application.history" in imports
    assert "app.application.directory" not in imports
    assert "app.modules.filemanager" not in imports
    forbidden_prefixes = (
        "app.adapters",
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
    assert "app.log" not in imports
    assert "app.db.transferhistory_oper" not in imports

    tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
    failure_writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "add_transfer_fail"
    ]
    assert len(failure_writes) == 1
