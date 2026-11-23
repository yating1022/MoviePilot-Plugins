"""GDriveStrm 实例级测试：宿主桩 + 两个 SQLite 库。"""

from __future__ import annotations

import importlib.util
import queue
import sys
import threading
import types
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins.v3" / "gdrivestrm"
PLUGIN_PATH = PLUGIN_DIR / "__init__.py"
PACKAGE_NAME = "gdrivestrm_plugin_under_test"
STUB_NAMES = (
    "app",
    "app.schemas",
    "app.schemas.types",
    "app.sdk",
    "app.sdk.config",
    "app.sdk.database",
    "app.sdk.events",
    "app.sdk.logging",
    "app.sdk.plugin",
)
URL_TEMPLATE = (
    "https://www.googleapis.com/drive/v3/files/"
    "{fileId}?alt=media&supportsAllDrives=true"
)


class _StubEventType:
    """宿主 EventType 的最小替身。"""

    PluginTriggered = "plugin.triggered"


class _StubEventManager:
    """宿主 eventmanager 的最小替身。"""

    def register(self, *args: Any, **kwargs: Any):
        """返回保持原函数不变的装饰器。"""

        def decorator(func):
            return func

        return decorator


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


class _Database:
    """插件库句柄的最小替身。"""

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
    """宿主 _PluginBase 的最小替身，按 plugin_id 路由数据库。"""

    def __init__(self) -> None:
        self._databases: Dict[str, _Database] = {}
        self._data: Dict[str, Any] = {}

    def get_config(self, plugin_id: Optional[str] = None) -> Any:
        """返回预设配置。"""
        return None

    def get_database(self, plugin_id: Optional[str] = None) -> _Database:
        """按插件 ID 返回数据库句柄。"""
        key = plugin_id or type(self).__name__
        if key not in self._databases:
            self._databases[key] = _Database()
        return self._databases[key]

    def save_data(self, key: str, value: Any, plugin_id: Optional[str] = None) -> None:
        """模拟数据保存。"""
        self._data[key] = value

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """模拟数据读取。"""
        return self._data.get(key) if key else dict(self._data)

    def del_data(self, key: str, plugin_id: Optional[str] = None) -> Any:
        """模拟数据删除。"""
        return self._data.pop(key, None)


def _declarative_base() -> type[DeclarativeBase]:
    """构造独立的声明式基类。"""

    class Base(DeclarativeBase):
        pass

    return Base


def _install_stubs(config_path: Path) -> None:
    """在 sys.modules 中安装 app.* 桩。"""
    app = types.ModuleType("app")
    schemas = types.ModuleType("app.schemas")
    schema_types = types.ModuleType("app.schemas.types")
    sdk = types.ModuleType("app.sdk")
    config = types.ModuleType("app.sdk.config")
    database = types.ModuleType("app.sdk.database")
    events = types.ModuleType("app.sdk.events")
    logging_module = types.ModuleType("app.sdk.logging")
    plugin_module = types.ModuleType("app.sdk.plugin")
    schema_types.EventType = _StubEventType
    config.settings = SimpleNamespace(CONFIG_PATH=config_path)
    database.plugin_declarative_base = _declarative_base
    events.Event = object
    events.eventmanager = _StubEventManager()
    logging_module.logger = _StubLogger()
    plugin_module._PluginBase = _StubPluginBase
    schemas.types = schema_types
    sdk.config = config
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
        "app.sdk.config": config,
        "app.sdk.database": database,
        "app.sdk.events": events,
        "app.sdk.logging": logging_module,
        "app.sdk.plugin": plugin_module,
    })


@pytest.fixture(scope="module")
def plugin_module(tmp_path_factory: Any):
    """加载插件包并恢复宿主模块。"""
    config_path = tmp_path_factory.mktemp("config")
    saved = {name: sys.modules.get(name) for name in STUB_NAMES + (PACKAGE_NAME,)}
    _install_stubs(config_path)
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


def _seed_tree(database: _Database, nodes: List[Dict[str, Any]], root_path: str = "/") -> None:
    """建目录树表并写入节点。"""
    with database.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE IF NOT EXISTS tree_meta (id INTEGER PRIMARY KEY, root_path TEXT)")
        )
        connection.execute(
            text("INSERT OR REPLACE INTO tree_meta (id, root_path) VALUES (1, :path)"),
            {"path": root_path},
        )
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tree_node (file_id TEXT, name TEXT, path TEXT, "
                "size INTEGER, modified_time REAL, shortcut_target_id TEXT, is_dir BOOLEAN)"
            )
        )
        for node in nodes:
            connection.execute(
                text(
                    "INSERT INTO tree_node (file_id, name, path, size, modified_time, "
                    "shortcut_target_id, is_dir) VALUES (:file_id, :name, :path, :size, "
                    ":modified_time, :shortcut_target_id, :is_dir)"
                ),
                {
                    "file_id": node.get("file_id", ""),
                    "name": node.get("name", ""),
                    "path": node.get("path", ""),
                    "size": node.get("size", 0),
                    "modified_time": node.get("modified_time", 0.0),
                    "shortcut_target_id": node.get("shortcut_target_id", ""),
                    "is_dir": node.get("is_dir", False),
                },
            )


