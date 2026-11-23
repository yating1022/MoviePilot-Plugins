"""只读访问 MoviePilot 整理历史（归档记录）。

宿主没有在 ``app.sdk`` 暴露整理历史读取接口，这里集中封装内部导入，
便于失败时降级为「仅事件驱动」，避免污染插件其它模块。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Set

from .errors import GDriveTreeError


@dataclass(slots=True)
class ArchiveRecord:
    """一条归档记录（只保留目录树需要的字段）。"""

    history_id: int
    dest: str
    dest_storage: str
    dest_fileitem: Dict[str, Any] = field(default_factory=dict)
    files: List[str] = field(default_factory=list)


class ArchiveSource:
    """分页读取目标存储属于 Google Drive 的整理历史。"""

    def __init__(self, storage_names: Set[str]) -> None:
        """
        保存目标存储标识集合。

        :param storage_names: 例如 {"gdrive", "Google Drive"}
        """
        self.storage_names = {str(name).strip() for name in storage_names if str(name).strip()}

    def iter_records(
        self,
        *,
        page_size: int = 200,
        max_pages: int = 200,
    ) -> Iterator[ArchiveRecord]:
        """
        按页产出成功且目标存储匹配的归档记录。

        :param page_size: 每页记录数（宿主上限 200）
        :param max_pages: 最大扫描页数保护
        :return: ArchiveRecord 迭代器
        :raises GDriveTreeError: 宿主未提供整理历史接口
        """
        try:
            from app.db.oper.transferhistory import TransferHistoryOper
            from app.schemas.query import QueryPageRequest, TransferHistoryFilter
        except ImportError as err:
            raise GDriveTreeError(
                "当前宿主未提供整理历史读取接口，自动归档增量暂不可用"
            ) from err
        try:
            oper = TransferHistoryOper()
        except Exception as err:
            raise GDriveTreeError(f"初始化整理历史读取失败：{err}") from err

        page = 1
        while page <= max(1, int(max_pages or 1)):
            try:
                records, total = oper.query(
                    TransferHistoryFilter(),
                    QueryPageRequest(page=page, count=max(1, min(200, int(page_size or 200)))),
                )
            except Exception as err:
                raise GDriveTreeError(f"读取整理历史失败：{err}") from err
            if not records:
                return
            for record in records:
                # status=None 的旧记录按成功处理；明确失败的不入树
                if getattr(record, "status", True) is False:
                    continue
                dest_storage = str(getattr(record, "dest_storage", "") or "").strip()
                if dest_storage not in self.storage_names:
                    continue
                dest_fileitem = getattr(record, "dest_fileitem", None)
                if not isinstance(dest_fileitem, dict):
                    dest_fileitem = {}
                raw_files = getattr(record, "files", None)
                files = (
                    [str(item) for item in raw_files if item]
                    if isinstance(raw_files, list)
                    else []
                )
                yield ArchiveRecord(
                    history_id=int(getattr(record, "id", 0) or 0),
                    dest=str(getattr(record, "dest", "") or ""),
                    dest_storage=dest_storage,
                    dest_fileitem=dest_fileitem,
                    files=files,
                )
            if page * page_size >= int(total or 0):
                return
            page += 1

    @staticmethod
    def target_file_id(record: ArchiveRecord) -> str:
        """从归档记录的目标文件项中提取 fileid（兼容不同字段拼写）。"""
        for key in ("fileid", "file_id", "id"):
            value = record.dest_fileitem.get(key)
            if value:
                return str(value)
        return ""
