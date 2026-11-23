"""GDriveTree 实例级测试：用最小宿主桩 + SQLite 内存库验证刷新与查询。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "gdrivetree"
PLUGIN_PATH = PLUGIN_DIR / "__init__.py"
PACKAGE_NAME = "gdrivetree_plugin_under_test"
FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
STUB_NAMES = (
    "app",
    "app.schemas",
    "app.schemas.types",
    "app.sdk",
    "app.sdk.database",
    "app.sdk.events",
    "app.sdk.logging",
    "app.sdk.plugin",
)


class _StubEventType:
    """宿主 EventType 的最小替身。"""

    TransferComplete = "transfer.complete"
    PluginTriggered = "plugin.triggered"


class _StubEventManager:
    """宿主 eventmanager 的最小替身，记录已发送事件。"""

    def __init__(self) -> None:
        self.events: List[Any] = []

    def register(self, *args: Any, **kwargs: Any):
        """返回保持原函数不变的装饰器。"""

        def decorator(func):
            return func

        return decorator

    def send_event(self, etype: Any, data: Any = None, priority: Any = None) -> None:
        """记录广播事件。"""
        self.events.append((etype, data))


class _StubLogger:
    """记录日志的宿主 logger 替身。"""

    def __init__(self) -> None:
        self.messages: List[str] = []

    def debug(self, *args: Any, **kwargs: Any) -> None:
        self.messages.append(" ".join(str(arg) for arg in args))

    info = debug
    warning = debug
    error = debug
    exception = debug


class _StubDatabase:
    """插件自有数据库句柄的最小替身。"""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def session(self):
        """新建会话。"""
        return self.session_factory()


class _StubPluginBase:
    """宿主 _PluginBase 的最小替身。"""

    def __init__(self) -> None:
        self._configs: Dict[str, Any] = {}
        self._database = _StubDatabase()
        self._data: Dict[str, Any] = {}

    def get_config(self, plugin_id: Optional[str] = None) -> Any:
        """返回预设的插件配置。"""
        return self._configs.get(plugin_id or self.__class__.__name__)

    def update_config(self, config: Dict[str, Any], plugin_id: Optional[str] = None) -> bool:
        """模拟配置保存。"""
        self._configs[plugin_id or self.__class__.__name__] = dict(config)
        return True

    def get_database(self, plugin_id: Optional[str] = None) -> _StubDatabase:
        """返回实例库。"""
        return self._database

    def save_data(self, key: str, value: Any, plugin_id: Optional[str] = None) -> None:
        """模拟插件数据保存。"""
        self._data[key] = value

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """模拟插件数据读取。"""
        return self._data.get(key) if key else dict(self._data)

    def del_data(self, key: str, plugin_id: Optional[str] = None) -> Any:
        """模拟插件数据删除。"""
        return self._data.pop(key, None)


def _declarative_base() -> type[DeclarativeBase]:
    """构造独立的声明式基类。"""

    class Base(DeclarativeBase):
        pass

    return Base


def _install_stubs() -> None:
    """在 sys.modules 中安装 app.* 桩。"""
    app = types.ModuleType("app")
    schemas = types.ModuleType("app.schemas")
    schema_types = types.ModuleType("app.schemas.types")
    sdk = types.ModuleType("app.sdk")
    database = types.ModuleType("app.sdk.database")
    events = types.ModuleType("app.sdk.events")
    logging_module = types.ModuleType("app.sdk.logging")
    plugin_module = types.ModuleType("app.sdk.plugin")
    schema_types.EventType = _StubEventType
    database.plugin_declarative_base = _declarative_base
    events.Event = object
    events.eventmanager = _StubEventManager()
    logging_module.logger = _StubLogger()
    plugin_module._PluginBase = _StubPluginBase
    schemas.types = schema_types
    sdk.database = database
    sdk.events = events
    sdk.logging = logging_module
    sdk.plugin = plugin_module
    app.schemas = schemas
    app.sdk = sdk
    sys.modules.update({
        "app": app,
        "app.schemas": schemas,
        "app.schemas.types": schema_types,
        "app.sdk": sdk,
        "app.sdk.database": database,
        "app.sdk.events": events,
        "app.sdk.logging": logging_module,
        "app.sdk.plugin": plugin_module,
    })


@pytest.fixture(scope="module")
def plugin_module():
    """加载插件包并恢复宿主模块。"""
    saved = {name: sys.modules.get(name) for name in STUB_NAMES + (PACKAGE_NAME,)}
    _install_stubs()
    try:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            PLUGIN_PATH,
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = module
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def _new_plugin(module: Any, config: Optional[Dict[str, Any]] = None) -> Any:
    """创建实例、建表并写入源插件配置。"""
    instance = module.GDriveTree()
    instance.init_plugin(config or {"enabled": True})
    module.TreeMeta.metadata.create_all(instance._database.engine)
    instance._configs["GDriveStorage"] = {
        "client_id": "cid",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "root_folder_id": "root",
        "root_path": "/",
        "use_shared_drive": False,
        "shared_drive_id": "",
    }
    return instance


def _sample_files() -> List[Dict[str, Any]]:
    """构造一棵简单目录树。"""
    return [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {"id": "d1", "name": "Movies", "mimeType": FOLDER, "parents": ["root"]},
        {
            "id": "f1",
            "name": "a.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["d1"],
            "size": "10",
        },
    ]


def _save(module: Any, instance: Any) -> Any:
    """把样例树写入快照。"""
    nodes, stats = module.build_tree(
        _sample_files(), root_id="root", root_name="Root", root_path="/"
    )
    root = module.RootInfo(file_id="root", name="Root", drive_id="", corpora="user")
    instance._save_snapshot(nodes, stats, root, {"root_path": "/"}, 1, 0.2)
    return stats


def _save_files(
    module: Any,
    instance: Any,
    files: List[Dict[str, Any]],
    *,
    root_path: str = "/",
) -> Any:
    """把指定文件元数据写入快照并返回变更集。"""
    nodes, stats = module.build_tree(
        files, root_id="root", root_name="Root", root_path=root_path
    )
    root = module.RootInfo(file_id="root", name="Root", drive_id="", corpora="user")
    return instance._save_snapshot(
        nodes, stats, root, {"root_path": root_path}, 1, 0.1
    )


def test_snapshot_persists_and_meta_updates(plugin_module: Any) -> None:
    """快照写入后节点可查、统计正确。"""
    instance = _new_plugin(plugin_module)
    stats = _save(plugin_module, instance)
    page, pages, total, items = instance._page_nodes(1)
    assert (page, pages, total) == (1, 1, stats.node_count)
    assert {item["name"] for item in items} == {"Root", "Movies", "a.mkv"}
    meta = instance._load_meta()
    assert meta["status"] == "success"
    assert meta["node_count"] == stats.node_count
    assert meta["dir_count"] == 2
    assert meta["file_count"] == 1
    assert meta["total_size"] == 10


def test_search_filters_name_and_path(plugin_module: Any) -> None:
    """搜索同时匹配名称和路径。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    _, _, total, items = instance._page_nodes(1, "Movies")
    assert total == 2  # Movies 目录 + 其下 a.mkv 的路径
    _, _, total2, _ = instance._page_nodes(1, "a.mkv")
    assert total2 == 1


