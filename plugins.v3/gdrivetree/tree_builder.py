"""把 Google Drive 文件元数据构建为可裁剪的目录树。

本模块只做纯数据处理，不依赖 MoviePilot 宿主，也不发起网络请求。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import GDriveTreeBuildError

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."


@dataclass(slots=True)
class TreeNodeData:
    """一条目录树节点（与数据库表字段一一对应）。"""

    file_id: str
    parent_file_id: Optional[str]
    name: str
    mime_type: str
    is_dir: bool
    size: int
    modified_time: float
    created_time: float
    path: str
    depth: int
    md5: str
    trashed: bool
    shortcut_target_id: str


@dataclass(slots=True)
class TreeStats:
    """一次建树的汇总统计。"""

    node_count: int = 0
    dir_count: int = 0
    file_count: int = 0
    total_size: int = 0
    max_depth: int = 0


def normalize_root_path(root_path: Any) -> str:
    """
    把根路径归一化为以 ``/`` 开头和结尾的 POSIX 路径。

    :param root_path: 用户配置的虚拟根路径
    :return: 归一化路径，例如 ``/``、``/GDrive/``
    """
    text = str(root_path or "/").strip().replace("\\", "/")
    if not text:
        text = "/"
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    if not text.endswith("/"):
        text += "/"
    return text


def _parse_time(value: Any) -> float:
    """把 RFC3339 时间字符串转成 Unix 秒；无法解析时返回 0。"""
    if value in (None, ""):
        return 0.0
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return float(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _parse_size(value: Any) -> int:
    """把 size 字段转成非负整数；Drive 原生文档没有 size。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _all_parents(raw: Mapping[str, Any]) -> List[str]:
    """取全部父节点 ID；Drive 允许多父，按任意一个父可达即应入库。"""
    parents = raw.get("parents")
    if isinstance(parents, str):
        return [parents] if parents else []
    if isinstance(parents, (list, tuple)):
        return [str(parent) for parent in parents if parent]
    return []


def _join_path(parent_path: str, name: str, is_dir: bool) -> str:
    """在父路径下拼接子节点路径；目录保留结尾斜杠。"""
    base = parent_path or "/"
    child = f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}"
    if is_dir and not child.endswith("/"):
        child += "/"
    return child


def _is_native_doc(mime_type: str) -> bool:
    """判断是否为 Google 原生文档（没有 size，也没有媒体内容）。"""
    return mime_type.startswith(GOOGLE_APPS_PREFIX)


def build_tree(
    files: Iterable[Mapping[str, Any]],
    *,
    root_id: str,
    root_name: str,
    root_path: str = "/",
    max_nodes: int = 200_000,
    skip_trashed: bool = True,
) -> Tuple[List[TreeNodeData], TreeStats]:
    """
    从全量文件元数据中裁剪出根目录子树。

    先按 ``parents`` 建立子节点索引，再从 ``root_id`` 做 BFS；根子树之外的
    文件不会进入结果。根节点若不在输入中出现，会合成一条目录节点。

    :param files: ``files.list`` 返回的原始文件字典
    :param root_id: 裁剪起点（真实文件夹 ID）
    :param root_name: 根显示名（根元数据缺失时使用）
    :param root_path: 对外虚拟根路径
    :param max_nodes: 最大入库节点数，超出抛错
    :param skip_trashed: 是否跳过 trashed 节点
    :return: (按 depth/path 排序的节点列表, 统计)
    :raises GDriveTreeBuildError: 根 ID 为空或节点数超限
    """
    if not root_id:
        raise GDriveTreeBuildError("根目录 ID 为空，无法建树")

    normalized_root = normalize_root_path(root_path)
    children: Dict[str, List[Mapping[str, Any]]] = {}
    by_id: Dict[str, Mapping[str, Any]] = {}

    for raw in files:
        if not isinstance(raw, Mapping):
            continue
        file_id = str(raw.get("id") or "")
        if not file_id:
            continue
        if skip_trashed and bool(raw.get("trashed")):
            continue
        by_id.setdefault(file_id, raw)
        for parent_id in _all_parents(raw):
            children.setdefault(parent_id, []).append(raw)

    root_raw = by_id.get(root_id) or {}
    root_mime = str(root_raw.get("mimeType") or FOLDER_MIME)
    root_shortcut = (root_raw.get("shortcutDetails") or {}).get("targetId") or ""
    root_node = TreeNodeData(
        file_id=root_id,
        parent_file_id=None,
        name=str(root_raw.get("name") or root_name or "/"),
        mime_type=root_mime,
        is_dir=True,
        size=0,
        modified_time=_parse_time(root_raw.get("modifiedTime")),
        created_time=_parse_time(root_raw.get("createdTime")),
        path=normalized_root,
        depth=0,
        md5=str(root_raw.get("md5Checksum") or ""),
        trashed=bool(root_raw.get("trashed")),
        shortcut_target_id=str(root_shortcut),
    )
    nodes: List[TreeNodeData] = [root_node]
    stats = TreeStats(node_count=1, dir_count=1)

    visited = {root_id}
    queue: deque[Tuple[str, str, int]] = deque([(root_id, normalized_root, 0)])
    while queue:
        parent_id, parent_path, parent_depth = queue.popleft()
        for raw in children.get(parent_id, []):
            file_id = str(raw.get("id") or "")
            if not file_id or file_id in visited:
                continue
            visited.add(file_id)
            mime_type = str(raw.get("mimeType") or "")
            name = str(raw.get("name") or "")
            depth = parent_depth + 1
            shortcut_target = ""
            shortcut_is_dir = False
            if mime_type == SHORTCUT_MIME:
                details = raw.get("shortcutDetails") or {}
                shortcut_target = str(details.get("targetId") or "")
                shortcut_is_dir = str(details.get("targetMimeType") or "") == FOLDER_MIME
            is_dir = mime_type == FOLDER_MIME or shortcut_is_dir
            size = 0 if (is_dir or _is_native_doc(mime_type)) else _parse_size(raw.get("size"))
            node = TreeNodeData(
                file_id=file_id,
                parent_file_id=parent_id,
                name=name,
                mime_type=mime_type,
                is_dir=is_dir,
                size=size,
                modified_time=_parse_time(raw.get("modifiedTime")),
                created_time=_parse_time(raw.get("createdTime")),
                path=_join_path(parent_path, name, is_dir),
                depth=depth,
                md5=str(raw.get("md5Checksum") or ""),
                trashed=bool(raw.get("trashed")),
                shortcut_target_id=shortcut_target,
            )
            nodes.append(node)
            if is_dir:
                queue.append((file_id, node.path, depth))
            if len(nodes) > max_nodes:
                raise GDriveTreeBuildError(
                    f"目录树节点数超过上限 {max_nodes}，已中止；"
                    "请缩小根目录范围或调大 max_nodes"
                )

    stats.node_count = len(nodes)
    stats.dir_count = sum(1 for node in nodes if node.is_dir)
    stats.file_count = stats.node_count - stats.dir_count
    stats.total_size = sum(node.size for node in nodes if not node.is_dir)
    stats.max_depth = max((node.depth for node in nodes), default=0)

    nodes.sort(key=lambda node: (node.depth, node.path))
    return nodes, stats