def _video(file_id: str, path: str, name: str = "") -> Dict[str, Any]:
    """构造视频节点。"""
    return {
        "file_id": file_id,
        "name": name or path.rsplit("/", 1)[-1],
        "path": path,
        "size": 10,
        "modified_time": 1.0,
        "is_dir": False,
    }


def _new_plugin(module: Any, output: Path, **overrides: Any) -> Any:
    """创建实例、装配两个库并建表。"""
    instance = module.GDriveStrm()
    instance._databases["GDriveStrm"] = _Database()
    instance._databases["GDriveTree"] = _Database()
    config = {
        "enabled": True,
        "output_dir": str(output),
        "event_watch": True,
        "cleanup": True,
    }
    config.update(overrides)
    instance.init_plugin(config)
    module.StrmMeta.metadata.create_all(instance._databases["GDriveStrm"].engine)
    # 测试直接调用 _execute，停掉后台线程并换一个空队列，避免执行时序不确定
    instance.stop_service()
    instance._queue = queue.Queue()
    return instance


def _run(instance: Any, job: Dict[str, Any], module: Any) -> None:
    """同步执行一个作业。"""
    instance._execute(job, threading.Event())


def _event(payload: Dict[str, Any], **overrides: Any) -> Any:
    """构造 PluginTriggered 事件对象。"""
    data = {
        "plugin_id": "GDriveTree",
        "event_name": "tree.changed",
        "data": payload,
    }
    data.update(overrides)
    return SimpleNamespace(event_data=data)


def test_full_sync_maps_tree_to_strm(tmp_path: Path, plugin_module: Any) -> None:
    """全量同步按树结构生成 STRM，过滤非视频。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(instance._databases["GDriveTree"], [
        {"file_id": "root", "name": "Root", "path": "/", "is_dir": True},
        {"file_id": "d1", "name": "A", "path": "/A/", "is_dir": True},
        _video("f1", "/A/B/a.mkv"),
        {"file_id": "f2", "name": "a.srt", "path": "/A/B/a.srt", "is_dir": False},
    ])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "A" / "B" / "a.strm"
    assert written.read_text(encoding="utf-8") == URL_TEMPLATE.replace(
        "{fileId}", "f1"
    )
    assert not (tmp_path / "out" / "A" / "B" / "a.srt.strm").exists()
    meta = instance._load_meta()
    assert meta["status"] == "success"
    assert meta["record_count"] == 1
    assert meta["target_count"] == 1


def test_full_sync_is_idempotent(tmp_path: Path, plugin_module: Any) -> None:
    """重复全量同步不改变已一致文件的 mtime。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "a.strm"
    before = written.stat().st_mtime_ns
    _run(instance, {"kind": "full"}, plugin_module)
    assert written.stat().st_mtime_ns == before
    assert instance._load_meta()["skipped_count"] == 1


def test_event_adds_and_removes_strm(tmp_path: Path, plugin_module: Any) -> None:
    """事件新增生成 STRM，事件删除清理 STRM。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "A" / "a.strm"
    assert written.exists()

    plugin_module.logger.messages.clear()
    added = {
        "file_id": "f2",
        "name": "b.mkv",
        "path": "/A/b.mkv",
        "is_dir": False,
        "size": 1,
        "modified_time": 1.0,
    }
    _run(instance, {"kind": "event", "payload": {"added": [added]}}, plugin_module)
    assert (tmp_path / "out" / "A" / "b.strm").exists()
    logs = "\n".join(plugin_module.logger.messages)
    assert "新增 STRM" in logs
    assert "A/b.strm" in logs

    plugin_module.logger.messages.clear()
    removed = {"file_id": "f1", "path": "/A/a.mkv"}
    _run(instance, {"kind": "event", "payload": {"removed": [removed]}}, plugin_module)
    assert not written.exists()
    assert "删除 STRM" in "\n".join(plugin_module.logger.messages)
    assert instance._load_meta()["removed_count"] == 1


def test_event_removal_keeps_file_when_cleanup_off(
    tmp_path: Path, plugin_module: Any
) -> None:
    """清理关闭时事件删除不删文件。"""
    instance = _new_plugin(plugin_module, tmp_path / "out", cleanup=False)
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "A" / "a.strm"
    _run(
        instance,
        {"kind": "event", "payload": {"removed": [{"file_id": "f1", "path": "/A/a.mkv"}]}},
        plugin_module,
    )
    assert written.exists()


def test_diff_repairs_missing_file(tmp_path: Path, plugin_module: Any) -> None:
    """增量对账补齐被外部删除的 STRM。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "A" / "a.strm"
    written.unlink()
    _run(instance, {"kind": "diff"}, plugin_module)
    assert written.exists()