def test_failed_refresh_keeps_previous_nodes(plugin_module: Any) -> None:
    """失败只更新状态，不删除旧节点。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    instance._safe_failed("failed", "boom")
    _, _, total, _ = instance._page_nodes(1)
    assert total == 3
    meta = instance._load_meta()
    assert meta["status"] == "failed"
    assert meta["error"] == "boom"


def test_clear_removes_nodes(plugin_module: Any) -> None:
    """清空操作删除节点并重置统计。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    result = instance.api_clear()
    assert set(result) == {"success", "message", "data"}
    assert result["data"]["deleted"] == 3
    _, _, total, _ = instance._page_nodes(1)
    assert total == 0
    assert instance._load_meta()["status"] == "idle"


def test_api_envelopes_are_three_keys(plugin_module: Any) -> None:
    """页面 API 返回恰好三键；刷新接口用未启用实例避免真实线程。"""
    instance = _new_plugin(plugin_module)
    assert set(instance.api_status()) == {"success", "message", "data"}
    assert set(instance.api_nodes_page({"page": 1})) == {"success", "message", "data"}
    assert set(instance.api_search({"keyword": "x"})) == {"success", "message", "data"}
    assert set(instance.api_archive_status()) == {"success", "message", "data"}
    disabled = _new_plugin(plugin_module, {"enabled": False})
    assert set(disabled.api_refresh()) == {"success", "message", "data"}
    assert set(disabled.api_archive_scan()) == {"success", "message", "data"}


