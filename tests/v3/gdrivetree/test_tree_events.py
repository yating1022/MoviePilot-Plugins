"""目录树变化事件载荷的纯逻辑测试。"""

from __future__ import annotations

from typing import Any

import pytest

from _plugin_loader import load_module


@pytest.fixture(scope="module")
def events() -> Any:
    """加载 tree_events 模块。"""
    return load_module("tree_events")


@pytest.fixture(scope="module")
def builder() -> Any:
    """加载 tree_builder 模块。"""
    return load_module("tree_builder")


def _node(builder: Any, file_id: str = "f1", is_dir: bool = False) -> Any:
    """构造一个节点。"""
    return builder.TreeNodeData(
        file_id=file_id,
        parent_file_id="root",
        name="a.mkv" if not is_dir else "Movies",
        mime_type="video/x-matroska",
        is_dir=is_dir,
        size=10,
        modified_time=1.5,
        created_time=0.5,
        path="/Movies/a.mkv" if not is_dir else "/Movies/",
        depth=2 if not is_dir else 1,
        md5="abc",
        trashed=False,
        shortcut_target_id="target-1" if not is_dir else "",
    )


def test_node_payload_has_stable_fields(events: Any, builder: Any) -> None:
    """节点载荷字段固定，便于消费方长期解析。"""
    payload = events.node_payload(_node(builder))
    assert payload == {
        "file_id": "f1",
        "path": "/Movies/a.mkv",
        "name": "a.mkv",
        "is_dir": False,
        "size": 10,
        "modified_time": 1.5,
        "shortcut_target_id": "target-1",
    }


def test_build_payload_targets_plugin(events: Any, builder: Any) -> None:
    """定向事件包含目标插件键与稳定事件名。"""
    change = events.change_from_nodes(
        "refresh", added=[_node(builder)], removed=[("old", "/old.mkv")]
    )
    payload = events.build_payload("GDriveTree", "GDriveStrm", change)
    assert payload["plugin_id"] == "GDriveTree"
    assert payload["event_name"] == "tree.changed"
    assert payload["__mp_target_plugin_id"] == "GDriveStrm"
    data = payload["data"]
    assert data["kind"] == "refresh"
    assert data["full"] is False
    assert data["truncated"] is False
    assert data["node_count"] == 2
    assert data["added"][0]["file_id"] == "f1"
    assert data["removed"] == [{"file_id": "old", "path": "/old.mkv"}]


def test_build_payload_full_signal(events: Any) -> None:
    """全量信号不携带明细。"""
    payload = events.build_payload(
        "GDriveTree", "GDriveStrm", events.full_signal("clear")
    )
    data = payload["data"]
    assert data["kind"] == "clear"
    assert data["full"] is True
    assert data["added"] == []
    assert data["updated"] == []
    assert data["removed"] == []


def test_build_payload_truncates_large_change(events: Any, builder: Any) -> None:
    """超过上限的变更退化为全量信号。"""
    nodes = [_node(builder, file_id=f"f{index}") for index in range(2001)]
    change = events.change_from_nodes("refresh", added=nodes)
    payload = events.build_payload("GDriveTree", "GDriveStrm", change)
    data = payload["data"]
    assert data["full"] is True
    assert data["truncated"] is True
    assert data["node_count"] == 2001
    assert data["added"] == []


def test_build_payload_without_target_omits_routing_key(events: Any) -> None:
    """没有目标插件时不写定向键。"""
    payload = events.build_payload("GDriveTree", "", events.full_signal("refresh"))
    assert "__mp_target_plugin_id" not in payload


def test_coerce_change_set_roundtrip(events: Any, builder: Any) -> None:
    """事件 data 段可还原为变更集。"""
    change = events.change_from_nodes(
        "archive", added=[_node(builder)], updated=[_node(builder, "f2")]
    )
    payload = events.build_payload("GDriveTree", "GDriveStrm", change)
    restored = events.coerce_change_set(payload["data"])
    assert restored.kind == "archive"
    assert restored.node_count == 2
    assert restored.added[0]["file_id"] == "f1"
    assert restored.updated[0]["file_id"] == "f2"
