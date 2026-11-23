"""strm_syncer 的文件系统同步测试。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict

import pytest

from _gdrivestrm_loader import load_module

mapper = load_module("strm_mapper")
syncer_module = load_module("strm_syncer")
EXTENSIONS = mapper.parse_extensions(None)
URL = mapper.DEFAULT_URL_TEMPLATE


def _target(
    rel_path: str = "A/a.strm",
    file_id: str = "f1",
    url: str = "https://example.test/f1",
    source_path: str = "/A/a.mkv",
) -> Any:
    """构造同步目标。"""
    return mapper.StrmTarget(
        rel_path=rel_path,
        source_file_id=file_id,
        source_path=source_path,
        url=url,
        size=1,
        modified_time=1.0,
    )


def _record(
    rel_path: str = "A/a.strm",
    file_id: str = "f1",
    url: str = "https://example.test/f1",
    source_path: str = "/A/a.mkv",
) -> Any:
    """构造记录。"""
    return syncer_module.StrmRecordData(
        rel_path=rel_path,
        source_file_id=file_id,
        source_path=source_path,
        url=url,
        size=1,
        modified_time=1.0,
    )


def _apply(outcome: Any, records: Dict[str, Any]) -> Dict[str, Any]:
    """把同步结果合并进记录字典，模拟插件持久化。"""
    merged = dict(records)
    for item in outcome.added + outcome.updated:
        merged[item.rel_path] = item
    for relative in outcome.removed:
        merged.pop(relative, None)
    return merged


def test_full_sync_writes_nested_file(tmp_path: Path) -> None:
    """全量同步创建完整目录链并写入 URL。"""
    syncer = syncer_module.StrmSyncer(tmp_path / "out")
    outcome = syncer.full_sync({"A/B/C/D/a.strm": _target("A/B/C/D/a.strm")}, {})
    written = tmp_path / "out" / "A" / "B" / "C" / "D" / "a.strm"
    assert written.read_text(encoding="utf-8") == "https://example.test/f1"
    assert [item.rel_path for item in outcome.added] == ["A/B/C/D/a.strm"]
    assert outcome.errors == []


def test_full_sync_is_idempotent(tmp_path: Path) -> None:
    """内容一致时不重写文件，mtime 保持不变。"""
    syncer = syncer_module.StrmSyncer(tmp_path / "out")
    first = syncer.full_sync({"A/a.strm": _target()}, {})
    records = _apply(first, {})
    written = tmp_path / "out" / "A" / "a.strm"
    before = written.stat().st_mtime_ns
    second = syncer.full_sync({"A/a.strm": _target()}, records)
    assert second.added == []
    assert second.updated == []
    assert second.skipped == 1
    assert written.stat().st_mtime_ns == before


def test_full_sync_rewrites_changed_url(tmp_path: Path) -> None:
    """URL 变化时重写内容并产出 updated。"""
    syncer = syncer_module.StrmSyncer(tmp_path / "out")
    first = syncer.full_sync({"A/a.strm": _target()}, {})
    records = _apply(first, {})
    outcome = syncer.full_sync(
        {"A/a.strm": _target(url="https://example.test/new")}, records
    )
    assert [item.url for item in outcome.updated] == ["https://example.test/new"]
    written = tmp_path / "out" / "A" / "a.strm"
    assert written.read_text(encoding="utf-8") == "https://example.test/new"


def test_full_sync_cleanup_removes_stale_and_empty_dirs(tmp_path: Path) -> None:
    """清理开启时删除失效文件和空目录。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=True)
    first = syncer.full_sync({"Old/deep/a.strm": _target("Old/deep/a.strm")}, {})
    records = _apply(first, {})
    outcome = syncer.full_sync({"New/a.strm": _target("New/a.strm")}, records)
    assert outcome.removed == ["Old/deep/a.strm"]
    assert not (root / "Old").exists()
    assert (root / "New" / "a.strm").exists()