def test_extract_drive_id_variants(plugin_module: Any) -> None:
    """分享链接、共享盘 URL 与纯 ID 都能归一化。"""
    extract = plugin_module._extract_drive_id
    assert extract("abcDEF_123") == "abcDEF_123"
    assert extract("root") == "root"
    assert extract("https://drive.google.com/drive/folders/ABC123?usp=sharing") == "ABC123"
    assert extract("https://drive.google.com/open?id=XYZ789") == "XYZ789"
    assert extract("https://drive.google.com/file/d/FILE001/view") == "FILE001"
    assert extract("") == ""


def test_get_service_respects_switches(plugin_module: Any) -> None:
    """全量刷新与归档轮询分别按开关注册。"""
    disabled = _new_plugin(plugin_module, {"enabled": False, "auto_refresh": True})
    assert disabled.get_service() == []
    no_service = _new_plugin(
        plugin_module,
        {"enabled": True, "auto_refresh": False, "archive_watch": False},
    )
    assert no_service.get_service() == []
    enabled = _new_plugin(
        plugin_module,
        {
            "enabled": True,
            "auto_refresh": True,
            "refresh_interval_hours": 6,
            "archive_watch": False,
        },
    )
    services = enabled.get_service()
    assert services and services[0]["kwargs"]["seconds"] == 6 * 3600
    archive_only = _new_plugin(
        plugin_module,
        {
            "enabled": True,
            "auto_refresh": False,
            "archive_watch": True,
            "archive_poll_interval_minutes": 3,
        },
    )
    services = archive_only.get_service()
    assert len(services) == 1
    assert services[0]["id"] == "GDriveTreeArchive"
    assert services[0]["kwargs"]["seconds"] == 180


def test_start_refresh_requires_enabled(plugin_module: Any) -> None:
    """未启用时不允许启动刷新。"""
    instance = _new_plugin(plugin_module, {"enabled": False})
    assert instance.start_refresh() is False


class _FakeClient:
    """替换真实客户端的替身。"""

    fail = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def resolve_root(self, root_id: str, fallback_name: str = "") -> Any:
        return _FakeClient.root_info

    def list_all(self, **kwargs: Any) -> Any:
        if _FakeClient.fail:
            raise _FakeClient.error
        return _sample_files(), 1

    def close(self) -> None:
        pass


_FakeClient.root_info = None
_FakeClient.error = None


def test_full_refresh_orchestration(plugin_module: Any, monkeypatch: Any) -> None:
    """后台刷新走完整编排并成功入库。"""
    instance = _new_plugin(plugin_module)
    _FakeClient.fail = False
    _FakeClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="user"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeClient)
    assert instance.start_refresh() is True
    instance._thread.join(timeout=5)
    meta = instance._load_meta()
    assert meta["status"] == "success"
    assert meta["node_count"] == 3
    assert meta["page_count"] == 1


