"""Google Drive 目录树插件（GDriveTree）。

复用 GDriveStorage 的 OAuth 配置，通过 ``files.list`` 一次性分页拉取文件元数据，
在本地按根目录裁剪建树，并持久化到插件自有数据库。
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple, Type

from fastapi import Body
from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from app.schemas.types import EventType
from app.sdk.database import plugin_declarative_base
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .archive_source import ArchiveRecord, ArchiveSource
from .errors import GDriveTreeAuthError, GDriveTreeCancelled, GDriveTreeError
from .gdrive_tree_client import FILE_FIELDS, GDriveTreeClient, RootInfo
from .tree_builder import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    TreeStats,
    TreeNodeData,
    _is_native_doc,
    _parse_size,
    _parse_time,
    build_tree,
    normalize_root_path,
)
from .tree_events import (
    TreeChangeSet,
    build_payload,
    change_from_nodes,
    full_signal,
)

DEFAULT_SOURCE_PLUGIN = "GDriveStorage"
DEFAULT_STRM_PLUGIN = "GDriveStrm"
DEFAULT_ROOT_PATH = "/"
DEFAULT_INTERVAL_HOURS = 24
DEFAULT_MAX_PAGES = 2000
DEFAULT_MAX_NODES = 200_000
DEFAULT_PAGE_SIZE = 20
DEFAULT_ARCHIVE_INTERVAL_MINUTES = 5
ARCHIVE_MAX_RETRIES = 3
ARCHIVE_SCAN_LIMIT = 50

_PluginDBBase = plugin_declarative_base()


class TreeMeta(_PluginDBBase):
    """目录树刷新状态与统计（固定单行，id=1）。"""

    __tablename__ = "tree_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_plugin_id: Mapped[str] = mapped_column(String(128), default="")
    root_file_id: Mapped[str] = mapped_column(String(128), default="")
    root_name: Mapped[str] = mapped_column(String(512), default="")
    root_path: Mapped[str] = mapped_column(String(1024), default="/")
    corpora: Mapped[str] = mapped_column(String(16), default="user")
    drive_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), index=True, default="idle")
    last_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    dir_count: Mapped[int] = mapped_column(Integer, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_size: Mapped[int] = mapped_column(BigInteger, default=0)
    max_depth: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ArchiveLink(_PluginDBBase):
    """归档记录与目录树的增量处理状态。"""

    __tablename__ = "archive_link"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    history_id: Mapped[int] = mapped_column(Integer, index=True, default=0)
    dest_path: Mapped[str] = mapped_column(String(2048), default="")
    dest_storage: Mapped[str] = mapped_column(String(128), default="")
    dest_file_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    node_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class TreeNode(_PluginDBBase):
    """目录树节点快照。"""

    __tablename__ = "tree_node"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    parent_file_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    name: Mapped[str] = mapped_column(String(512), index=True, default="")
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    is_dir: Mapped[bool] = mapped_column(Boolean, index=True, default=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    modified_time: Mapped[float] = mapped_column(Float, default=0.0)
    created_time: Mapped[float] = mapped_column(Float, default=0.0)
    path: Mapped[str] = mapped_column(String(2048), index=True, default="")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    md5: Mapped[str] = mapped_column(String(64), default="")
    trashed: Mapped[bool] = mapped_column(Boolean, default=False)
    shortcut_target_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


def _extract_drive_id(value: Any) -> str:
    """
    从文件 ID、文件夹分享链接或共享盘 URL 中提取 Drive ID。

    :param value: 原始配置值
    :return: 纯 ID；无法识别时返回去空白后的原值
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    patterns = (
        r"/folders/([A-Za-z0-9_-]+)",
        r"/drive/folders/([A-Za-z0-9_-]+)",
        r"/file/d/([A-Za-z0-9_-]+)",
        r"[?&]id=([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return text


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """把配置值限制在闭区间内。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _join_child_path(parent_path: str, name: str, is_dir: bool) -> str:
    """在树中父路径下拼接子节点路径；目录保留结尾斜杠。"""
    base = parent_path or "/"
    child = f"{base.rstrip('/')}/{name}" if base != "/" else f"/{name}"
    if is_dir and not child.endswith("/"):
        child += "/"
    return child


def _format_size(size: Any) -> str:
    """把字节数格式化为易读文本。"""
    value = float(max(0, int(size or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _format_timestamp(value: Any) -> str:
    """把 Unix 秒格式化为本地时间文本。"""
    try:
        number = float(value or 0)
        if number <= 0:
            return "-"
        return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _format_datetime(value: Any) -> str:
    """格式化 datetime/None。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return "-"


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符。"""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GDriveTree(_PluginBase):
    """Google Drive 目录树插件。"""

    plugin_name = "Google Drive 目录树"
    plugin_desc = (
        "复用 Google Drive 存储插件的授权配置，一次性拉取文件元数据并在本地建树，"
        "把指定根目录的目录树持久化到插件自有数据库，支持手动和定时刷新。"
    )
    plugin_version = "1.2.4"
    plugin_label = "网盘"
    plugin_author = "yating1022"
    plugin_config_prefix = "gdrivetree_"
    plugin_order = 101
    auth_level = 1

    def __init__(self) -> None:
        """初始化运行时状态；不做网络或数据库操作。"""
        super().__init__()
        self._config: Dict[str, Any] = {}
        self._enabled = False
        self._source_plugin_id = DEFAULT_SOURCE_PLUGIN
        self._auto_refresh = False
        self._interval_hours = DEFAULT_INTERVAL_HOURS
        self._include_trashed = False
        self._max_pages = DEFAULT_MAX_PAGES
        self._max_nodes = DEFAULT_MAX_NODES
        self._page_size = DEFAULT_PAGE_SIZE
        self._incremental_write = True
        self._archive_watch = True
        self._archive_interval_minutes = DEFAULT_ARCHIVE_INTERVAL_MINUTES
        self._strm_notify = False
        self._strm_plugin_id = DEFAULT_STRM_PLUGIN
        self._page = 1
        self._stop_event = threading.Event()
        self._refresh_lock = threading.Lock()
        self._archive_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._archive_thread: Optional[threading.Thread] = None
        self._client: Optional[GDriveTreeClient] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重置运行时状态，可重复调用。"""
        self.stop_service()
        cfg = self._normalize_config(config or {})
        self._config = cfg
        self._enabled = bool(cfg["enabled"])
        self._source_plugin_id = cfg["source_plugin_id"]
        self._auto_refresh = bool(cfg["auto_refresh"])
        self._interval_hours = cfg["refresh_interval_hours"]
        self._include_trashed = bool(cfg["include_trashed"])
        self._max_pages = cfg["max_pages"]
        self._max_nodes = cfg["max_nodes"]
        self._page_size = cfg["page_size"]
        self._incremental_write = bool(cfg["incremental_write"])
        self._archive_watch = bool(cfg["archive_watch"])
        self._archive_interval_minutes = cfg["archive_poll_interval_minutes"]
        self._strm_notify = bool(cfg["strm_notify"])
        self._strm_plugin_id = cfg["strm_plugin_id"]
        self._page = 1
        self._thread = None
        self._archive_thread = None
        if self._enabled:
            logger.info(
                f"【GDriveTree】插件已启用 v{self.plugin_version}："
                f"源插件={self._source_plugin_id}，自动刷新="
                f"{self._interval_hours}h，包含回收站={self._include_trashed}"
            )
            logger.info(
                f"【GDriveTree】事件通知："
                f"{'开启' if self._strm_notify else '关闭'}"
                f"（目标={self._strm_plugin_id}）"
            )
        else:
            logger.debug("【GDriveTree】插件未启用")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def stop_service(self) -> None:
        """中止刷新线程并等待其退出，可重复调用。"""
        self._stop_event.set()
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("【GDriveTree】关闭客户端失败", exc_info=True)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning("【GDriveTree】刷新线程未在 5 秒内退出")
        self._thread = None

    def get_database_models(self) -> List[Type[Any]]:
        """声明插件自有表。"""
        return [TreeMeta, TreeNode, ArchiveLink]

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把用户配置归一化为稳定类型和默认值。"""
        source_plugin_id = str(
            cfg.get("source_plugin_id") or DEFAULT_SOURCE_PLUGIN
        ).strip() or DEFAULT_SOURCE_PLUGIN
        return {
            "enabled": bool(cfg.get("enabled")),
            "source_plugin_id": source_plugin_id,
            "auto_refresh": bool(cfg.get("auto_refresh")),
            "refresh_interval_hours": _clamp_int(
                cfg.get("refresh_interval_hours"), 1, 168, DEFAULT_INTERVAL_HOURS
            ),
            "include_trashed": bool(cfg.get("include_trashed")),
            "max_pages": _clamp_int(cfg.get("max_pages"), 1, 20000, DEFAULT_MAX_PAGES),
            "max_nodes": _clamp_int(
                cfg.get("max_nodes"), 1, 1_000_000, DEFAULT_MAX_NODES
            ),
            "page_size": _clamp_int(cfg.get("page_size"), 5, 100, DEFAULT_PAGE_SIZE),
            "incremental_write": bool(cfg.get("incremental_write", True)),
            "archive_watch": bool(cfg.get("archive_watch", True)),
            "archive_poll_interval_minutes": _clamp_int(
                cfg.get("archive_poll_interval_minutes"),
                1,
                1440,
                DEFAULT_ARCHIVE_INTERVAL_MINUTES,
            ),
            "strm_notify": bool(cfg.get("strm_notify")),
            "strm_plugin_id": str(
                cfg.get("strm_plugin_id") or DEFAULT_STRM_PLUGIN
            ).strip() or DEFAULT_STRM_PLUGIN,
        }

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回配置表单和默认值。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "class": "mb-3",
                            "text": (
                                "本插件不单独配置授权，直接复用 GDriveStorage 的 "
                                "client_id/client_secret/refresh_token。"
                                "请先启用并完成 GDriveStorage 授权。"
                                "抓取方式为一次全量拉取 + 本地按根目录裁剪。"
                            ),
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "auto_refresh", "label": "定时刷新"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "refresh_interval_hours",
                                        "label": "刷新间隔（小时）",
                                        "type": "number",
                                        "min": 1,
                                        "max": 168,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "source_plugin_id",
                                        "label": "源插件 ID",
                                        "placeholder": DEFAULT_SOURCE_PLUGIN,
                                        "hint": "默认 GDriveStorage；使用插件分身时填写分身 ID",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "include_trashed",
                                        "label": "包含回收站",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "page_size",
                                        "label": "每页节点数",
                                        "type": "number",
                                        "min": 5,
                                        "max": 100,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_pages",
                                        "label": "最大拉取页数",
                                        "type": "number",
                                        "min": 1,
                                        "max": 20000,
                                        "hint": "每页最多 1000 条；默认 2000 页",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_nodes",
                                        "label": "最大入库节点数",
                                        "type": "number",
                                        "min": 1,
                                        "max": 1000000,
                                        "hint": "超出后中止刷新并保留旧数据",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "incremental_write",
                                        "label": "全量刷新时增量写库",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "archive_watch",
                                        "label": "监控归档记录增量入树",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "archive_poll_interval_minutes",
                                        "label": "归档轮询间隔（分钟）",
                                        "type": "number",
                                        "min": 1,
                                        "max": 1440,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "strm_notify",
                                        "label": "通知 STRM 插件",
                                        "hint": "目录树变化后发送 tree.changed 事件",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "strm_plugin_id",
                                        "label": "STRM 插件 ID",
                                        "placeholder": DEFAULT_STRM_PLUGIN,
                                        "hint": "默认 GDriveStrm；使用分身时填分身 ID",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "source_plugin_id": DEFAULT_SOURCE_PLUGIN,
            "auto_refresh": False,
            "refresh_interval_hours": DEFAULT_INTERVAL_HOURS,
            "include_trashed": False,
            "max_pages": DEFAULT_MAX_PAGES,
            "max_nodes": DEFAULT_MAX_NODES,
            "page_size": DEFAULT_PAGE_SIZE,
            "incremental_write": True,
            "archive_watch": True,
            "archive_poll_interval_minutes": DEFAULT_ARCHIVE_INTERVAL_MINUTES,
            "strm_notify": False,
            "strm_plugin_id": DEFAULT_STRM_PLUGIN,
        }

    # ------------------------------------------------------------------
    # 定时服务
    # ------------------------------------------------------------------
    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """按开关注册全量刷新和归档轮询服务。"""
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = []
        if self._auto_refresh:
            services.append({
                "id": "GDriveTree",
                "name": "Google Drive 目录树（定时刷新）",
                "trigger": "interval",
                "func": self.refresh_from_scheduler,
                "kwargs": {"seconds": max(3600, self._interval_hours * 3600)},
            })
        if self._archive_watch:
            services.append({
                "id": "GDriveTreeArchive",
                "name": "Google Drive 目录树（归档增量）",
                "trigger": "interval",
                "func": self.scan_archive_from_scheduler,
                "kwargs": {
                    "seconds": max(60, self._archive_interval_minutes * 60)
                },
            })
        return services

    def refresh_from_scheduler(self) -> None:
        """调度器入口：只启动后台刷新，立即返回。"""
        if not self._enabled:
            return
        try:
            if not self.start_refresh():
                logger.info("【GDriveTree】定时刷新跳过：已有刷新任务在运行")
        except Exception:
            logger.exception("【GDriveTree】启动定时刷新失败")

    # ------------------------------------------------------------------
    # 刷新编排
    # ------------------------------------------------------------------
    def start_refresh(self) -> bool:
        """
        启动后台刷新线程。

        :return: True 已启动；False 已有刷新任务
        """
        if not self._enabled:
            return False
        if not self._refresh_lock.acquire(blocking=False):
            return False
        event = threading.Event()
        try:
            self._stop_event = event
            # 先同步写 running，避免页面在后台线程启动前读到旧状态
            self._write_meta(
                status="running",
                source_plugin_id=self._source_plugin_id,
                last_started_at=datetime.now(),
                last_finished_at=None,
                error="",
            )
            thread = threading.Thread(
                target=self._run_refresh,
                args=(event, self._refresh_lock),
                name="GDriveTree-refresh",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return True
        except Exception:
            try:
                self._refresh_lock.release()
            except RuntimeError:
                pass
            raise

    def _run_refresh(self, stop_event: threading.Event, lock: threading.Lock) -> None:
        """刷新线程主体；成功替换节点，失败保留旧数据。"""
        started = time.monotonic()
        try:
            if stop_event.is_set():
                raise GDriveTreeCancelled("刷新已中止")
            source = self._read_source_config()
            logger.info(
                f"【GDriveTree】开始刷新：源插件={self._source_plugin_id}，"
                f"根={source['root_folder_id']}"
            )
            client = GDriveTreeClient(
                client_id=source["client_id"],
                client_secret=source["client_secret"],
                refresh_token=source["refresh_token"],
            )
            self._client = client
            try:
                root = self._resolve_root(client, source)
                logger.info(
                    f"【GDriveTree】根解析：id={root.file_id} name={root.name} "
                    f"corpora={root.corpora} drive={root.drive_id or '-'}"
                )
                files, pages = client.list_all(
                    corpora=root.corpora,
                    drive_id=root.drive_id,
                    include_trashed=self._include_trashed,
                    max_pages=self._max_pages,
                    max_files=self._max_nodes,
                    stop_event=stop_event,
                )
            finally:
                client.close()
                if self._client is client:
                    self._client = None
            if stop_event.is_set():
                raise GDriveTreeCancelled("刷新已中止")
            nodes, stats = build_tree(
                files,
                root_id=root.file_id,
                root_name=root.name,
                root_path=source["root_path"],
                max_nodes=self._max_nodes,
                skip_trashed=not self._include_trashed,
            )
            if stop_event.is_set():
                raise GDriveTreeCancelled("刷新已中止")
            duration = time.monotonic() - started
            self._save_snapshot(nodes, stats, root, source, pages, duration)
            logger.info(
                f"【GDriveTree】刷新完成：节点={stats.node_count}，目录="
                f"{stats.dir_count}，文件={stats.file_count}，拉取元数据="
                f"{len(files)}，页数={pages}，耗时={duration:.1f}s"
            )
        except GDriveTreeCancelled as err:
            logger.info(f"【GDriveTree】刷新已中止：{err}")
            self._safe_failed("cancelled", str(err))
        except GDriveTreeAuthError as err:
            logger.warning(f"【GDriveTree】授权失败：{err}")
            self._safe_failed("failed", str(err))
        except GDriveTreeError as err:
            logger.warning(f"【GDriveTree】刷新失败：{err}")
            self._safe_failed("failed", str(err))
        except Exception as err:  # pragma: no cover - 兜底
            logger.exception("【GDriveTree】刷新出现未预期错误")
            if stop_event.is_set():
                self._safe_failed("cancelled", "刷新已中止")
            else:
                self._safe_failed("failed", f"未预期错误：{err}")
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    def _read_source_config(self) -> Dict[str, str]:
        """读取并校验 GDriveStorage 配置。"""
        raw = self.get_config(self._source_plugin_id)
        if not isinstance(raw, dict):
            raise GDriveTreeAuthError(
                f"未找到插件 {self._source_plugin_id} 的配置；"
                "请先启用并完成 GDriveStorage 授权"
            )
        missing = [
            label
            for key, label in (
                ("client_id", "client_id"),
                ("client_secret", "client_secret"),
                ("refresh_token", "refresh_token"),
            )
            if not str(raw.get(key) or "").strip()
        ]
        if missing:
            raise GDriveTreeAuthError(
                f"插件 {self._source_plugin_id} 缺少 {'、'.join(missing)}；"
                "请先在 GDriveStorage 完成授权"
            )
        return {
            "client_id": str(raw.get("client_id") or "").strip(),
            "client_secret": str(raw.get("client_secret") or "").strip(),
            "refresh_token": str(raw.get("refresh_token") or "").strip(),
            "root_folder_id": _extract_drive_id(raw.get("root_folder_id")) or "root",
            "root_path": str(raw.get("root_path") or DEFAULT_ROOT_PATH).strip() or DEFAULT_ROOT_PATH,
            "use_shared_drive": str(raw.get("use_shared_drive") or ""),
            "shared_drive_id": _extract_drive_id(raw.get("shared_drive_id")),
        }

    def _resolve_root(self, client: GDriveTreeClient, source: Dict[str, str]) -> RootInfo:
        """按 GDriveStorage 配置解析真实根；共享盘根不可用时回退账号根。"""
        root_folder_id = source["root_folder_id"]
        fallback_name = PurePosixPath(
            normalize_root_path(source["root_path"])
        ).name or "/"
        use_shared = source.get("use_shared_drive") in {"True", "true", "1"}
        shared_drive_id = source.get("shared_drive_id") or ""
        if use_shared and shared_drive_id and root_folder_id == "root":
            # 共享盘根不可用时直接失败，避免把账号根误标成共享盘目录树
            return client.resolve_root(shared_drive_id, fallback_name=fallback_name)
        return client.resolve_root(root_folder_id, fallback_name=fallback_name)

    # ------------------------------------------------------------------
    # 归档增量
    # ------------------------------------------------------------------
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """监听整理完成事件，实时把 Google Drive 目标加入目录树。"""
        if not self._enabled or not self._archive_watch:
            return
        try:
            data = event.event_data
            transferinfo = getattr(data, "transferinfo", None)
            target = getattr(transferinfo, "target_item", None) if transferinfo else None
            if target is None and transferinfo is not None:
                target = getattr(transferinfo, "target_diritem", None)
            if target is None:
                return
            storage = str(getattr(target, "storage", "") or "")
            if storage not in self._gdrive_storage_names():
                return
            file_id = str(
                getattr(target, "fileid", "") or getattr(target, "file_id", "") or ""
            )
            path = str(getattr(target, "path", "") or "")
            history_id = int(getattr(data, "transfer_history_id", 0) or 0)
        except Exception:
            logger.debug("【GDriveTree】解析整理完成事件失败", exc_info=True)
            return
        if not file_id and not path:
            return
        threading.Thread(
            target=self._handle_event_target,
            args=(file_id, path, history_id, storage),
            name="GDriveTree-archive-event",
            daemon=True,
        ).start()

    def _gdrive_storage_names(self) -> set:
        """返回目标存储可能使用的标识（类型与显示名）。"""
        raw = self.get_config(self._source_plugin_id)
        names = {"gdrive"}
        if isinstance(raw, dict):
            display = str(raw.get("display_name") or "").strip()
            if display:
                names.add(display)
        return names

    def _handle_event_target(
        self,
        file_id: str,
        path: str,
        history_id: int,
        storage: str = "",
    ) -> None:
        """处理单条整理完成事件。"""
        if not self._archive_lock.acquire(blocking=False):
            logger.info("【GDriveTree】归档处理繁忙，事件交由定时轮询补偿")
            return
        client: Optional[GDriveTreeClient] = None
        try:
            source = self._read_source_config()
            client = GDriveTreeClient(
                client_id=source["client_id"],
                client_secret=source["client_secret"],
                refresh_token=source["refresh_token"],
            )
            root = self._resolve_root(client, source)
            nodes = self._resolve_archive_nodes(
                client, root, source, file_id=file_id, path=path
            )
            self._upsert_nodes(nodes)
            self._link_update(
                history_id, path, storage, file_id, "done", len(nodes), ""
            )
            self._recount_meta()
            logger.info(
                f"【GDriveTree】归档事件已入树：{path or file_id}，节点={len(nodes)}"
            )
        except Exception as err:
            logger.warning(f"【GDriveTree】归档事件处理失败：{path or file_id} - {err}")
            self._link_update(history_id, path, storage, file_id, "failed", 0, str(err))
        finally:
            if client is not None:
                client.close()
            self._archive_lock.release()

    def scan_archive_from_scheduler(self) -> None:
        """调度器入口：只启动后台扫描，立即返回。"""
        if not self._enabled or not self._archive_watch:
            return
        try:
            if not self.start_archive_scan():
                logger.info("【GDriveTree】归档扫描跳过：已有任务在运行")
        except Exception:
            logger.exception("【GDriveTree】启动归档扫描失败")

    def start_archive_scan(self) -> bool:
        """启动后台归档扫描。"""
        if not self._enabled or not self._archive_watch:
            return False
        if self._archive_lock.locked():
            return False
        thread = threading.Thread(
            target=self.scan_archive,
            name="GDriveTree-archive",
            daemon=True,
        )
        self._archive_thread = thread
        thread.start()
        return True

    def scan_archive(self) -> Dict[str, Any]:
        """对比宿主归档记录与 archive_link，把缺失的目标增量加入目录树。"""
        result: Dict[str, Any] = {
            "scanned": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "busy": False,
        }
        if not self._enabled or not self._archive_watch:
            return result
        if not self._archive_lock.acquire(blocking=False):
            result["busy"] = True
            return result
        client: Optional[GDriveTreeClient] = None
        try:
            source = self._read_source_config()
            client = GDriveTreeClient(
                client_id=source["client_id"],
                client_secret=source["client_secret"],
                refresh_token=source["refresh_token"],
            )
            root = self._resolve_root(client, source)
            done, retries = self._archive_link_state()
            archive = ArchiveSource(self._gdrive_storage_names())
            for record in archive.iter_records(page_size=200, max_pages=200):
                result["scanned"] += 1
                if record.history_id and record.history_id in done:
                    result["skipped"] += 1
                    continue
                if record.history_id and retries.get(record.history_id, 0) >= ARCHIVE_MAX_RETRIES:
                    result["skipped"] += 1
                    continue
                if result["processed"] >= ARCHIVE_SCAN_LIMIT:
                    break
                try:
                    nodes = self._nodes_for_archive_record(client, root, source, record)
                    self._upsert_nodes(nodes)
                    self._link_update(
                        record.history_id,
                        record.dest,
                        record.dest_storage,
                        ArchiveSource.target_file_id(record),
                        "done",
                        len(nodes),
                        "",
                    )
                    result["processed"] += 1
                except Exception as err:
                    result["failed"] += 1
                    logger.warning(
                        f"【GDriveTree】归档记录 {record.history_id} 入树失败：{err}"
                    )
                    self._link_update(
                        record.history_id,
                        record.dest,
                        record.dest_storage,
                        ArchiveSource.target_file_id(record),
                        "failed",
                        0,
                        str(err),
                    )
            if result["processed"]:
                self._recount_meta()
            self._save_archive_status(result)
            if result["processed"] or result["failed"]:
                logger.info(f"【GDriveTree】归档扫描完成：{result}")
            return result
        except Exception as err:
            logger.warning(f"【GDriveTree】归档扫描失败：{err}")
            result["error"] = str(err)
            self._save_archive_status(result)
            return result
        finally:
            if client is not None:
                client.close()
            self._archive_lock.release()

    def _nodes_for_archive_record(
        self,
        client: GDriveTreeClient,
        root: RootInfo,
        source: Dict[str, str],
        record: ArchiveRecord,
    ) -> List[TreeNodeData]:
        """把一条归档记录转换为待写入的节点（可能多个文件）。"""
        target_id = ArchiveSource.target_file_id(record)
        errors: List[str] = []
        if target_id:
            try:
                return self._nodes_for_target(
                    client, root, source, file_id=target_id
                )
            except GDriveTreeError as err:
                errors.append(f"按文件 ID {target_id} 解析失败：{err}")
        raw_paths = [str(path) for path in record.files if str(path).strip()]
        if not raw_paths:
            item = record.dest_fileitem or {}
            item_path = str(item.get("path") or "").strip()
            item_name = str(item.get("name") or item.get("basename") or "").strip()
            if item_path:
                raw_paths = [item_path]
            elif item_name and record.dest:
                raw_paths = [f"{record.dest.rstrip('/')}/{item_name}"]
            elif record.dest:
                raw_paths = [record.dest]
        merged: Dict[str, TreeNodeData] = {}
        resolved = False
        for raw_path in raw_paths:
            path = self._normalize_archive_path(str(raw_path), record.dest)
            if not path:
                continue
            try:
                nodes = self._ensure_nodes_for_path(client, path)
            except GDriveTreeError as err:
                errors.append(f"按路径 {path} 解析失败：{err}")
                continue
            resolved = True
            for node in nodes:
                merged[node.file_id] = node
        if not resolved:
            raise GDriveTreeError(
                "；".join(errors[:4]) or "归档记录缺少目标文件信息"
            )
        return sorted(merged.values(), key=lambda node: (node.depth, node.path))

    @staticmethod
    def _normalize_archive_path(raw_path: str, dest: str) -> str:
        """把归档记录中的路径统一成以 / 开头的绝对路径。"""
        text = str(raw_path or "").strip().replace("\\", "/")
        if not text:
            return ""
        base = str(dest or "").strip().replace("\\", "/")
        if base:
            base = base.rstrip("/")
            if not text.startswith("/"):
                text = base + "/" + text.lstrip("/")
            elif not (text == base or text.startswith(base + "/")):
                # files 里可能是源路径；按文件名落到目标目录下
                text = base + "/" + PurePosixPath(text).name
        if not text.startswith("/"):
            text = "/" + text
        return text

    def _nodes_for_target(
        self,
        client: GDriveTreeClient,
        root: RootInfo,
        source: Dict[str, str],
        *,
        file_id: str = "",
        path: str = "",
    ) -> List[TreeNodeData]:
        """按 fileid 或虚拟路径获取目标元数据并构建父链节点。"""
        meta: Optional[Dict[str, Any]] = None
        if file_id:
            meta = client.get_file(file_id, FILE_FIELDS)
        elif path:
            pure = PurePosixPath(path.rstrip("/"))
            name = pure.name
            parent_path = normalize_root_path(str(pure.parent or "/"))
            parent = self._find_parent_node(path)
            if parent is None:
                raise GDriveTreeError(
                    f"父目录不在目录树中：{parent_path}；请先刷新目录树"
                )
            meta = client.find_child(
                parent.shortcut_target_id or parent.file_id, name
            )
            if meta is None:
                raise GDriveTreeError(f"云端未找到目标文件：{path}")
        if not meta:
            raise GDriveTreeError("缺少目标文件 ID 或路径")
        return self._nodes_from_metadata(client, meta, root, source)

    def _resolve_archive_nodes(
        self,
        client: GDriveTreeClient,
        root: RootInfo,
        source: Dict[str, str],
        *,
        file_id: str = "",
        path: str = "",
    ) -> List[TreeNodeData]:
        """优先按文件 ID 解析；失败后按目录树中的父路径定位目标。"""
        errors: List[str] = []
        if file_id:
            try:
                return self._nodes_for_target(
                    client, root, source, file_id=file_id
                )
            except GDriveTreeError as err:
                errors.append(f"按文件 ID {file_id} 解析失败：{err}")
        if path:
            try:
                return self._ensure_nodes_for_path(client, path)
            except GDriveTreeError as err:
                errors.append(f"按路径 {path} 解析失败：{err}")
        raise GDriveTreeError("；".join(errors) or "缺少目标文件信息")

    def _ensure_nodes_for_path(
        self, client: GDriveTreeClient, path: str
    ) -> List[TreeNodeData]:
        """
        按树路径逐段补齐缺失目录，并返回新建节点。

        从树根节点开始：路径段已存在于树中则直接下钻；缺失时用父节点的
        Drive ID 查询子项并新建。快捷方式目录使用其目标目录 ID 查询，
        因此真实父链不在树根下也能入树；遇到已存在节点即停止补建。
        """
        text = str(path or "").strip()
        if not text:
            raise GDriveTreeError("目标路径为空")
        meta = self._load_meta()
        root_path = normalize_root_path(meta.get("root_path") or "/")
        root_node = self._find_node_by_path(root_path)
        if root_node is None:
            raise GDriveTreeError("目录树根节点不存在，请先刷新目录树")
        segments = self._relative_path_segments(text, root_node, root_path)
        if not segments:
            raise GDriveTreeError(f"目标路径为空：{path}")
        anchor: Any = root_node
        nodes: List[TreeNodeData] = []
        for index, segment in enumerate(segments):
            is_last = index == len(segments) - 1
            existing = self._find_node_by_path(
                _join_child_path(anchor.path, segment, True)
            )
            if existing is None:
                existing = self._find_node_by_path(
                    _join_child_path(anchor.path, segment, False)
                )
            if existing is not None:
                if not is_last and not existing.is_dir:
                    raise GDriveTreeError(f"路径段不是目录：{existing.path}")
                anchor = existing
                continue
            lookup_id = (
                getattr(anchor, "shortcut_target_id", "") or anchor.file_id
            )
            child = client.find_child(lookup_id, segment)
            if child is None:
                raise GDriveTreeError(
                    f"云端未找到路径段：{anchor.path}{segment}"
                )
            node = self._node_from_child_meta(anchor, child, segment)
            if not is_last and not node.is_dir:
                raise GDriveTreeError(f"路径段不是目录：{node.path}")
            nodes.append(node)
            anchor = node
        return nodes

    def _relative_path_segments(
        self, path: str, root_node: TreeNode, root_path: str
    ) -> List[str]:
        """把归档路径转换为相对树根节点的路径段。"""
        segments = [part for part in PurePosixPath(path).parts if part != "/"]
        if not segments:
            return []
        candidates: List[List[str]] = []
        if path.startswith(root_path):
            tail = path[len(root_path):].strip("/")
            candidates.append([part for part in tail.split("/") if part])
        for start in range(1, len(segments)):
            candidate = "/" + "/".join(segments[start:])
            if not candidate.startswith(root_path):
                continue
            tail = candidate[len(root_path):].strip("/")
            candidates.append([part for part in tail.split("/") if part])
        for candidate in candidates:
            if not candidate:
                continue
            child = _join_child_path(root_node.path, candidate[0], True)
            if self._find_node_by_path(child) is not None:
                return candidate
        # 没有任何已知锚点（例如全新的顶层目录），从树根开始创建完整路径
        return segments

    def _node_from_child_meta(
        self, parent: Any, meta: Dict[str, Any], fallback_name: str
    ) -> TreeNodeData:
        """以已存在的树节点为父，构造一个子节点。"""
        target_id = str(meta.get("id") or "")
        if not target_id:
            raise GDriveTreeError(f"目标元数据缺少文件 ID：{fallback_name}")
        mime_type = str(meta.get("mimeType") or "")
        shortcut_target = ""
        shortcut_is_dir = False
        if mime_type == SHORTCUT_MIME:
            details = meta.get("shortcutDetails") or {}
            shortcut_target = str(details.get("targetId") or "")
            shortcut_is_dir = str(
                details.get("targetMimeType") or ""
            ) == FOLDER_MIME
        is_dir = mime_type == FOLDER_MIME or shortcut_is_dir
        size = (
            0
            if (is_dir or _is_native_doc(mime_type))
            else _parse_size(meta.get("size"))
        )
        name = str(meta.get("name") or fallback_name)
        return TreeNodeData(
            file_id=target_id,
            parent_file_id=parent.file_id,
            name=name,
            mime_type=mime_type,
            is_dir=is_dir,
            size=size,
            modified_time=_parse_time(meta.get("modifiedTime")),
            created_time=_parse_time(meta.get("createdTime")),
            path=_join_child_path(parent.path, name, is_dir),
            depth=int(parent.depth or 0) + 1,
            md5=str(meta.get("md5Checksum") or ""),
            trashed=bool(meta.get("trashed")),
            shortcut_target_id=shortcut_target,
        )

    def _nodes_from_metadata(
        self,
        client: GDriveTreeClient,
        meta: Dict[str, Any],
        root: RootInfo,
        source: Dict[str, str],
    ) -> List[TreeNodeData]:
        """拉取父链并在本地裁剪成从根到目标的节点列表。"""
        target_id = str(meta.get("id") or "")
        if not target_id:
            raise GDriveTreeError("目标元数据缺少 file id")
        chain: List[Dict[str, Any]] = [meta]
        current = meta
        seen = {target_id}
        for _ in range(64):
            parents = current.get("parents") or []
            parent_id = str(parents[0]) if parents else ""
            if not parent_id or parent_id == root.file_id:
                break
            if parent_id in seen:
                raise GDriveTreeError("目标文件的父目录链出现循环")
            seen.add(parent_id)
            current = client.get_file(parent_id, FILE_FIELDS)
            chain.append(current)
        else:
            raise GDriveTreeError("目标文件父目录层级过深")
        root_raw = {
            "id": root.file_id,
            "name": root.name or "/",
            "mimeType": FOLDER_MIME,
            "parents": [],
        }
        files = [root_raw] + list(reversed(chain))
        nodes, _ = build_tree(
            files,
            root_id=root.file_id,
            root_name=root.name,
            root_path=normalize_root_path(source["root_path"]),
            max_nodes=max(1000, self._max_nodes),
            skip_trashed=False,
        )
        if not any(node.file_id == target_id for node in nodes):
            raise GDriveTreeError("归档目标不在当前目录树根目录下")
        return nodes

    def _find_node_by_path(self, path: str) -> Optional[TreeNode]:
        """按目录路径查找已有节点。"""
        normalized = normalize_root_path(path)
        try:
            with self._session() as session:
                return session.scalar(
                    select(TreeNode).where(TreeNode.path == normalized).limit(1)
                )
        except SQLAlchemyError:
            logger.debug("【GDriveTree】按路径查询节点失败", exc_info=True)
            return None

    def _find_parent_node(self, path: str) -> Optional[TreeNode]:
        """
        从目标父路径由深到浅查找树中已存在的目录。

        归档路径可能带本地挂载前缀（如 ``/workspace``）而树路径没有，
        逐级去掉前导目录后仍能定位到真正的父目录。
        """
        pure = PurePosixPath(str(path or "").rstrip("/"))
        parts = [
            part for part in pure.parent.parts if part not in ("/", "")
        ]
        for start in range(0, len(parts) + 1):
            candidate = "/" + "/".join(parts[start:]) + "/"
            node = self._find_node_by_path(candidate)
            if node is not None:
                return node
        return None

    def _upsert_nodes(
        self, nodes: List[TreeNodeData]
    ) -> Tuple[List[TreeNodeData], List[TreeNodeData]]:
        """按 file_id 增量写入节点，不删除其它节点；提交后通知 STRM 插件。"""
        if not nodes:
            return [], []
        now = datetime.now()
        added: List[TreeNodeData] = []
        updated: List[TreeNodeData] = []
        with self._session() as session:
            ids = [node.file_id for node in nodes]
            existing = {
                row.file_id: row
                for row in session.scalars(
                    select(TreeNode).where(TreeNode.file_id.in_(ids))
                )
            }
            for node in nodes:
                values = self._node_values(node, now)
                row = existing.get(node.file_id)
                if row is None:
                    session.add(TreeNode(**values))
                    added.append(node)
                elif self._node_changed(row, node):
                    values.pop("created_at", None)
                    for key, value in values.items():
                        setattr(row, key, value)
                    updated.append(node)
            session.commit()
        if added or updated:
            self._publish_tree_change(
                change_from_nodes("archive", added=added, updated=updated)
            )
        return added, updated

    def _link_update(
        self,
        history_id: int,
        dest_path: str,
        dest_storage: str,
        dest_file_id: str,
        status: str,
        node_count: int,
        error: str,
    ) -> None:
        """写入归档记录处理状态；history_id 为空时不记录。"""
        try:
            history_id = int(history_id or 0)
        except (TypeError, ValueError):
            history_id = 0
        if history_id <= 0:
            return
        try:
            with self._session() as session:
                link = session.scalar(
                    select(ArchiveLink).where(ArchiveLink.history_id == history_id)
                )
                if link is None:
                    link = ArchiveLink(history_id=history_id)
                    session.add(link)
                link.dest_path = str(dest_path or "")[:2048]
                link.dest_storage = str(dest_storage or "")[:128]
                link.dest_file_id = str(dest_file_id or "")[:128]
                link.status = status
                if status == "failed":
                    link.retry_count = int(link.retry_count or 0) + 1
                link.node_count = int(node_count or 0)
                link.error = str(error or "")[:500]
                link.updated_at = datetime.now()
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveTree】写入归档处理状态失败")

    def _archive_link_state(self) -> Tuple[set, Dict[int, int]]:
        """返回已完成的历史 ID 集合与失败重试次数。"""
        done: set = set()
        retries: Dict[int, int] = {}
        try:
            with self._session() as session:
                rows = list(session.scalars(select(ArchiveLink)))
        except SQLAlchemyError:
            logger.debug("【GDriveTree】读取归档处理状态失败", exc_info=True)
            return done, retries
        for row in rows:
            history_id = int(row.history_id or 0)
            if history_id <= 0:
                continue
            if row.status == "done":
                done.add(history_id)
            retries[history_id] = int(row.retry_count or 0)
        return done, retries

    def _archive_link_counts(self) -> Dict[str, int]:
        """统计归档记录处理状态。"""
        counts = {"total": 0, "done": 0, "failed": 0, "pending": 0}
        try:
            with self._session() as session:
                total = int(session.scalar(select(func.count()).select_from(ArchiveLink)) or 0)
                done = int(session.scalar(
                    select(func.count()).select_from(ArchiveLink).where(
                        ArchiveLink.status == "done"
                    )
                ) or 0)
                failed = int(session.scalar(
                    select(func.count()).select_from(ArchiveLink).where(
                        ArchiveLink.status == "failed"
                    )
                ) or 0)
        except SQLAlchemyError:
            return counts
        counts["total"] = total
        counts["done"] = done
        counts["failed"] = failed
        counts["pending"] = max(0, total - done - failed)
        return counts

    def _save_archive_status(self, result: Dict[str, Any]) -> None:
        """保存最近一次归档扫描结果供页面展示。"""
        payload = dict(result)
        payload["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.save_data("archive_status", payload)
        except Exception:
            logger.debug("【GDriveTree】保存归档扫描状态失败", exc_info=True)

    def _recount_meta(self) -> None:
        """按节点表重新计算统计，归档增量后调用。"""
        now = datetime.now()
        try:
            with self._session() as session:
                total = int(session.scalar(
                    select(func.count()).select_from(TreeNode)
                ) or 0)
                dirs = int(session.scalar(
                    select(func.count()).select_from(TreeNode).where(
                        TreeNode.is_dir.is_(True)
                    )
                ) or 0)
                total_size = int(session.scalar(
                    select(func.coalesce(func.sum(TreeNode.size), 0)).select_from(TreeNode).where(
                        TreeNode.is_dir.is_(False)
                    )
                ) or 0)
                max_depth = int(session.scalar(
                    select(func.max(TreeNode.depth)).select_from(TreeNode)
                ) or 0)
                meta = session.get(TreeMeta, 1)
                if meta is None:
                    meta = TreeMeta(id=1)
                    session.add(meta)
                meta.node_count = total
                meta.dir_count = dirs
                meta.file_count = max(0, total - dirs)
                meta.total_size = total_size
                meta.max_depth = max_depth
                if meta.status in ("idle", "success"):
                    meta.status = "success"
                meta.last_success_at = now
                meta.updated_at = now
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveTree】重算目录树统计失败")

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    def _session(self):
        """返回插件自有数据库会话。"""
        return self.get_database().session()

    def _write_meta(self, **fields: Any) -> None:
        """写入刷新状态；失败只记录日志，不覆盖旧节点。"""
        try:
            with self._session() as session:
                meta = session.get(TreeMeta, 1)
                if meta is None:
                    meta = TreeMeta(id=1)
                    session.add(meta)
                for key, value in fields.items():
                    if hasattr(TreeMeta, key):
                        setattr(meta, key, value)
                meta.updated_at = datetime.now()
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveTree】写入刷新状态失败")

    def _instance_id(self) -> str:
        """返回当前插件实例 ID；分身使用宿主改写后的实例 ID。"""
        cls = type(self)
        return str(getattr(cls, "plugin_instance_id", "") or cls.__name__)

    def _publish_tree_change(self, change: TreeChangeSet) -> None:
        """把目录树变化定向通知 STRM 插件；通知失败不影响刷新。"""
        if not self._enabled:
            return
        if not self._strm_notify:
            logger.info("【GDriveTree】未开启「通知 STRM 插件」，跳过事件通知")
            return
        target = str(self._strm_plugin_id or "").strip()
        if not target:
            return
        try:
            payload = build_payload(self._instance_id(), target, change)
            eventmanager.send_event(EventType.PluginTriggered, payload)
            logger.info(
                f"【GDriveTree】发送目录树事件 → {target}："
                f"kind={change.kind}，新增={len(change.added)}，"
                f"更新={len(change.updated)}，删除={len(change.removed)}，"
                f"full={change.full}，节点数={change.node_count}"
            )
        except Exception:
            logger.warning("【GDriveTree】发送目录树变化事件失败", exc_info=True)

    def _save_snapshot(
        self,
        nodes: List[TreeNodeData],
        stats: TreeStats,
        root: RootInfo,
        source: Dict[str, str],
        pages: int,
        duration: float,
    ) -> TreeChangeSet:
        """按配置选择整体替换或按 file_id 增量写库，并在提交后发布变化事件。"""
        now = datetime.now()
        rows = [self._node_values(node, now) for node in nodes]
        if self._incremental_write:
            change = self._save_snapshot_incremental(
                nodes, rows, stats, root, source, pages, duration, now
            )
        else:
            change = self._save_snapshot_replace(
                rows, stats, root, source, pages, duration, now
            )
        if change.full or change.node_count:
            self._publish_tree_change(change)
        else:
            logger.debug("【GDriveTree】目录树无变化，跳过事件通知")
        return change

    @staticmethod
    def _node_values(node: TreeNodeData, now: datetime) -> Dict[str, Any]:
        """把建树节点转换为数据库行字典。"""
        return {
            "file_id": node.file_id,
            "parent_file_id": node.parent_file_id or "",
            "name": node.name,
            "mime_type": node.mime_type,
            "is_dir": node.is_dir,
            "size": node.size,
            "modified_time": node.modified_time,
            "created_time": node.created_time,
            "path": node.path,
            "depth": node.depth,
            "md5": node.md5,
            "trashed": node.trashed,
            "shortcut_target_id": node.shortcut_target_id,
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _node_changed(row: TreeNode, node: TreeNodeData) -> bool:
        """判断已有行与最新节点是否有需要更新的字段。"""
        checks = (
            ("parent_file_id", node.parent_file_id or ""),
            ("name", node.name),
            ("mime_type", node.mime_type),
            ("is_dir", bool(node.is_dir)),
            ("size", int(node.size or 0)),
            ("modified_time", float(node.modified_time or 0)),
            ("created_time", float(node.created_time or 0)),
            ("path", node.path),
            ("depth", int(node.depth or 0)),
            ("md5", node.md5 or ""),
            ("trashed", bool(node.trashed)),
            ("shortcut_target_id", node.shortcut_target_id or ""),
        )
        for attr, new_value in checks:
            old_value = getattr(row, attr, None)
            if attr == "parent_file_id":
                old_value = old_value or ""
            elif attr in ("size", "depth"):
                old_value = int(old_value or 0)
            elif attr in ("modified_time", "created_time"):
                old_value = float(old_value or 0)
            elif attr in ("is_dir", "trashed"):
                old_value = bool(old_value)
            elif attr in ("md5", "shortcut_target_id"):
                old_value = old_value or ""
            if old_value != new_value:
                return True
        return False

    def _apply_snapshot_meta(
        self,
        meta: TreeMeta,
        stats: TreeStats,
        root: RootInfo,
        source: Dict[str, str],
        pages: int,
        duration: float,
        now: datetime,
    ) -> None:
        """更新成功刷新的公共元信息。"""
        meta.source_plugin_id = self._source_plugin_id
        meta.root_file_id = root.file_id
        meta.root_name = root.name
        meta.root_path = normalize_root_path(source["root_path"])
        meta.corpora = root.corpora
        meta.drive_id = root.drive_id
        meta.status = "success"
        meta.last_finished_at = now
        meta.last_success_at = now
        meta.node_count = stats.node_count
        meta.dir_count = stats.dir_count
        meta.file_count = stats.file_count
        meta.total_size = stats.total_size
        meta.max_depth = stats.max_depth
        meta.page_count = pages
        meta.duration_seconds = round(duration, 3)
        meta.error = ""
        meta.updated_at = now

    def _save_snapshot_replace(
        self,
        rows: List[Dict[str, Any]],
        stats: TreeStats,
        root: RootInfo,
        source: Dict[str, str],
        pages: int,
        duration: float,
        now: datetime,
    ) -> TreeChangeSet:
        """整体替换模式：删除旧节点后批量写入，并返回全量信号。"""
        with self._session() as session:
            session.execute(delete(TreeNode))
            batch = 500
            for start in range(0, len(rows), batch):
                session.bulk_insert_mappings(TreeNode, rows[start:start + batch])
            meta = session.get(TreeMeta, 1)
            if meta is None:
                meta = TreeMeta(id=1)
                session.add(meta)
            self._apply_snapshot_meta(
                meta, stats, root, source, pages, duration, now
            )
            session.commit()
        return full_signal("refresh", len(rows))

    def _save_snapshot_incremental(
        self,
        nodes: List[TreeNodeData],
        rows: List[Dict[str, Any]],
        stats: TreeStats,
        root: RootInfo,
        source: Dict[str, str],
        pages: int,
        duration: float,
        now: datetime,
    ) -> TreeChangeSet:
        """增量模式：删除缺失、插入新增、只更新变化行，并返回变更集。"""
        with self._session() as session:
            existing = {
                row.file_id: row
                for row in session.scalars(select(TreeNode))
            }
            new_ids = {node.file_id for node in nodes}
            missing = [file_id for file_id in existing if file_id not in new_ids]
            removed = [(file_id, existing[file_id].path) for file_id in missing]
            for start in range(0, len(missing), 500):
                session.execute(
                    delete(TreeNode).where(
                        TreeNode.file_id.in_(missing[start:start + 500])
                    )
                )
            inserts: List[Dict[str, Any]] = []
            updates: List[Dict[str, Any]] = []
            added_nodes: List[TreeNodeData] = []
            updated_nodes: List[TreeNodeData] = []
            for node, row in zip(nodes, rows):
                old = existing.get(node.file_id)
                if old is None:
                    inserts.append(row)
                    added_nodes.append(node)
                elif self._node_changed(old, node):
                    update_row = dict(row)
                    update_row["id"] = old.id
                    update_row.pop("created_at", None)
                    updates.append(update_row)
                    updated_nodes.append(node)
            for start in range(0, len(inserts), 500):
                session.bulk_insert_mappings(
                    TreeNode, inserts[start:start + 500]
                )
            for start in range(0, len(updates), 500):
                session.bulk_update_mappings(
                    TreeNode, updates[start:start + 500]
                )
            meta = session.get(TreeMeta, 1)
            if meta is None:
                meta = TreeMeta(id=1)
                session.add(meta)
            self._apply_snapshot_meta(
                meta, stats, root, source, pages, duration, now
            )
            session.commit()
        logger.info(
            f"【GDriveTree】增量写库：删除={len(missing)}，插入={len(inserts)}，"
            f"更新={len(updates)}，未变化={len(rows) - len(inserts) - len(updates)}"
        )
        return change_from_nodes(
            "refresh",
            added=added_nodes,
            updated=updated_nodes,
            removed=removed,
        )

    def _safe_failed(self, status: str, error: str) -> None:
        """写入失败/取消状态，保留旧节点。"""
        self._write_meta(
            status=status,
            last_finished_at=datetime.now(),
            error=str(error or "")[:500],
        )

    def _load_meta(self) -> Dict[str, Any]:
        """读取刷新状态；表不存在或读取失败时返回空字典。"""
        try:
            with self._session() as session:
                meta = session.get(TreeMeta, 1)
                if meta is None:
                    return {}
                return {
                    "status": meta.status or "idle",
                    "source_plugin_id": meta.source_plugin_id or "",
                    "root_file_id": meta.root_file_id or "",
                    "root_name": meta.root_name or "",
                    "root_path": meta.root_path or "",
                    "corpora": meta.corpora or "",
                    "drive_id": meta.drive_id or "",
                    "last_started_at": meta.last_started_at,
                    "last_finished_at": meta.last_finished_at,
                    "last_success_at": meta.last_success_at,
                    "node_count": int(meta.node_count or 0),
                    "dir_count": int(meta.dir_count or 0),
                    "file_count": int(meta.file_count or 0),
                    "total_size": int(meta.total_size or 0),
                    "max_depth": int(meta.max_depth or 0),
                    "page_count": int(meta.page_count or 0),
                    "duration_seconds": float(meta.duration_seconds or 0.0),
                    "error": meta.error or "",
                    "updated_at": meta.updated_at,
                }
        except SQLAlchemyError:
            logger.debug("【GDriveTree】读取刷新状态失败", exc_info=True)
            return {}

    def _page_nodes(
        self,
        page: int,
        keyword: str = "",
    ) -> Tuple[int, int, int, List[Dict[str, Any]]]:
        """分页查询节点。"""
        page = max(1, int(page or 1))
        page_size = self._page_size
        try:
            with self._session() as session:
                count_stmt = select(func.count()).select_from(TreeNode)
                stmt = select(TreeNode)
                if keyword:
                    pattern = f"%{_escape_like(keyword)}%"
                    condition = or_(
                        TreeNode.name.like(pattern, escape="\\"),
                        TreeNode.path.like(pattern, escape="\\"),
                    )
                    count_stmt = count_stmt.where(condition)
                    stmt = stmt.where(condition)
                total = int(session.scalar(count_stmt) or 0)
                pages = max(1, math.ceil(total / page_size)) if total else 1
                page = min(page, pages)
                stmt = (
                    stmt.order_by(
                        TreeNode.depth.asc(),
                        TreeNode.path.asc(),
                        TreeNode.id.asc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
                items = [self._node_dict(node) for node in session.scalars(stmt)]
                return page, pages, total, items
        except SQLAlchemyError:
            logger.debug("【GDriveTree】查询节点失败", exc_info=True)
            return 1, 1, 0, []

    @staticmethod
    def _node_dict(node: TreeNode) -> Dict[str, Any]:
        """把节点模型转换为页面/API 字典。"""
        return {
            "file_id": node.file_id or "",
            "parent_file_id": node.parent_file_id or "",
            "name": node.name or "",
            "path": node.path or "",
            "is_dir": bool(node.is_dir),
            "size": int(node.size or 0),
            "size_text": "-" if node.is_dir else _format_size(node.size),
            "modified_time": _format_timestamp(node.modified_time),
            "depth": int(node.depth or 0),
        }

    def _source_status(self) -> Tuple[bool, str]:
        """返回源配置是否可用及可读说明。"""
        raw = self.get_config(self._source_plugin_id)
        if not isinstance(raw, dict):
            return False, f"未找到插件 {self._source_plugin_id} 的配置"
        missing = [
            label
            for key, label in (
                ("client_id", "client_id"),
                ("client_secret", "client_secret"),
                ("refresh_token", "refresh_token"),
            )
            if not str(raw.get(key) or "").strip()
        ]
        if missing:
            return False, f"缺少 {'、'.join(missing)}"
        root = _extract_drive_id(raw.get("root_folder_id")) or "root"
        root_path = str(raw.get("root_path") or "/").strip() or "/"
        use_shared = str(raw.get("use_shared_drive") or "").lower() in {"true", "1"}
        shared = _extract_drive_id(raw.get("shared_drive_id"))
        if use_shared and shared:
            scope = f"共享盘 {shared}"
        elif use_shared:
            scope = "共享盘（未填 ID）"
        else:
            scope = "账号网盘"
        return True, f"根目录 {root}，虚拟路径 {root_path}，抓取范围 {scope}"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页操作 API。"""
        return [
            {
                "path": "/refresh",
                "endpoint": self.api_refresh,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即刷新目录树",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新状态",
            },
            {
                "path": "/nodes/page",
                "endpoint": self.api_nodes_page,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "节点分页",
            },
            {
                "path": "/search",
                "endpoint": self.api_search,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按名称或路径搜索节点",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空目录树数据",
            },
            {
                "path": "/archive/scan",
                "endpoint": self.api_archive_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描归档记录",
            },
            {
                "path": "/archive/status",
                "endpoint": self.api_archive_status,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "归档增量状态",
            },
        ]

    def api_refresh(self) -> Dict[str, Any]:
        """启动后台刷新。"""
        try:
            started = self.start_refresh()
        except Exception as err:
            logger.exception("【GDriveTree】启动刷新失败")
            return {"success": False, "message": f"启动刷新失败：{err}", "data": None}
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        message = "刷新已启动" if started else "已有刷新任务在运行"
        return {"success": True, "message": message, "data": {"started": started}}

    def api_status(self) -> Dict[str, Any]:
        """返回刷新状态与源配置状态。"""
        meta = self._load_meta()
        configured, detail = self._source_status()
        return {
            "success": True,
            "message": "状态已刷新",
            "data": {
                "status": meta.get("status") or "idle",
                "source": {
                    "plugin_id": self._source_plugin_id,
                    "configured": configured,
                    "detail": detail,
                },
                "meta": self._meta_summary(meta),
            },
        }

    def api_nodes_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """切换节点页码。"""
        try:
            page = int((payload or {}).get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        page, pages, total, items = self._page_nodes(page)
        self._page = page
        return {
            "success": True,
            "message": f"第 {page} / {pages} 页",
            "data": {"page": page, "pages": pages, "total": total, "items": items},
        }

    def api_search(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """按名称或路径关键字分页搜索。"""
        data = payload or {}
        keyword = str(data.get("keyword") or "").strip()
        try:
            page = int(data.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        page, pages, total, items = self._page_nodes(page, keyword)
        return {
            "success": True,
            "message": f"匹配 {total} 条",
            "data": {
                "keyword": keyword,
                "page": page,
                "pages": pages,
                "total": total,
                "items": items,
            },
        }

    def api_clear(self) -> Dict[str, Any]:
        """清空节点并重置统计；刷新进行中时拒绝。"""
        if self._refresh_lock.locked():
            return {"success": False, "message": "刷新进行中，暂不能清空", "data": None}
        try:
            with self._session() as session:
                result = session.execute(delete(TreeNode))
                deleted = max(0, int(result.rowcount or 0))
                meta = session.get(TreeMeta, 1)
                if meta is not None:
                    meta.status = "idle"
                    meta.node_count = 0
                    meta.dir_count = 0
                    meta.file_count = 0
                    meta.total_size = 0
                    meta.max_depth = 0
                    meta.page_count = 0
                    meta.duration_seconds = 0.0
                    meta.error = ""
                    meta.root_file_id = ""
                    meta.root_name = ""
                    meta.root_path = ""
                    meta.corpora = ""
                    meta.drive_id = ""
                    meta.last_started_at = None
                    meta.last_finished_at = None
                    meta.last_success_at = None
                    meta.updated_at = datetime.now()
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveTree】清空目录树失败")
            return {"success": False, "message": "清空失败，请查看日志", "data": None}
        self._page = 1
        self._publish_tree_change(full_signal("clear", deleted))
        logger.info(f"【GDriveTree】已清空目录树节点：{deleted} 条")
        return {"success": True, "message": f"已清空 {deleted} 条节点", "data": {"deleted": deleted}}

    def api_archive_scan(self) -> Dict[str, Any]:
        """启动一次归档记录扫描。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        if not self._archive_watch:
            return {"success": False, "message": "未开启归档监控", "data": None}
        try:
            started = self.start_archive_scan()
        except Exception as err:
            logger.exception("【GDriveTree】启动归档扫描失败")
            return {
                "success": False,
                "message": f"启动归档扫描失败：{err}",
                "data": None,
            }
        return {
            "success": True,
            "message": "归档扫描已启动" if started else "归档扫描正在运行",
            "data": {"started": started},
        }

    def api_archive_status(self) -> Dict[str, Any]:
        """返回归档增量处理状态。"""
        last_scan = self.get_data("archive_status")
        return {
            "success": True,
            "message": "归档状态已刷新",
            "data": {
                "enabled": self._archive_watch,
                "interval_minutes": self._archive_interval_minutes,
                "counts": self._archive_link_counts(),
                "last_scan": last_scan if isinstance(last_scan, dict) else {},
            },
        }

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    def get_page(self) -> Optional[List[dict]]:
        """返回状态、统计、按钮和节点分页表格。"""
        contents: List[dict] = []
        if not self._enabled:
            contents.append(self._alert("warning", "插件未启用，以下为最近一次状态。"))

        configured, detail = self._source_status()
        contents.append(self._alert(
            "success" if configured else "error",
            f"源配置（{self._source_plugin_id}）："
            f"{'可用，' + detail if configured else '不可用，' + detail}。",
        ))

        meta = self._load_meta()
        status = meta.get("status") or "idle"
        status_text = {
            "idle": "空闲",
            "running": "刷新中",
            "success": "成功",
            "failed": "失败",
            "cancelled": "已中止",
        }.get(status, status)
        stats_text = (
            f"状态：{status_text}；"
            f"最后成功：{_format_datetime(meta.get('last_success_at'))}；"
            f"节点 {int(meta.get('node_count') or 0)}"
            f"（目录 {int(meta.get('dir_count') or 0)}，"
            f"文件 {int(meta.get('file_count') or 0)}）；"
            f"总大小 {_format_size(meta.get('total_size'))}；"
            f"最大深度 {int(meta.get('max_depth') or 0)}；"
            f"页数 {int(meta.get('page_count') or 0)}；"
            f"耗时 {float(meta.get('duration_seconds') or 0):.1f}s。"
        )
        contents.append(self._alert("info", stats_text))

        if self._archive_watch:
            counts = self._archive_link_counts()
            last_scan = self.get_data("archive_status")
            last_scan = last_scan if isinstance(last_scan, dict) else {}
            archive_text = (
                f"归档增量：已处理 {counts['done']}，失败 {counts['failed']}，"
                f"待处理 {counts['pending']}；最近扫描 "
                f"{last_scan.get('time') or '-'}：扫描 {last_scan.get('scanned', 0)}，"
                f"新增 {last_scan.get('processed', 0)}，"
                f"失败 {last_scan.get('failed', 0)}。"
            )
        else:
            archive_text = "归档增量：未开启。"
        contents.append(self._alert("info", archive_text))

        if meta.get("root_file_id"):
            contents.append(self._alert(
                "info",
                f"根节点：{meta.get('root_name') or '-'}"
                f"（ID {meta.get('root_file_id')}）；"
                f"抓取范围 {meta.get('corpora') or '-'}"
                f"{'，共享盘 ' + str(meta.get('drive_id')) if meta.get('drive_id') else ''}；"
                f"虚拟根路径 {meta.get('root_path') or '/'}。",
            ))
        if meta.get("error"):
            contents.append(self._alert(
                "error",
                f"最近错误（{_format_datetime(meta.get('last_finished_at'))}）："
                f"{meta.get('error')}",
            ))

        page, pages, total, items = self._page_nodes(self._page)
        self._page = page
        buttons: List[dict] = [
            self._page_button("立即刷新", "mdi-refresh", "/refresh"),
            self._page_button("刷新状态", "mdi-sync", "/status"),
            self._page_button("扫描归档", "mdi-history", "/archive/scan"),
            self._page_button("清空数据", "mdi-delete-sweep", "/clear"),
        ]
        contents.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"},
            "content": buttons,
        })

        if items:
            headers = ["路径", "类型", "大小", "修改时间", "ID"]
            ths = [{"component": "th", "text": header} for header in headers]
            rows = []
            for item in items:
                rows.append({
                    "component": "tr",
                    "content": [
                        self._text_cell(item.get("path") or "-"),
                        self._type_cell(bool(item.get("is_dir"))),
                        self._text_cell(item.get("size_text") or "-"),
                        self._text_cell(item.get("modified_time") or "-"),
                        self._text_cell(item.get("file_id") or "-", title=item.get("file_id")),
                    ],
                })
            contents.append({
                "component": "div",
                "props": {"class": "flex-grow-1", "style": "min-height: 0; overflow: auto;"},
                "content": [{
                    "component": "VTable",
                    "props": {"density": "compact", "hover": True},
                    "content": [
                        {
                            "component": "thead",
                            "props": {
                                "style": (
                                    "position: sticky; top: 0; z-index: 1; "
                                    "background: rgb(var(--v-theme-surface));"
                                ),
                            },
                            "content": [{"component": "tr", "content": ths}],
                        },
                        {"component": "tbody", "content": rows},
                    ],
                }],
            })
        else:
            contents.append({
                "component": "div",
                "props": {
                    "class": "flex-grow-1 d-flex align-center justify-center",
                    "style": "min-height: 0;",
                },
                "content": [self._alert("info", "暂无节点数据，点击“立即刷新”生成目录树。")],
            })

        contents.append({
            "component": "div",
            "props": {"class": "d-flex align-center justify-center ga-2 mt-2 flex-shrink-0"},
            "content": [
                self._page_button(
                    "上一页", "mdi-chevron-left", "/nodes/page",
                    {"page": self._page - 1}, disabled=self._page <= 1,
                ),
                {
                    "component": "span",
                    "props": {"class": "text-body-2"},
                    "text": f"第 {self._page} / {pages} 页（共 {total} 条）",
                },
                self._page_button(
                    "下一页", "mdi-chevron-right", "/nodes/page",
                    {"page": self._page + 1}, disabled=self._page >= pages,
                ),
            ],
        })

        return [{
            "component": "div",
            "props": {
                "class": "d-flex flex-column",
                "style": (
                    "height: 72vh; min-height: 520px; max-height: 800px; "
                    "padding-bottom: 84px; box-sizing: border-box;"
                ),
            },
            "content": contents,
        }]

    @staticmethod
    def _meta_summary(meta: Dict[str, Any]) -> Dict[str, Any]:
        """把 meta 转成 API 友好的摘要（时间转文本）。"""
        return {
            "root_file_id": meta.get("root_file_id") or "",
            "root_name": meta.get("root_name") or "",
            "root_path": meta.get("root_path") or "",
            "corpora": meta.get("corpora") or "",
            "drive_id": meta.get("drive_id") or "",
            "last_started_at": _format_datetime(meta.get("last_started_at")),
            "last_finished_at": _format_datetime(meta.get("last_finished_at")),
            "last_success_at": _format_datetime(meta.get("last_success_at")),
            "node_count": int(meta.get("node_count") or 0),
            "dir_count": int(meta.get("dir_count") or 0),
            "file_count": int(meta.get("file_count") or 0),
            "total_size": int(meta.get("total_size") or 0),
            "total_size_text": _format_size(meta.get("total_size")),
            "max_depth": int(meta.get("max_depth") or 0),
            "page_count": int(meta.get("page_count") or 0),
            "duration_seconds": float(meta.get("duration_seconds") or 0.0),
            "error": meta.get("error") or "",
        }

    @staticmethod
    def _alert(level: str, text: str) -> dict:
        """构建固定高度容器内不会被压扁的告警。"""
        return {
            "component": "VAlert",
            "props": {
                "type": level,
                "variant": "tonal",
                "class": "mb-3",
                "style": "flex: 0 0 auto;",
                "text": text,
            },
        }

    def _page_button(
        self,
        text: str,
        icon: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        disabled: bool = False,
    ) -> dict:
        """构建调用插件 API 的页面按钮。"""
        return {
            "component": "VBtn",
            "props": {
                "variant": "tonal",
                "color": "primary",
                "prepend-icon": icon,
                "disabled": bool(disabled),
            },
            "text": text,
            "events": {
                "click": {
                    "api": f"plugin/{self.__class__.__name__}{path}",
                    "method": "post",
                    "params": params or {},
                },
            },
        }

    @staticmethod
    def _text_cell(text: Any, title: Any = None) -> dict:
        """构建截断显示的表格单元格。"""
        value = str(text or "-")
        return {
            "component": "td",
            "props": {
                "style": (
                    "max-width: 420px; overflow: hidden; text-overflow: ellipsis; "
                    "white-space: nowrap;"
                ),
                "title": str(title or value),
            },
            "text": value,
        }

    @staticmethod
    def _type_cell(is_dir: bool) -> dict:
        """构建目录/文件类型单元格。"""
        return {
            "component": "td",
            "content": [{
                "component": "VChip",
                "props": {
                    "color": "warning" if is_dir else "info",
                    "size": "small",
                    "variant": "tonal",
                },
                "text": "目录" if is_dir else "文件",
            }],
        }
