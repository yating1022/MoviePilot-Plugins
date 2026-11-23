"""tree_builder 的纯逻辑单元测试（不依赖 MoviePilot 宿主）。"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from _plugin_loader import load_module

tree_builder = load_module("tree_builder")
errors = load_module("errors")

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
build_tree = tree_builder.build_tree
GDriveTreeBuildError = errors.GDriveTreeBuildError


def _item(
    file_id: str,
    name: str,
    mime_type: str = FOLDER,
    parents: List[str] | None = None,
    **extra: Any,
) -> Dict[str, Any]:
    """构造一条 files.list 风格的元数据。"""
    data: Dict[str, Any] = {
        "id": file_id,
        "name": name,
        "mimeType": mime_type,
        "parents": list(parents or []),
    }
    data.update(extra)
    return data


def test_build_tree_keeps_only_root_subtree() -> None:
    """根子树之外的节点必须被裁剪。"""
    files = [
        _item("d1", "Movies", parents=["root"]),
        _item("f1", "a.mkv", "video/x-matroska", ["d1"], size="100"),
        _item("f2", "b.mkv", "video/x-matroska", ["root"], size=200),
        _item("orphan", "lost.mkv", "video/x-matroska", ["elsewhere"], size=999),
    ]
    nodes, stats = build_tree(files, root_id="root", root_name="My Drive")
    ids = {node.file_id for node in nodes}
    assert ids == {"root", "d1", "f1", "f2"}
    assert stats.node_count == 4
    assert stats.dir_count == 2
    assert stats.file_count == 2
    assert stats.total_size == 300
    assert stats.max_depth == 2


def test_synthetic_root_and_paths() -> None:
    """根不在输入中时合成根，路径按 root_path 拼接。"""
    files = [
        _item("d1", "TV", parents=["root"]),
        _item("f1", "ep1.mkv", "video/x-matroska", ["d1"], size=10),
    ]
    nodes, _ = build_tree(
        files, root_id="root", root_name="Drive", root_path="/GDrive"
    )
    by_id = {node.file_id: node for node in nodes}
    assert by_id["root"].name == "Drive"
    assert by_id["root"].path == "/GDrive/"
    assert by_id["d1"].path == "/GDrive/TV/"
    assert by_id["f1"].path == "/GDrive/TV/ep1.mkv"
    assert by_id["f1"].depth == 2


def test_root_metadata_is_used_when_present() -> None:
    """根在输入中时使用真实名称/时间。"""
    files = [
        _item(
            "root",
            "Real Root",
            parents=[],
            modifiedTime="2024-01-02T03:04:05.000Z",
        ),
        _item("f1", "a.mkv", "video/x-matroska", ["root"], size=5),
    ]
    nodes, _ = build_tree(files, root_id="root", root_name="Fallback")
    by_id = {node.file_id: node for node in nodes}
    assert by_id["root"].name == "Real Root"
    assert by_id["root"].modified_time > 0


def test_multi_parent_uses_first_parent() -> None:
    """多父节点按第一个 parent 归入树中。"""
    files = [
        _item("d1", "A", parents=["root"]),
        _item("f1", "a.mkv", "video/x-matroska", ["d1", "other"], size=1),
    ]
    nodes, _ = build_tree(files, root_id="root", root_name="root")
    by_id = {node.file_id: node for node in nodes}
    assert by_id["f1"].parent_file_id == "d1"
    assert by_id["f1"].path.endswith("/A/a.mkv")


def test_trashed_nodes_are_skipped() -> None:
    """默认跳过回收站节点及其子树。"""
    files = [
        _item("d1", "Trash", parents=["root"], trashed=True),
        _item("f1", "gone.mkv", "video/x-matroska", ["d1"], size=1),
        _item("f2", "keep.mkv", "video/x-matroska", ["root"], size=2),
    ]
    nodes, stats = build_tree(files, root_id="root", root_name="root")
    assert {node.file_id for node in nodes} == {"root", "f2"}
    assert stats.file_count == 1


def test_trashed_nodes_can_be_included() -> None:
    """skip_trashed=False 时保留回收站节点。"""
    files = [
        _item("f1", "gone.mkv", "video/x-matroska", ["root"], size=1, trashed=True),
    ]
    nodes, stats = build_tree(
        files, root_id="root", root_name="root", skip_trashed=False
    )
    assert stats.file_count == 1
    assert any(node.file_id == "f1" for node in nodes)


def test_multi_parent_second_parent_is_reachable() -> None:
    """仅通过第二个父节点可达的子树也应入库。"""
    files = [
        _item("d1", "Inside", parents=["root"]),
        _item("f1", "a.mkv", "video/x-matroska", ["outside", "d1"], size=1),
    ]
    nodes, stats = build_tree(files, root_id="root", root_name="root")
    by_id = {node.file_id: node for node in nodes}
    assert "f1" in by_id
    assert by_id["f1"].parent_file_id == "d1"
    assert stats.file_count == 1


def test_shortcut_folder_target_counts_as_dir() -> None:
    """指向文件夹的快捷方式按目录统计。"""
    files = [
        _item(
            "sc",
            "Team Folder",
            SHORTCUT,
            ["root"],
            shortcutDetails={"targetId": "T1", "targetMimeType": FOLDER},
        )
    ]
    nodes, stats = build_tree(files, root_id="root", root_name="root")
    by_id = {node.file_id: node for node in nodes}
    assert by_id["sc"].is_dir is True
    assert stats.dir_count == 2
    assert stats.file_count == 0


def test_shortcut_target_is_preserved() -> None:
    """快捷方式节点保留目标 ID，但不展开目标。"""
    files = [
        _item(
            "sc",
            "Team Drive",
            SHORTCUT,
            ["root"],
            shortcutDetails={"targetId": "target-id"},
        )
    ]
    nodes, _ = build_tree(files, root_id="root", root_name="root")
    by_id = {node.file_id: node for node in nodes}
    assert by_id["sc"].shortcut_target_id == "target-id"
    assert not by_id["sc"].is_dir


def test_native_google_docs_have_zero_size() -> None:
    """Google 原生文档没有 size，按 0 统计。"""
    files = [
        _item(
            "doc",
            "Sheet",
            "application/vnd.google-apps.spreadsheet",
            ["root"],
            size="12345",
        )
    ]
    nodes, stats = build_tree(files, root_id="root", root_name="root")
    by_id = {node.file_id: node for node in nodes}
    assert by_id["doc"].size == 0
    assert stats.total_size == 0


def test_max_nodes_aborts() -> None:
    """节点数超过上限时抛错。"""
    files = [
        _item("f1", "a", "text/plain", ["root"], size=1),
        _item("f2", "b", "text/plain", ["root"], size=1),
    ]
    with pytest.raises(GDriveTreeBuildError):
        build_tree(files, root_id="root", root_name="root", max_nodes=1)


def test_empty_root_id_rejected() -> None:
    """空根 ID 直接拒绝。"""
    with pytest.raises(GDriveTreeBuildError):
        build_tree([], root_id="", root_name="root")


def test_nodes_are_sorted_by_depth_and_path() -> None:
    """输出顺序稳定，便于入库与分页。"""
    files = [
        _item("d1", "B", parents=["root"]),
        _item("d2", "A", parents=["root"]),
        _item("f1", "x", "text/plain", ["d1"], size=1),
    ]
    nodes, _ = build_tree(files, root_id="root", root_name="root")
    keys = [(node.depth, node.path) for node in nodes]
    assert keys == sorted(keys)