def test_full_refresh_failure_keeps_nodes(plugin_module: Any, monkeypatch: Any) -> None:
    """客户端失败时状态为 failed，旧节点保留。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    _FakeClient.fail = True
    _FakeClient.error = plugin_module.GDriveTreeError("network down")
    _FakeClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="user"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeClient)
    assert instance.start_refresh() is True
    instance._thread.join(timeout=5)
    meta = instance._load_meta()
    assert meta["status"] == "failed"
    assert "network down" in meta["error"]
    _, _, total, _ = instance._page_nodes(1)
    assert total == 3


def test_cancelled_refresh_keeps_nodes(plugin_module: Any, monkeypatch: Any) -> None:
    """取消刷新时状态为 cancelled，旧节点保留。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    _FakeClient.fail = True
    _FakeClient.error = plugin_module.GDriveTreeCancelled("stopped")
    _FakeClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="user"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeClient)
    assert instance.start_refresh() is True
    instance._thread.join(timeout=5)
    meta = instance._load_meta()
    assert meta["status"] == "cancelled"
    _, _, total, _ = instance._page_nodes(1)
    assert total == 3


def test_missing_source_config_fails(plugin_module: Any) -> None:
    """源插件配置缺失时状态为 failed。"""
    instance = _new_plugin(plugin_module)
    instance._configs.pop("GDriveStorage", None)
    assert instance.start_refresh() is True
    instance._thread.join(timeout=5)
    meta = instance._load_meta()
    assert meta["status"] == "failed"
    assert "未找到插件" in meta["error"]


def test_source_config_normalizes_share_links(plugin_module: Any) -> None:
    """共享链接在读取源配置时被归一化为纯 ID。"""
    instance = _new_plugin(plugin_module)
    instance._configs["GDriveStorage"].update({
        "root_folder_id": "https://drive.google.com/drive/folders/FOLDER123?usp=sharing",
        "shared_drive_id": "https://drive.google.com/drive/folders/DRIVE456",
    })
    source = instance._read_source_config()
    assert source["root_folder_id"] == "FOLDER123"
    assert source["shared_drive_id"] == "DRIVE456"


def test_page_number_is_corrected(plugin_module: Any) -> None:
    """页码超过总页数时回退到最后一页。"""
    instance = _new_plugin(plugin_module)
    files = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
    ] + [
        {
            "id": f"f{index}",
            "name": f"file-{index:02d}.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["root"],
            "size": "1",
        }
        for index in range(25)
    ]
    nodes, stats = plugin_module.build_tree(
        files, root_id="root", root_name="Root", root_path="/"
    )
    root = plugin_module.RootInfo(file_id="root", name="Root", drive_id="", corpora="user")
    instance._save_snapshot(nodes, stats, root, {"root_path": "/"}, 1, 0.1)
    page, pages, total, items = instance._page_nodes(99)
    assert pages == 2
    assert page == 2
    assert total == 26
    assert len(items) == 6


def test_like_wildcards_are_escaped(plugin_module: Any) -> None:
    """搜索关键字中的 % 和 _ 不作为通配符。"""
    instance = _new_plugin(plugin_module)
    files = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {
            "id": "f1",
            "name": "100%_done.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["root"],
            "size": "1",
        },
    ]
    nodes, stats = plugin_module.build_tree(
        files, root_id="root", root_name="Root", root_path="/"
    )
    root = plugin_module.RootInfo(file_id="root", name="Root", drive_id="", corpora="user")
    instance._save_snapshot(nodes, stats, root, {"root_path": "/"}, 1, 0.1)
    _, _, exact, _ = instance._page_nodes(1, "100%_done")
    assert exact == 1
    _, _, wildcard, _ = instance._page_nodes(1, "100%x")
    assert wildcard == 0


def test_clear_rejected_while_refreshing(plugin_module: Any) -> None:
    """刷新进行中拒绝清空。"""
    instance = _new_plugin(plugin_module)
    assert instance._refresh_lock.acquire(blocking=False)
    try:
        result = instance.api_clear()
    finally:
        instance._refresh_lock.release()
    assert result["success"] is False
    assert "刷新进行中" in result["message"]


