"""把目录树目标同步为磁盘上的 STRM 文件。

只依赖标准库与 ``strm_mapper``；记录以字典注入，文件系统以输出根注入，
便于在临时目录中做单元测试。
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, Iterable, List, Optional

from .strm_mapper import (
    StrmTarget,
    TreeFile,
    build_target,
    is_video_name,
    strip_root_path,
)


@dataclass(frozen=True, slots=True)
class StrmRecordData:
    """已写入 STRM 的记录。"""

    rel_path: str
    source_file_id: str
    source_path: str
    url: str
    size: int = 0
    modified_time: float = 0.0


@dataclass
class SyncOutcome:
    """一次同步的结果，调用方据此持久化记录。"""

    added: List[StrmRecordData] = field(default_factory=list)
    updated: List[StrmRecordData] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    cancelled: bool = False


class StrmSyncer:
    """STRM 文件系统同步器。"""

    def __init__(
        self,
        root: Path,
        *,
        cleanup: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """
        :param root: STRM 输出根目录
        :param cleanup: 是否删除失效 STRM
        :param stop_event: 可选停止信号
        """
        self.root = Path(root)
        self.cleanup = bool(cleanup)
        self.stop_event = stop_event

    # ------------------------------------------------------------------
    # 全量 / 事件
    # ------------------------------------------------------------------
    def full_sync(
        self,
        targets: Dict[str, StrmTarget],
        records: Dict[str, StrmRecordData],
        *,
        verify_content: bool = False,
    ) -> SyncOutcome:
        """
        按完整目标集合对账：逐文件校验内容、清理失效、回收空目录。

        :param targets: rel_path → 目标
        :param records: rel_path → 已有记录
        :param verify_content: 记录一致时仍读取文件内容校验（全量同步）
        :return: 同步结果
        """
        outcome = SyncOutcome()
        for relative in sorted(targets):
            if self._stopped(outcome):
                return outcome
            target = targets[relative]
            self._ensure_target(
                target, records.get(relative), outcome, verify=verify_content
            )
        for relative in sorted(records):
            if relative in targets:
                continue
            if self._stopped(outcome):
                return outcome
            if self.cleanup:
                self._remove_record(relative, outcome)
            else:
                outcome.skipped += 1
        return outcome

    def apply_changes(
        self,
        added: Iterable[dict],
        updated: Iterable[dict],
        removed: Iterable[dict],
        *,
        root_path: str,
        extensions: FrozenSet[str],
        url_template: str,
        records: Dict[str, StrmRecordData],
    ) -> SyncOutcome:
        """
        只处理事件携带的变更节点，不扫描整棵树。

        :param added: 新增节点载荷
        :param updated: 更新节点载荷
        :param removed: 删除节点载荷
        :param root_path: 目录树根路径
        :param extensions: 视频后缀集合
        :param url_template: URL 模板
        :param records: 已有记录，方法内会同步更新
        :return: 同步结果
        """
        outcome = SyncOutcome()
        by_file, by_path = self._build_indexes(records)

        for node in added:
            if self._stopped(outcome):
                return outcome
            if not _is_file_node(node):
                continue
            name = str(node.get("name") or "")
            path = str(node.get("path") or "")
            if not is_video_name(name or path, extensions):
                # 新增的非视频节点没有历史记录，无需清理
                continue
            self._apply_node(
                node,
                root_path,
                extensions,
                url_template,
                records,
                by_file,
                by_path,
                outcome,
            )

        for node in updated:
            if self._stopped(outcome):
                return outcome
            if not _is_file_node(node):
                continue
            name = str(node.get("name") or "")
            path = str(node.get("path") or "")
            if not is_video_name(name or path, extensions):
                self._remove_matches(
                    node, records, by_file, by_path, outcome
                )
                continue
            self._apply_node(
                node,
                root_path,
                extensions,
                url_template,
                records,
                by_file,
                by_path,
                outcome,
            )

        for node in removed:
            if self._stopped(outcome):
                return outcome
            self._remove_matches(
                node, records, by_file, by_path, outcome, only_cleanup=True
            )
        return outcome

    def _apply_node(
        self,
        node: dict,
        root_path: str,
        extensions: FrozenSet[str],
        url_template: str,
        records: Dict[str, StrmRecordData],
        by_file: Dict[str, List[str]],
        by_path: Dict[str, List[str]],
        outcome: SyncOutcome,
    ) -> None:
        """把单个新增/更新节点写入磁盘并维护索引。"""
        path = str(node.get("path") or "")
        target = build_target(
            _tree_file(node), root_path, extensions, url_template
        )
        if target is None:
            outcome.errors.append(f"{path}: 无法映射为 STRM 目标")
            return
        # 同一文件 ID 改名/移动：先移除旧路径
        for old_relative in list(by_file.get(target.source_file_id, [])):
            if old_relative == target.rel_path:
                continue
            old = records.get(old_relative)
            if old is None or not self.cleanup:
                continue
            if not self._remove_record(old_relative, outcome):
                continue
            records.pop(old_relative, None)
            self._unindex(old_relative, old, by_file, by_path)
        old = records.get(target.rel_path)
        if old is not None and old.source_file_id != target.source_file_id:
            # 同一路径被新的文件 ID 替换：旧记录不再代表该文件
            self._unindex(target.rel_path, old, by_file, by_path)
            records.pop(target.rel_path, None)
            old = None
        record = self._ensure_target(target, old, outcome)
        if record is not None:
            records[target.rel_path] = record
            self._index_record(target.rel_path, record, by_file, by_path)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _ensure_target(
        self,
        target: StrmTarget,
        record: Optional[StrmRecordData],
        outcome: SyncOutcome,
        *,
        verify: bool = False,
    ) -> Optional[StrmRecordData]:
        """确保单个目标存在且内容正确，返回最新记录。"""
        path = self._absolute(target.rel_path)
        if not self._inside(path):
            outcome.errors.append(f"{target.rel_path}: 路径越界，跳过")
            return record
        if (
            not verify
            and record is not None
            and record.url == target.url
            and path.exists()
        ):
            outcome.skipped += 1
            return record
        current: Optional[str] = None
        if path.exists():
            try:
                current = path.read_text(encoding="utf-8", errors="replace")
            except OSError as err:
                outcome.errors.append(f"{target.rel_path}: 读取失败 {err}")
                return record
        new_record = StrmRecordData(
            rel_path=target.rel_path,
            source_file_id=target.source_file_id,
            source_path=target.source_path,
            url=target.url,
            size=target.size,
            modified_time=target.modified_time,
        )
        if current is not None and current.strip() == target.url:
            if record is None:
                outcome.added.append(new_record)
            elif (
                record.url == target.url
                and record.source_file_id == target.source_file_id
            ):
                outcome.skipped += 1
            else:
                outcome.updated.append(new_record)
            return new_record
        try:
            self._write(target.rel_path, target.url)
        except OSError as err:
            outcome.errors.append(f"{target.rel_path}: 写入失败 {err}")
            return record
        if record is None:
            outcome.added.append(new_record)
        else:
            outcome.updated.append(new_record)
        return new_record

    def _build_indexes(
        self, records: Dict[str, StrmRecordData]
    ) -> tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """按文件 ID 与源路径建立记录索引。"""
        by_file: Dict[str, List[str]] = {}
        by_path: Dict[str, List[str]] = {}
        for relative, record in records.items():
            by_file.setdefault(record.source_file_id, []).append(relative)
            by_path.setdefault(record.source_path, []).append(relative)
        return by_file, by_path

    @staticmethod
    def _index_record(
        relative: str,
        record: StrmRecordData,
        by_file: Dict[str, List[str]],
        by_path: Dict[str, List[str]],
    ) -> None:
        """把记录加入索引。"""
        files = by_file.setdefault(record.source_file_id, [])
        if relative not in files:
            files.append(relative)
        paths = by_path.setdefault(record.source_path, [])
        if relative not in paths:
            paths.append(relative)

    @staticmethod
    def _unindex(
        relative: str,
        record: Optional[StrmRecordData],
        by_file: Dict[str, List[str]],
        by_path: Dict[str, List[str]],
    ) -> None:
        """把记录移出索引。"""
        if record is None:
            return
        for index, key in (
            (by_file, record.source_file_id),
            (by_path, record.source_path),
        ):
            items = index.get(key)
            if not items:
                continue
            remaining = [item for item in items if item != relative]
            if remaining:
                index[key] = remaining
            else:
                index.pop(key, None)

    def _remove_matches(
        self,
        node: dict,
        records: Dict[str, StrmRecordData],
        by_file: Dict[str, List[str]],
        by_path: Dict[str, List[str]],
        outcome: SyncOutcome,
        *,
        only_cleanup: bool = False,
    ) -> None:
        """按文件 ID 删除匹配记录；无 ID 时只按路径匹配无 ID 记录。"""
        file_id = str(node.get("file_id") or "")
        source_path = str(node.get("path") or "")
        matches: set = set()
        if file_id:
            for relative in by_file.get(file_id, []):
                record = records.get(relative)
                if record is None:
                    continue
                if record.source_file_id and record.source_file_id != file_id:
                    continue
                matches.add(relative)
        elif source_path:
            for relative in by_path.get(source_path, []):
                record = records.get(relative)
                if record is None or record.source_file_id:
                    continue
                matches.add(relative)
        for relative in list(matches):
            if not self.cleanup:
                if only_cleanup:
                    outcome.skipped += 1
                continue
            record = records.get(relative)
            if not self._remove_record(relative, outcome):
                continue
            records.pop(relative, None)
            self._unindex(relative, record, by_file, by_path)

    def _remove_record(self, relative: str, outcome: SyncOutcome) -> bool:
        """删除单个 STRM 文件与记录；成功返回 True。"""
        path = self._absolute(relative)
        if not self._inside(path):
            outcome.errors.append(f"{relative}: 路径越界，跳过删除")
            return False
        try:
            if path.exists():
                if path.suffix.lower() != ".strm":
                    outcome.errors.append(f"{relative}: 非 STRM 文件，跳过删除")
                    return False
                path.unlink()
        except OSError as err:
            outcome.errors.append(f"{relative}: 删除失败 {err}")
            return False
        outcome.removed.append(relative)
        self._prune(relative)
        return True

    def _write(self, relative: str, content: str) -> None:
        """原子写入 STRM 内容。"""
        path = self._absolute(relative)
        if not self._inside(path):
            raise OSError("目标路径越界")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex}"
        try:
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _prune(self, relative: str) -> None:
        """自底向上删除空目录，不触碰输出根及其外部。"""
        parent = PurePosixPath(relative).parent
        while str(parent) not in (".", "/", ""):
            if any(part in ("..", "/", "\\") for part in parent.parts):
                return
            candidate = self.root.joinpath(*parent.parts)
            if not self._inside(candidate):
                return
            try:
                candidate.rmdir()
            except OSError:
                return
            parent = parent.parent

    def _absolute(self, relative: str) -> Path:
        """把 POSIX 相对路径转换为绝对路径。"""
        return self.root.joinpath(*PurePosixPath(relative).parts)

    def _inside(self, path: Path) -> bool:
        """判断路径是否位于输出根内。"""
        try:
            root = self.root.resolve()
            return path.resolve().is_relative_to(root)
        except (OSError, ValueError):
            return False

    def _stopped(self, outcome: SyncOutcome) -> bool:
        """检查停止信号。"""
        if self.stop_event is not None and self.stop_event.is_set():
            outcome.cancelled = True
            return True
        return False


def _is_file_node(node: Any) -> bool:
    """判断事件节点是否为文件。"""
    return isinstance(node, dict) and not bool(node.get("is_dir"))


def _tree_file(node: dict) -> TreeFile:
    """把事件节点还原为 TreeFile。"""
    return TreeFile(
        file_id=str(node.get("file_id") or ""),
        name=str(node.get("name") or ""),
        path=str(node.get("path") or ""),
        size=int(node.get("size") or 0),
        modified_time=float(node.get("modified_time") or 0.0),
        shortcut_target_id=str(node.get("shortcut_target_id") or ""),
    )


def relative_source_path(tree_path: str, root_path: str) -> Optional[str]:
    """公开的路径裁剪助手，供日志与测试使用。"""
    return strip_root_path(tree_path, root_path)
