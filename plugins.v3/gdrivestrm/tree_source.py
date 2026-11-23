"""只读读取 GDriveTree 插件的自有数据库。

通过宿主 ``get_database(plugin_id)`` 取得目录树插件库句柄，再用原生 SQL 读取
``tree_meta`` / ``tree_node``。SQL 集中在本模块，避免表结构细节散落到同步逻辑。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .strm_mapper import TreeFile

_META_SQL = "SELECT root_path FROM tree_meta WHERE id = 1"
_NODE_SQL = (
    "SELECT file_id, name, path, size, modified_time, shortcut_target_id "
    "FROM tree_node WHERE is_dir = :is_dir ORDER BY path, file_id"
)


class TreeSourceError(Exception):
    """目录树读取失败。"""


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """一次读取得到的目录树快照。"""

    root_path: str
    files: List[TreeFile]
    raw_rows: int = 0


def read_root_path(database: Any) -> str:
    """
    读取目录树根路径。

    :param database: 目录树插件的数据库句柄
    :return: 归一化前的根路径字符串
    :raises TreeSourceError: 表不存在、未建树或读取失败
    """
    try:
        with database.session() as session:
            row = session.execute(text(_META_SQL)).first()
    except SQLAlchemyError as err:
        raise TreeSourceError(f"读取目录树状态失败：{err}") from err
    if row is None:
        raise TreeSourceError("目录树尚未构建，请先在 GDriveTree 执行一次刷新")
    return str(row[0] or "/")


def read_snapshot(
    database: Any,
    stop_event: Optional[threading.Event] = None,
) -> TreeSnapshot:
    """
    读取目录树中的文件节点。

    :param database: 目录树插件的数据库句柄
    :param stop_event: 可选停止信号
    :return: 根路径与文件节点列表
    :raises TreeSourceError: 表不存在或读取失败
    :raises InterruptedError: 收到停止信号
    """
    root_path = "/"
    files: List[TreeFile] = []
    try:
        with database.session() as session:
            # 根路径与节点在同一事务内读取，避免刷新提交后两者来自不同快照
            meta = session.execute(text(_META_SQL)).first()
            if meta is None:
                raise TreeSourceError(
                    "目录树尚未构建，请先在 GDriveTree 执行一次刷新"
                )
            root_path = str(meta[0] or "/")
            result = session.execute(text(_NODE_SQL), {"is_dir": False})
            for row in result:
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("同步已中止")
                files.append(
                    TreeFile(
                        file_id=str(row[0] or ""),
                        name=str(row[1] or ""),
                        path=str(row[2] or ""),
                        size=int(row[3] or 0),
                        modified_time=float(row[4] or 0.0),
                        shortcut_target_id=str(row[5] or ""),
                    )
                )
    except SQLAlchemyError as err:
        raise TreeSourceError(f"读取目录树节点失败：{err}") from err
    return TreeSnapshot(root_path=root_path, files=files, raw_rows=len(files))


def probe(database: Any) -> None:
    """
    轻量探测目录树是否可用，供状态页使用。

    :param database: 目录树插件的数据库句柄
    :raises TreeSourceError: 不可用
    """
    read_root_path(database)