def test_incremental_write_deletes_inserts_and_updates(plugin_module: Any) -> None:
    """增量写库只删除缺失、插入新增、更新变化行。"""
    instance = _new_plugin(plugin_module)
    _save(plugin_module, instance)
    with instance._session() as session:
        before = {
            row.file_id: row.id
            for row in session.scalars(select(plugin_module.TreeNode))
        }
    files = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {"id": "d1", "name": "Films", "mimeType": FOLDER, "parents": ["root"]},
        {
            "id": "f2",
            "name": "b.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["d1"],
            "size": "5",
        },
    ]
    nodes, stats = plugin_module.build_tree(
        files, root_id="root", root_name="Root", root_path="/"
    )
    root = plugin_module.RootInfo(file_id="root", name="Root", drive_id="", corpora="user")
    instance._incremental_write = True
    instance._save_snapshot(nodes, stats, root, {"root_path": "/"}, 1, 0.1)
    with instance._session() as session:
        rows = {row.file_id: row for row in session.scalars(select(plugin_module.TreeNode))}
    assert set(rows) == {"root", "d1", "f2"}
    assert rows["root"].id == before["root"]
    assert rows["d1"].id == before["d1"]
    assert rows["d1"].name == "Films"
    assert rows["d1"].path == "/Films/"
    assert rows["f2"].size == 5


class _FakeArchiveClient:
    """归档测试用客户端替身。"""

    files: Dict[str, Dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def resolve_root(self, root_id: str, fallback_name: str = "") -> Any:
        return _FakeArchiveClient.root_info

    def get_file(self, file_id: str, fields: Any = None) -> Dict[str, Any]:
        return dict(_FakeArchiveClient.files[file_id])

    def find_child(self, parent_id: str, name: str) -> Optional[Dict[str, Any]]:
        for meta in _FakeArchiveClient.files.values():
            if meta.get("name") == name and parent_id in (meta.get("parents") or []):
                return dict(meta)
        return None

    def close(self) -> None:
        pass


_FakeArchiveClient.root_info = None


def _archive_files() -> Dict[str, Dict[str, Any]]:
    """构造 root -> Movies -> a.mkv 的云端元数据。"""
    return {
        "root": {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        "d1": {"id": "d1", "name": "Movies", "mimeType": FOLDER, "parents": ["root"]},
        "f1": {
            "id": "f1",
            "name": "a.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["d1"],
            "size": "10",
        },
    }


def test_archive_event_inserts_parent_chain(plugin_module: Any, monkeypatch: Any) -> None:
    """归档事件按 fileid 拉取父链并增量入树。"""
    instance = _new_plugin(plugin_module)
    _FakeArchiveClient.files = _archive_files()
    _FakeArchiveClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="allDrives"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeArchiveClient)
    instance._handle_event_target("f1", "/Movies/a.mkv", 42)
    with instance._session() as session:
        rows = {row.file_id: row for row in session.scalars(select(plugin_module.TreeNode))}
        link = session.scalar(
            select(plugin_module.ArchiveLink).where(
                plugin_module.ArchiveLink.history_id == 42
            )
        )
    assert {"root", "d1", "f1"} <= set(rows)
    assert rows["f1"].path == "/Movies/a.mkv"
    assert rows["f1"].parent_file_id == "d1"
    assert link is not None and link.status == "done"


def test_archive_event_falls_back_to_tree_path_for_shortcut(
    plugin_module: Any, monkeypatch: Any
) -> None:
    """真实父链不在树根下时，按树中快捷方式路径定位归档文件。"""
    instance = _new_plugin(plugin_module)
    seed = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {
            "id": "sc",
            "name": "Shared",
            "mimeType": SHORTCUT,
            "parents": ["root"],
            "shortcutDetails": {"targetId": "td", "targetMimeType": FOLDER},
        },
        {"id": "td", "name": "Target", "mimeType": FOLDER, "parents": []},
        {
            "id": "d2",
            "name": "Shows",
            "mimeType": FOLDER,
            "parents": ["td"],
        },
    ]
    _save_files(plugin_module, instance, seed)
    _FakeArchiveClient.files = {
        "root": {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        "sc": {
            "id": "sc",
            "name": "Shared",
            "mimeType": SHORTCUT,
            "parents": ["root"],
            "shortcutDetails": {"targetId": "td", "targetMimeType": FOLDER},
        },
        "td": {"id": "td", "name": "Target", "mimeType": FOLDER, "parents": []},
        "d2": {"id": "d2", "name": "Shows", "mimeType": FOLDER, "parents": ["td"]},
        "f9": {
            "id": "f9",
            "name": "a.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["d2"],
            "size": "10",
        },
    }
    _FakeArchiveClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="allDrives"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeArchiveClient)
    instance._handle_event_target("f9", "/workspace/Shared/Shows/a.mkv", 43)
    with instance._session() as session:
        row = session.scalar(
            select(plugin_module.TreeNode).where(
                plugin_module.TreeNode.file_id == "f9"
            )
        )
        link = session.scalar(
            select(plugin_module.ArchiveLink).where(
                plugin_module.ArchiveLink.history_id == 43
            )
        )
    assert row is not None
    assert row.path == "/Shared/Shows/a.mkv"
    assert row.parent_file_id == "d2"
    assert link is not None and link.status == "done"