def test_missing_meta_keeps_existing_files(
    tmp_path: Path, plugin_module: Any
) -> None:
    """目录树未建树时状态失败且不清空已有文件。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    written = tmp_path / "out" / "A" / "a.strm"
    assert written.exists()
    with instance._databases["GDriveTree"].engine.begin() as connection:
        connection.execute(text("DELETE FROM tree_meta"))
    _run(instance, {"kind": "diff"}, plugin_module)
    assert written.exists()
    assert instance._load_meta()["status"] == "failed"


def test_handler_filters_other_plugins(tmp_path: Path, plugin_module: Any) -> None:
    """其他插件或事件名的事件被忽略。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    instance.on_plugin_triggered(_event({"added": []}, plugin_id="Other"))
    with pytest.raises(Empty):
        instance._queue.get_nowait()
    instance.on_plugin_triggered(_event({"added": []}, event_name="other.changed"))
    with pytest.raises(Empty):
        instance._queue.get_nowait()
    instance.on_plugin_triggered(_event({"added": []}))
    job = instance._queue.get_nowait()
    assert job["kind"] == "event"
    assert "收到目录树事件" in "\n".join(plugin_module.logger.messages)


def test_invalid_template_aborts_without_writing(
    tmp_path: Path, plugin_module: Any
) -> None:
    """模板缺少占位符时中止同步，不写文件也不清理旧文件。"""
    instance = _new_plugin(
        plugin_module, tmp_path / "out", url_template="https://example.test/x"
    )
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    meta = instance._load_meta()
    assert meta["status"] == "failed"
    assert "{fileId}" in meta["error"]
    assert not (tmp_path / "out" / "A" / "a.strm").exists()


def test_output_dir_change_skips_cleanup_at_new_root(
    tmp_path: Path, plugin_module: Any
) -> None:
    """切换输出目录后不删除新根目录中的用户文件，并重置记录。"""
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    instance = _new_plugin(plugin_module, out1)
    _seed_tree(instance._databases["GDriveTree"], [_video("f1", "/A/a.mkv")])
    _run(instance, {"kind": "full"}, plugin_module)
    assert (out1 / "A" / "a.strm").exists()
    user_file = out2 / "User" / "keep.strm"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("user-owned", encoding="utf-8")
    instance._output_dir = str(out2)
    _run(instance, {"kind": "diff"}, plugin_module)
    assert user_file.exists()
    assert (out2 / "A" / "a.strm").exists()
    assert instance._load_meta()["output_dir"] == str(out2.resolve())
    assert instance._load_meta()["record_count"] == 1


def test_name_swap_event_persists_without_unique_conflict(
    tmp_path: Path, plugin_module: Any
) -> None:
    """两个文件互换路径时，同一次事件不能触发 rel_path 唯一约束。"""
    instance = _new_plugin(plugin_module, tmp_path / "out")
    _seed_tree(
        instance._databases["GDriveTree"],
        [_video("X", "/x.mkv"), _video("Y", "/y.mkv")],
    )
    _run(instance, {"kind": "full"}, plugin_module)
    assert (tmp_path / "out" / "x.strm").exists()
    assert (tmp_path / "out" / "y.strm").exists()
    _run(
        instance,
        {
            "kind": "event",
            "payload": {
                "updated": [
                    {"file_id": "X", "name": "y.mkv", "path": "/y.mkv", "is_dir": False},
                    {"file_id": "Y", "name": "x.mkv", "path": "/x.mkv", "is_dir": False},
                ]
            },
        },
        plugin_module,
    )
    assert instance._load_meta()["status"] == "success"
    records = instance._load_records()
    assert records["x.strm"].source_file_id == "Y"
    assert records["y.strm"].source_file_id == "X"
    assert "Y" in (tmp_path / "out" / "x.strm").read_text(encoding="utf-8")
    assert "X" in (tmp_path / "out" / "y.strm").read_text(encoding="utf-8")


def test_merge_jobs_prefers_full_and_diff() -> None:
    """合并规则：full > diff > 事件；超大事件升级为 diff。"""
    module = sys.modules.get(PACKAGE_NAME)
    assert module is not None
    assert module.GDriveStrm._merge_jobs([{"kind": "diff"}, {"kind": "full"}]) == {
        "kind": "full"
    }
    assert module.GDriveStrm._merge_jobs([{"kind": "diff"}, {"kind": "event"}]) == {
        "kind": "diff"
    }
    merged = module.GDriveStrm._merge_jobs([
        {"kind": "event", "payload": {"added": [{"file_id": "1"}], "full": False}}
    ])
    assert merged["kind"] == "event"
    assert merged["payload"]["added"] == [{"file_id": "1"}]
