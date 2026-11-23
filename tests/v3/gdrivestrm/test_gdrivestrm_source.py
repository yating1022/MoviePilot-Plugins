"""tree_source 读取目录树插件库的测试。"""

from __future__ import annotations

import threading
from typing import Any, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from _gdrivestrm_loader import load_module

source = load_module("tree_source")


class _Database:
    """最小数据库句柄：只提供 session()。"""

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


def _database(rows: List[Any], *, root_path: str = "/", meta: bool = True) -> _Database:
    """建表并写入样例节点。"""
    database = _Database()
    with database.engine.begin() as connection:
        if meta:
            connection.execute(
                text("CREATE TABLE tree_meta (id INTEGER PRIMARY KEY, root_path TEXT)")
            )
            connection.execute(
                text("INSERT INTO tree_meta (id, root_path) VALUES (1, :path)"),
                {"path": root_path},
            )
        else:
            connection.execute(
                text("CREATE TABLE tree_meta (id INTEGER PRIMARY KEY, root_path TEXT)")
            )
        connection.execute(
            text(
                "CREATE TABLE tree_node (file_id TEXT, name TEXT, path TEXT, "
                "size INTEGER, modified_time REAL, shortcut_target_id TEXT, "
                "is_dir BOOLEAN)"
            )
        )
        for row in rows:
            connection.execute(
                text(
                    "INSERT INTO tree_node (file_id, name, path, size, "
                    "modified_time, shortcut_target_id, is_dir) VALUES "
                    "(:file_id, :name, :path, :size, :modified_time, "
                    ":shortcut_target_id, :is_dir)"
                ),
                row,
            )
    return database


def test_read_snapshot_returns_files_only() -> None:
    """只返回文件节点并保留路径信息。"""
    database = _database(
        [
            {
                "file_id": "root",
                "name": "Root",
                "path": "/",
                "size": 0,
                "modified_time": 0.0,
                "shortcut_target_id": "",
                "is_dir": True,
            },
            {
                "file_id": "f1",
                "name": "a.mkv",
                "path": "/A/a.mkv",
                "size": 12,
                "modified_time": 1.5,
                "shortcut_target_id": "target-1",
                "is_dir": False,
            },
        ],
        root_path="/GDrive/",
    )
    snapshot = source.read_snapshot(database)
    assert snapshot.root_path == "/GDrive/"
    assert snapshot.raw_rows == 1
    assert snapshot.files[0].file_id == "f1"
    assert snapshot.files[0].path == "/A/a.mkv"
    assert snapshot.files[0].shortcut_target_id == "target-1"


def test_missing_meta_raises() -> None:
    """未建树时给出可读错误。"""
    database = _database([], meta=False)
    with pytest.raises(source.TreeSourceError, match="尚未构建"):
        source.read_snapshot(database)


def test_missing_table_raises() -> None:
    """缺表时包装为 TreeSourceError。"""
    database = _Database()
    with pytest.raises(source.TreeSourceError):
        source.read_root_path(database)


def test_stop_event_interrupts() -> None:
    """停止信号在读取过程中生效。"""
    database = _database(
        [
            {
                "file_id": "f1",
                "name": "a.mkv",
                "path": "/a.mkv",
                "size": 1,
                "modified_time": 0.0,
                "shortcut_target_id": "",
                "is_dir": False,
            }
        ]
    )
    event = threading.Event()
    event.set()
    with pytest.raises(InterruptedError):
        source.read_snapshot(database, event)