def test_archive_event_creates_missing_parent_dirs(
    plugin_module: Any, monkeypatch: Any
) -> None:
    """父目录尚未入树时，按路径逐段补建缺失目录直到目标文件。"""
    instance = _new_plugin(plugin_module)
    seed = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {
            "id": "sc",
            "name": "媒体库",
            "mimeType": SHORTCUT,
            "parents": ["root"],
            "shortcutDetails": {"targetId": "td", "targetMimeType": FOLDER},
        },
    ]
    _save_files(plugin_module, instance, seed, root_path="/workspace/")
    _FakeArchiveClient.files = {
        "root": {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        "sc": {
            "id": "sc",
            "name": "媒体库",
            "mimeType": SHORTCUT,
            "parents": ["root"],
            "shortcutDetails": {"targetId": "td", "targetMimeType": FOLDER},
        },
        "td": {"id": "td", "name": "Target", "mimeType": FOLDER, "parents": []},
        "tv": {"id": "tv", "name": "电视剧", "mimeType": FOLDER, "parents": ["td"]},
        "s1": {"id": "s1", "name": "Season 1", "mimeType": FOLDER, "parents": ["tv"]},
        "f-new": {
            "id": "f-new",
            "name": "a.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["s1"],
            "size": "10",
        },
    }
    _FakeArchiveClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="allDrives"
    )
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeArchiveClient)
    instance._handle_event_target(
        "f-new", "/workspace/媒体库/电视剧/Season 1/a.mkv", 44
    )
    with instance._session() as session:
        rows = {
            row.file_id: row
            for row in session.scalars(select(plugin_module.TreeNode))
        }
        link = session.scalar(
            select(plugin_module.ArchiveLink).where(
                plugin_module.ArchiveLink.history_id == 44
            )
        )
    assert rows["tv"].path == "/workspace/媒体库/电视剧/"
    assert rows["tv"].parent_file_id == "sc"
    assert rows["s1"].path == "/workspace/媒体库/电视剧/Season 1/"
    assert rows["s1"].parent_file_id == "tv"
    assert rows["f-new"].path == "/workspace/媒体库/电视剧/Season 1/a.mkv"
    assert rows["f-new"].parent_file_id == "s1"
    assert link is not None and link.status == "done"


