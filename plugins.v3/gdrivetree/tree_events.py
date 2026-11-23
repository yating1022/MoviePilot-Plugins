"""目录树变化事件的载荷构建。

本模块只做纯数据处理，不依赖 MoviePilot 宿主，也不发起网络请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .tree_builder import TreeNodeData

# 跨插件事件稳定名：GDriveStrm 等消费方按此过滤
EVENT_NAME = "tree.changed"
# 单条事件最多携带的变更节点数；超过后退化为全量信号
MAX_EVENT_NODES = 2000


@dataclass(frozen=True, slots=True)
class TreeChangeSet:
    """一次目录树变更的摘要。"""

    kind: str = "refresh"
    added: Tuple[Dict[str, Any], ...] = ()
    updated: Tuple[Dict[str, Any], ...] = ()
    removed: Tuple[Dict[str, Any], ...] = ()
    full: bool = False
    node_count_hint: int = 0

    @property
    def node_count(self) -> int:
        """返回携带的变更节点总数（全量信号使用提示值）。"""
        return (
            len(self.added)
            + len(self.updated)
            + len(self.removed)
            or int(self.node_count_hint or 0)
        )


def node_payload(node: TreeNodeData) -> Dict[str, Any]:
    """
    把建树节点转换为事件载荷。

    :param node: 目录树节点
    :return: 可 JSON 序列化的节点字典
    """
    return {
        "file_id": node.file_id,
        "path": node.path,
        "name": node.name,
        "is_dir": bool(node.is_dir),
        "size": int(node.size or 0),
        "modified_time": float(node.modified_time or 0.0),
        "shortcut_target_id": node.shortcut_target_id or "",
    }


def removed_payload(file_id: str, path: str) -> Dict[str, Any]:
    """
    构造删除节点的载荷。

    :param file_id: 被删除节点的文件 ID
    :param path: 被删除节点的完整路径
    :return: 可 JSON 序列化的节点字典
    """
    return {"file_id": str(file_id or ""), "path": str(path or "")}


def change_from_nodes(
    kind: str,
    *,
    added: Iterable[TreeNodeData] = (),
    updated: Iterable[TreeNodeData] = (),
    removed: Iterable[Tuple[str, str]] = (),
    full: bool = False,
) -> TreeChangeSet:
    """
    由节点对象构建变更集。

    :param kind: 变更来源，例如 refresh/archive/clear
    :param added: 新增节点
    :param updated: 更新节点
    :param removed: 删除节点，元素为 ``(file_id, path)``
    :param full: 是否要求消费方按全量对账
    :return: 冻结的变更集
    """
    added_items = _as_list(added)
    updated_items = _as_list(updated)
    removed_items = _as_list(removed)
    total = len(added_items) + len(updated_items) + len(removed_items)
    if full or total > MAX_EVENT_NODES:
        # 先判断规模，避免为大变更物化整批字典
        return TreeChangeSet(
            kind=str(kind or "refresh"),
            full=True,
            node_count_hint=total,
        )
    return TreeChangeSet(
        kind=str(kind or "refresh"),
        added=tuple(node_payload(node) for node in added_items),
        updated=tuple(node_payload(node) for node in updated_items),
        removed=tuple(removed_payload(file_id, path) for file_id, path in removed_items),
    )


def _as_list(value: Iterable[Any]) -> List[Any]:
    """把可迭代对象转为列表；已是列表时直接复用。"""
    return value if isinstance(value, list) else list(value)


def full_signal(kind: str, node_count: int = 0) -> TreeChangeSet:
    """
    构建不带明细的全量信号。

    :param kind: 变更来源
    :param node_count: 本次全量涉及的节点数，仅用于日志与消费方提示
    :return: ``full=True`` 的变更集
    """
    return TreeChangeSet(
        kind=str(kind or "refresh"),
        full=True,
        node_count_hint=int(node_count or 0),
    )


def build_payload(
    publisher_id: str,
    target_plugin_id: str,
    change: TreeChangeSet,
) -> Dict[str, Any]:
    """
    构建 ``EventType.PluginTriggered`` 的事件数据。

    变更节点超过 ``MAX_EVENT_NODES`` 或 ``change.full`` 为真时，只发送全量信号，
    避免事件载荷过大和事件总线排队。

    :param publisher_id: 发布事件的插件实例 ID
    :param target_plugin_id: 目标插件实例 ID
    :param change: 目录树变更集
    :return: 可直接交给 ``eventmanager.send_event`` 的字典
    """
    total = change.node_count
    truncated = total > MAX_EVENT_NODES
    is_full = bool(change.full) or truncated
    if is_full:
        added: List[Dict[str, Any]] = []
        updated: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
    else:
        added = list(change.added)
        updated = list(change.updated)
        removed = list(change.removed)
    data = {
        "kind": change.kind,
        "full": is_full,
        "truncated": truncated,
        "node_count": total,
        "added": added,
        "updated": updated,
        "removed": removed,
    }
    payload: Dict[str, Any] = {
        "plugin_id": str(publisher_id or ""),
        "event_name": EVENT_NAME,
        "data": data,
    }
    target = str(target_plugin_id or "").strip()
    if target:
        payload["__mp_target_plugin_id"] = target
    return payload


def coerce_change_set(data: Any, kind: str = "refresh") -> TreeChangeSet:
    """
    从事件字典还原变更集，供测试或消费方复用。

    :param data: ``build_payload`` 生成的 ``data`` 段
    :param kind: 缺省变更来源
    :return: 变更集
    """
    if not isinstance(data, dict):
        return TreeChangeSet(kind=kind)
    return TreeChangeSet(
        kind=str(data.get("kind") or kind),
        added=tuple(_dicts(data.get("added"))),
        updated=tuple(_dicts(data.get("updated"))),
        removed=tuple(_dicts(data.get("removed"))),
        full=bool(data.get("full")),
        node_count_hint=int(data.get("node_count") or 0),
    )


def _dicts(value: Any) -> List[Dict[str, Any]]:
    """把任意序列过滤为字典列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]