def test_full_sync_cleanup_off_keeps_stale(tmp_path: Path) -> None:
    """清理关闭时保留失效文件与记录。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=False)
    first = syncer.full_sync({"Old/a.strm": _target("Old/a.strm")}, {})
    records = _apply(first, {})
    outcome = syncer.full_sync({"New/a.strm": _target("New/a.strm")}, records)
    assert outcome.removed == []
    assert outcome.skipped == 1
    assert (root / "Old" / "a.strm").exists()


def test_apply_changes_adds_updates_and_removes(tmp_path: Path) -> None:
    """事件增量分别处理新增、更新与删除。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=True)
    existing = _record("Old/keep.strm", file_id="keep")
    removed = _record("Old/gone.strm", file_id="gone")
    (root / "Old").mkdir(parents=True)
    (root / "Old" / "keep.strm").write_text(existing.url, encoding="utf-8")
    (root / "Old" / "gone.strm").write_text(removed.url, encoding="utf-8")
    records = {existing.rel_path: existing, removed.rel_path: removed}
    outcome = syncer.apply_changes(
        added=[
            {
                "file_id": "new",
                "name": "new.mkv",
                "path": "/New/new.mkv",
                "is_dir": False,
            }
        ],
        updated=[
            {
                "file_id": "keep",
                "name": "keep.mkv",
                "path": "/Old/keep.mkv",
                "is_dir": False,
            }
        ],
        removed=[{"file_id": "gone", "path": "/Old/gone.mkv"}],
        root_path="/",
        extensions=EXTENSIONS,
        url_template=URL,
        records=records,
    )
    assert [item.rel_path for item in outcome.added] == ["New/new.strm"]
    assert "Old/keep.strm" in [item.rel_path for item in outcome.updated]
    assert outcome.removed == ["Old/gone.strm"]
    assert (root / "New" / "new.strm").exists()
    assert not (root / "Old" / "gone.strm").exists()
    assert "Old/gone.strm" not in records


def test_apply_changes_migrates_renamed_file(tmp_path: Path) -> None:
    """同一文件 ID 改名时迁移旧 STRM。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=True)
    old = _record("Old/name.strm", file_id="f1", source_path="/Old/name.mkv")
    (root / "Old").mkdir(parents=True)
    (root / "Old" / "name.strm").write_text(old.url, encoding="utf-8")
    records = {old.rel_path: old}
    outcome = syncer.apply_changes(
        added=[],
        updated=[
            {
                "file_id": "f1",
                "name": "renamed.mkv",
                "path": "/New/renamed.mkv",
                "is_dir": False,
            }
        ],
        removed=[],
        root_path="/",
        extensions=EXTENSIONS,
        url_template=URL,
        records=records,
    )
    assert outcome.removed == ["Old/name.strm"]
    assert not (root / "Old" / "name.strm").exists()
    assert (root / "New" / "renamed.strm").exists()
    assert "Old/name.strm" not in records


def test_apply_changes_removes_record_when_becomes_non_video(tmp_path: Path) -> None:
    """文件改为非视频后缀时按删除处理。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=True)
    old = _record("A/a.strm", file_id="f1", source_path="/A/a.mkv")
    (root / "A").mkdir(parents=True)
    (root / "A" / "a.strm").write_text(old.url, encoding="utf-8")
    records = {old.rel_path: old}
    outcome = syncer.apply_changes(
        added=[],
        updated=[
            {
                "file_id": "f1",
                "name": "a.srt",
                "path": "/A/a.srt",
                "is_dir": False,
            }
        ],
        removed=[],
        root_path="/",
        extensions=EXTENSIONS,
        url_template=URL,
        records=records,
    )
    assert outcome.removed == ["A/a.strm"]
    assert not (root / "A" / "a.strm").exists()


def test_apply_changes_replacement_at_same_path_keeps_new_file(
    tmp_path: Path,
) -> None:
    """同一路径被新文件 ID 替换时，删除事件不能删掉刚写入的新 STRM。"""
    root = tmp_path / "out"
    syncer = syncer_module.StrmSyncer(root, cleanup=True)
    old = _record("A/a.strm", file_id="old", source_path="/A/a.mkv")
    (root / "A").mkdir(parents=True)
    (root / "A" / "a.strm").write_text(old.url, encoding="utf-8")
    records = {old.rel_path: old}
    outcome = syncer.apply_changes(
        added=[
            {
                "file_id": "new",
                "name": "a.mkv",
                "path": "/A/a.mkv",
                "is_dir": False,
            }
        ],
        updated=[],
        removed=[{"file_id": "old", "path": "/A/a.mkv"}],
        root_path="/",
        extensions=EXTENSIONS,
        url_template=URL,
        records=records,
    )
    written = root / "A" / "a.strm"
    assert written.exists()
    assert "new" in written.read_text(encoding="utf-8")
    assert outcome.removed == []
    assert records["A/a.strm"].source_file_id == "new"


def test_stop_event_cancels_before_write(tmp_path: Path) -> None:
    """停止信号置位后不再写入。"""
    event = threading.Event()
    event.set()
    syncer = syncer_module.StrmSyncer(tmp_path / "out", stop_event=event)
    outcome = syncer.full_sync({"A/a.strm": _target()}, {})
    assert outcome.cancelled is True
    assert not (tmp_path / "out" / "A" / "a.strm").exists()