def test_archive_target_paths_prefers_dest_file(plugin_module: Any) -> None:
    """dest 是目标文件路径时，不把源文件名拼到目标文件后面。"""
    instance = _new_plugin(plugin_module)
    dest = (
        "/workspace/媒体库/电视剧/日番/葬送的芙莉莲 (2023)/Season 2/"
        "葬送的芙莉莲.2023.S02E03.第3集.1080p.WEB-DL.H264-FROGWeb.mkv"
    )
    record = plugin_module.ArchiveRecord(
        history_id=1,
        dest=dest,
        dest_storage="gdrive",
        dest_fileitem={"fileid": "f1", "path": dest, "type": "file"},
        files=["/downloads/Frieren.Beyond.Journey's.End.S02E03.mkv"],
    )
    assert instance._archive_target_paths(record) == [dest]


def test_archive_target_paths_maps_files_under_dir(plugin_module: Any) -> None:
    """dest 是目录时，源文件名落到目标目录下。"""
    instance = _new_plugin(plugin_module)
    record = plugin_module.ArchiveRecord(
        history_id=2,
        dest="/workspace/媒体库/电影",
        dest_storage="gdrive",
        dest_fileitem={"path": "/workspace/媒体库/电影", "type": "dir"},
        files=["/downloads/a.mkv"],
    )
    assert instance._archive_target_paths(record) == [
        "/workspace/媒体库/电影/a.mkv"
    ]


class _FakeArchiveSource:
    """替换宿主整理历史源的替身。"""

    records: List[Any] = []

    def __init__(self, storage_names: Any) -> None:
        self.storage_names = storage_names

    def iter_records(self, **kwargs: Any):
        return iter(self.records)

    target_file_id = staticmethod(
        lambda record: str(record.dest_fileitem.get("fileid") or "")
    )


def test_archive_scan_skips_processed_history(plugin_module: Any, monkeypatch: Any) -> None:
    """归档轮询处理一次后不会重复处理同一条历史。"""
    instance = _new_plugin(plugin_module)
    _FakeArchiveClient.files = _archive_files()
    _FakeArchiveClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="allDrives"
    )
    _FakeArchiveSource.records = [
        plugin_module.ArchiveRecord(
            history_id=7,
            dest="/Movies",
            dest_storage="gdrive",
            dest_fileitem={"fileid": "f1"},
            files=[],
        )
    ]
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeArchiveClient)
    monkeypatch.setattr(plugin_module, "ArchiveSource", _FakeArchiveSource)
    first = instance.scan_archive()
    assert first["processed"] == 1
    assert first["failed"] == 0
    second = instance.scan_archive()
    assert second["processed"] == 0
    assert second["skipped"] == 1
    counts = instance._archive_link_counts()
    assert counts == {"total": 1, "done": 1, "failed": 0, "pending": 0}


def test_archive_scan_records_failure(plugin_module: Any, monkeypatch: Any) -> None:
    """归档目标无法解析时记录 failed 并保留重试次数。"""
    instance = _new_plugin(plugin_module)
    _FakeArchiveClient.files = {}
    _FakeArchiveClient.root_info = plugin_module.RootInfo(
        file_id="root", name="Root", drive_id="", corpora="allDrives"
    )
    _FakeArchiveSource.records = [
        plugin_module.ArchiveRecord(
            history_id=8,
            dest="/Missing",
            dest_storage="gdrive",
            dest_fileitem={},
            files=["/Missing/none.mkv"],
        )
    ]
    monkeypatch.setattr(plugin_module, "GDriveTreeClient", _FakeArchiveClient)
    monkeypatch.setattr(plugin_module, "ArchiveSource", _FakeArchiveSource)
    result = instance.scan_archive()
    assert result["failed"] == 1
    with instance._session() as session:
        link = session.scalar(
            select(plugin_module.ArchiveLink).where(
                plugin_module.ArchiveLink.history_id == 8
            )
        )
    assert link.status == "failed"
    assert link.retry_count == 1


def test_refresh_publishes_targeted_tree_event(plugin_module: Any) -> None:
    """开启通知后，刷新写库成功会向目标插件发送 tree.changed。"""
    instance = _new_plugin(
        plugin_module,
        {"enabled": True, "strm_notify": True, "strm_plugin_id": "GDriveStrm"},
    )
    plugin_module.eventmanager.events.clear()
    plugin_module.logger.messages.clear()
    _save(plugin_module, instance)
    events = plugin_module.eventmanager.events
    assert len(events) == 1
    logs = "\n".join(plugin_module.logger.messages)
    assert "发送目录树事件" in logs
    assert "GDriveStrm" in logs
    etype, payload = events[0]
    assert etype == plugin_module.EventType.PluginTriggered
    assert payload["plugin_id"] == "GDriveTree"
    assert payload["event_name"] == "tree.changed"
    assert payload["__mp_target_plugin_id"] == "GDriveStrm"
    data = payload["data"]
    assert data["kind"] == "refresh"
    assert data["full"] is False
    assert {item["file_id"] for item in data["added"]} == {"root", "d1", "f1"}


def test_refresh_skips_event_when_notify_disabled(plugin_module: Any) -> None:
    """未开启通知时不发送任何事件。"""
    instance = _new_plugin(plugin_module, {"enabled": True})
    plugin_module.eventmanager.events.clear()
    _save(plugin_module, instance)
    assert plugin_module.eventmanager.events == []


def test_incremental_refresh_reports_removed_node(plugin_module: Any) -> None:
    """增量刷新删除旧节点时，事件携带 removed 明细。"""
    instance = _new_plugin(
        plugin_module,
        {"enabled": True, "strm_notify": True, "strm_plugin_id": "GDriveStrm"},
    )
    _save(plugin_module, instance)
    plugin_module.eventmanager.events.clear()
    files = [
        {"id": "root", "name": "Root", "mimeType": FOLDER, "parents": []},
        {"id": "d1", "name": "Movies", "mimeType": FOLDER, "parents": ["root"]},
        {
            "id": "f2",
            "name": "b.mkv",
            "mimeType": "video/x-matroska",
            "parents": ["d1"],
            "size": "20",
        },
    ]
    change = _save_files(plugin_module, instance, files)
    assert [item["file_id"] for item in change.removed] == ["f1"]
    assert [item["path"] for item in change.removed] == ["/Movies/a.mkv"]
    events = plugin_module.eventmanager.events
    assert len(events) == 1
    data = events[0][1]["data"]
    assert data["removed"] == [{"file_id": "f1", "path": "/Movies/a.mkv"}]
    assert [item["file_id"] for item in data["added"]] == ["f2"]


def test_unchanged_refresh_does_not_publish(plugin_module: Any) -> None:
    """没有变化的刷新不发送空事件。"""
    instance = _new_plugin(
        plugin_module,
        {"enabled": True, "strm_notify": True, "strm_plugin_id": "GDriveStrm"},
    )
    _save(plugin_module, instance)
    plugin_module.eventmanager.events.clear()
    _save(plugin_module, instance)
    assert plugin_module.eventmanager.events == []


def test_archive_upsert_publishes_event(plugin_module: Any) -> None:
    """归档入树增量写入后发送 kind=archive 的事件。"""
    instance = _new_plugin(
        plugin_module,
        {"enabled": True, "strm_notify": True, "strm_plugin_id": "GDriveStrm"},
    )
    plugin_module.eventmanager.events.clear()
    node = plugin_module.TreeNodeData(
        file_id="f9",
        parent_file_id="root",
        name="new.mkv",
        mime_type="video/x-matroska",
        is_dir=False,
        size=1,
        modified_time=0.0,
        created_time=0.0,
        path="/new.mkv",
        depth=1,
        md5="",
        trashed=False,
        shortcut_target_id="",
    )
    added, updated = instance._upsert_nodes([node])
    assert [item.file_id for item in added] == ["f9"]
    assert updated == []
    events = plugin_module.eventmanager.events
    assert len(events) == 1
    data = events[0][1]["data"]
    assert data["kind"] == "archive"
    assert [item["file_id"] for item in data["added"]] == ["f9"]
