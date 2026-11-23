"""Google STRM 管理插件（GDriveStrm）。

读取 GDriveTree 插件持久化的目录树，把视频文件映射为本地 ``.strm``；
支持目录树变化的定向事件增量、定时对账与手动全量同步。
"""

from __future__ import annotations

import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from fastapi import Body
from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from app.schemas.types import EventType
from app.sdk.database import plugin_declarative_base
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

try:  # 兼容不带 app.sdk.config 的早期 v3 版本
    from app.sdk.config import settings as _settings
except Exception:  # pragma: no cover - 宿主差异
    _settings = None

from .strm_mapper import (
    DEFAULT_URL_TEMPLATE,
    DEFAULT_VIDEO_EXTENSIONS,
    build_targets,
    parse_extensions,
)
from .strm_syncer import StrmRecordData, StrmSyncer, SyncOutcome
from .tree_source import TreeSourceError, probe, read_root_path, read_snapshot

DEFAULT_TREE_PLUGIN = "GDriveTree"
DEFAULT_OUTPUT_DIR = "/config/strm"
DEFAULT_SYNC_INTERVAL_MINUTES = 10
DEFAULT_PAGE_SIZE = 20
COALESCE_SECONDS = 1.0
MAX_COALESCE_NODES = 5000
WORKER_JOIN_SECONDS = 5
EVENT_NAME = "tree.changed"

_PluginDBBase = plugin_declarative_base()


class StrmRecord(_PluginDBBase):
    """已写出的 STRM 记录。"""

    __tablename__ = "strm_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rel_path: Mapped[str] = mapped_column(
        String(2048), unique=True, index=True, default=""
    )
    source_file_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    source_path: Mapped[str] = mapped_column(String(2048), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    modified_time: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class StrmMeta(_PluginDBBase):
    """同步状态与统计（固定单行，id=1）。"""

    __tablename__ = "strm_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tree_plugin_id: Mapped[str] = mapped_column(String(128), default="")
    output_dir: Mapped[str] = mapped_column(String(2048), default="")
    status: Mapped[str] = mapped_column(String(16), index=True, default="idle")
    last_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    dir_count: Mapped[int] = mapped_column(Integer, default=0)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    removed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """把配置值限制在范围内。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _format_datetime(value: Any) -> str:
    """把时间格式化为本地文本。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return "-"


def _format_size(value: Any) -> str:
    """把字节数格式化为易读文本。"""
    number = float(max(0, int(value or 0)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.1f}{unit}"
        number /= 1024
    return f"{number:.1f}TB"


def _read_field(source: Any, name: str) -> Any:
    """从字典或对象中读取字段。"""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _config_root() -> Path:
    """返回宿主配置目录，用于解析相对输出路径。"""
    base = getattr(_settings, "CONFIG_PATH", None)
    if base:
        try:
            return Path(base)
        except (TypeError, ValueError):
            pass
    docker = Path("/config")
    if docker.is_dir():
        return docker
    return Path.cwd()


class GDriveStrm(_PluginBase):
    """Google STRM 管理插件。"""

    plugin_name = "谷歌 STRM 管理"
    plugin_desc = (
        "读取 Google Drive 目录树，把视频文件映射为本地 STRM；"
        "支持目录树事件增量、定时同步与全量同步。"
    )
    plugin_version = "1.0.1"
    plugin_label = "网盘"
    plugin_author = "yating1022"
    plugin_config_prefix = "gdrivestrm_"
    plugin_order = 102
    auth_level = 1

    def __init__(self) -> None:
        """初始化运行时状态；不做网络或数据库操作。"""
        super().__init__()
        self._config: Dict[str, Any] = {}
        self._enabled = False
        self._tree_plugin_id = DEFAULT_TREE_PLUGIN
        self._output_dir = DEFAULT_OUTPUT_DIR
        self._extensions = parse_extensions(DEFAULT_VIDEO_EXTENSIONS)
        self._url_template = DEFAULT_URL_TEMPLATE
        self._event_watch = True
        self._cleanup = True
        self._auto_sync = False
        self._sync_interval_minutes = DEFAULT_SYNC_INTERVAL_MINUTES
        self._page_size = DEFAULT_PAGE_SIZE
        self._page = 1
        self._sync_lock = threading.Lock()
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._worker_stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并启动后台同步线程，可重复调用。"""
        self.stop_service()
        cfg = self._normalize_config(config or {})
        self._config = cfg
        self._enabled = bool(cfg["enabled"])
        self._tree_plugin_id = cfg["tree_plugin_id"]
        self._output_dir = cfg["output_dir"]
        self._extensions = parse_extensions(cfg["extensions"])
        self._url_template = cfg["url_template"]
        self._event_watch = bool(cfg["event_watch"])
        self._cleanup = bool(cfg["cleanup"])
        self._auto_sync = bool(cfg["auto_sync"])
        self._sync_interval_minutes = cfg["sync_interval_minutes"]
        self._page_size = cfg["page_size"]
        self._page = 1
        if self._enabled:
            self._queue = queue.Queue()
            self._worker_stop = threading.Event()
            self._worker = threading.Thread(
                target=self._worker_loop,
                args=(self._queue, self._worker_stop),
                name="GDriveStrm-worker",
                daemon=True,
            )
            self._worker.start()
            logger.info(
                f"【GDriveStrm】插件已启用 v{self.plugin_version}："
                f"树插件={self._tree_plugin_id}，输出={self._output_dir}，"
                f"定时同步={self._auto_sync}，清理={self._cleanup}"
            )
        else:
            self._worker = None
            logger.debug("【GDriveStrm】插件未启用")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def stop_service(self) -> None:
        """停止后台线程；正在写入的单个文件允许完成。"""
        self._worker_stop.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            try:
                self._queue.put({"kind": "__stop__"})
            except Exception:  # pragma: no cover - 队列关闭时忽略
                pass
            worker.join(WORKER_JOIN_SECONDS)

    def get_database_models(self) -> List[Type[Any]]:
        """声明插件自有表。"""
        return [StrmMeta, StrmRecord]

    @staticmethod
    def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把用户配置归一化为稳定类型和默认值。"""
        return {
            "enabled": bool(cfg.get("enabled")),
            "tree_plugin_id": str(
                cfg.get("tree_plugin_id") or DEFAULT_TREE_PLUGIN
            ).strip() or DEFAULT_TREE_PLUGIN,
            "output_dir": str(
                cfg.get("output_dir") or DEFAULT_OUTPUT_DIR
            ).strip() or DEFAULT_OUTPUT_DIR,
            "extensions": str(
                cfg.get("extensions") or DEFAULT_VIDEO_EXTENSIONS
            ).strip() or DEFAULT_VIDEO_EXTENSIONS,
            "url_template": str(
                cfg.get("url_template") or DEFAULT_URL_TEMPLATE
            ).strip() or DEFAULT_URL_TEMPLATE,
            "event_watch": bool(cfg.get("event_watch", True)),
            "cleanup": bool(cfg.get("cleanup", True)),
            "auto_sync": bool(cfg.get("auto_sync")),
            "sync_interval_minutes": _clamp_int(
                cfg.get("sync_interval_minutes"),
                1,
                1440,
                DEFAULT_SYNC_INTERVAL_MINUTES,
            ),
            "page_size": _clamp_int(cfg.get("page_size"), 5, 100, DEFAULT_PAGE_SIZE),
        }

    # ------------------------------------------------------------------
    # 配置表单
    # ------------------------------------------------------------------
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
                                "本插件读取 GDriveTree 的目录树数据，请先启用并刷新目录树；"
                                "视频文件会生成 .strm，内容为 Google Drive 直链。"
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
                                    "props": {"model": "event_watch", "label": "处理目录树事件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "cleanup", "label": "清理失效 STRM"},
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
                                        "model": "tree_plugin_id",
                                        "label": "目录树插件 ID",
                                        "placeholder": DEFAULT_TREE_PLUGIN,
                                        "hint": "默认 GDriveTree；使用分身时填分身 ID",
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
                                        "model": "output_dir",
                                        "label": "STRM 输出根目录",
                                        "placeholder": DEFAULT_OUTPUT_DIR,
                                        "hint": "绝对路径；相对路径按宿主配置目录解析",
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
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "extensions",
                                        "label": "视频后缀",
                                        "hint": "逗号/空格/换行分隔；留空恢复默认视频格式",
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
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "url_template",
                                        "label": "STRM 内容模板",
                                        "hint": "必须包含 {fileId} 占位符",
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
                                    "props": {"model": "auto_sync", "label": "定时同步"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "sync_interval_minutes",
                                        "label": "同步间隔（分钟）",
                                        "type": "number",
                                        "min": 1,
                                        "max": 1440,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "page_size",
                                        "label": "每页记录数",
                                        "type": "number",
                                        "min": 5,
                                        "max": 100,
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "tree_plugin_id": DEFAULT_TREE_PLUGIN,
            "output_dir": DEFAULT_OUTPUT_DIR,
            "extensions": DEFAULT_VIDEO_EXTENSIONS,
            "url_template": DEFAULT_URL_TEMPLATE,
            "event_watch": True,
            "cleanup": True,
            "auto_sync": False,
            "sync_interval_minutes": DEFAULT_SYNC_INTERVAL_MINUTES,
            "page_size": DEFAULT_PAGE_SIZE,
        }

    # ------------------------------------------------------------------
    # 定时服务与事件
    # ------------------------------------------------------------------
    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """按开关注册定时同步服务。"""
        if not self._enabled or not self._auto_sync:
            return []
        return [{
            "id": "GDriveStrm",
            "name": "谷歌 STRM 管理（定时同步）",
            "trigger": "interval",
            "func": self.sync_from_scheduler,
            "kwargs": {"seconds": max(60, self._sync_interval_minutes * 60)},
        }]

    def sync_from_scheduler(self) -> None:
        """调度器入口：只入队，立即返回。"""
        if not self._enabled:
            return
        logger.debug("【GDriveStrm】定时同步入队")
        self._enqueue({"kind": "diff", "source": "scheduler"})

    @eventmanager.register(EventType.PluginTriggered)
    def on_plugin_triggered(self, event: Event) -> None:
        """处理目录树插件发来的定向变更事件。"""
        if not self._enabled or not self._event_watch:
            logger.debug("【GDriveStrm】插件未启用或未开启事件处理，忽略事件")
            return
        data = getattr(event, "event_data", event)
        publisher = str(_read_field(data, "plugin_id") or "").strip()
        event_name = str(_read_field(data, "event_name") or "").strip()
        if publisher.casefold() != self._tree_plugin_id.casefold():
            logger.debug(
                f"【GDriveStrm】忽略事件：发布方 {publisher or '-'} "
                f"不是目录树插件 {self._tree_plugin_id}"
            )
            return
        if event_name != EVENT_NAME:
            logger.debug(
                f"【GDriveStrm】忽略事件：事件名 {event_name or '-'} "
                f"不是 {EVENT_NAME}"
            )
            return
        payload = _read_field(data, "data")
        if not isinstance(payload, dict):
            payload = {}
        added = payload.get("added")
        updated = payload.get("updated")
        removed = payload.get("removed")
        logger.info(
            f"【GDriveStrm】收到目录树事件：来源={publisher}，"
            f"kind={payload.get('kind') or '-'}，"
            f"full={bool(payload.get('full'))}，"
            f"新增={len(added) if isinstance(added, list) else 0}，"
            f"更新={len(updated) if isinstance(updated, list) else 0}，"
            f"删除={len(removed) if isinstance(removed, list) else 0}"
        )
        self._enqueue({"kind": "event", "payload": payload})

    def _enqueue(self, job: Dict[str, Any]) -> bool:
        """把同步作业放入工作队列。"""
        if not self._enabled:
            return False
        self._queue.put(job)
        return True

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------
    def _worker_loop(
        self,
        jobs: "queue.Queue[Dict[str, Any]]",
        stop_event: threading.Event,
    ) -> None:
        """合并短时间内的作业并串行执行。"""
        while not stop_event.is_set():
            try:
                job = jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if str(job.get("kind") or "") == "__stop__":
                break
            batch = [job]
            deadline = time.monotonic() + COALESCE_SECONDS
            while not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = jobs.get(timeout=remaining)
                except queue.Empty:
                    break
                if str(item.get("kind") or "") == "__stop__":
                    stop_event.set()
                    break
                batch.append(item)
            if stop_event.is_set():
                break
            merged = self._merge_jobs(batch)
            try:
                self._execute(merged, stop_event)
            except Exception:
                logger.exception("【GDriveStrm】同步任务失败")
                self._write_meta(
                    status="failed",
                    last_finished_at=datetime.now(),
                    error="同步任务出现未预期异常，请查看日志",
                )

    @staticmethod
    def _merge_jobs(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把一批作业合并为一次执行；同一节点按最后一次动作生效。"""
        full = False
        diff = False
        actions: Dict[str, str] = {}
        nodes: Dict[str, Dict[str, Dict[str, Any]]] = {
            "added": {},
            "updated": {},
            "removed": {},
        }
        for job in batch:
            kind = str(job.get("kind") or "")
            if kind == "full":
                full = True
            elif kind == "diff":
                diff = True
            elif kind == "event":
                payload = job.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("full"):
                    full = True
                for action in ("added", "updated", "removed"):
                    items = payload.get(action)
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        identity = str(
                            item.get("file_id") or item.get("path") or ""
                        )
                        if not identity:
                            continue
                        actions[identity] = action
                        nodes[action][identity] = item
        if full:
            return {"kind": "full"}
        if diff:
            return {"kind": "diff"}
        changes: Dict[str, List[Dict[str, Any]]] = {
            "added": [],
            "updated": [],
            "removed": [],
        }
        for identity, action in actions.items():
            changes[action].append(nodes[action][identity])
        total = sum(len(items) for items in changes.values())
        if total > MAX_COALESCE_NODES:
            return {"kind": "diff"}
        return {"kind": "event", "payload": changes}

    # ------------------------------------------------------------------
    # 同步执行
    # ------------------------------------------------------------------
    def _execute(self, job: Dict[str, Any], stop_event: threading.Event) -> None:
        """执行一次同步作业；同一时刻只允许一个同步在跑。"""
        if not self._enabled:
            return
        if not self._sync_lock.acquire(timeout=1.0):
            # 上一代工作线程仍在收尾：保留作业，稍后重试
            self._queue.put(job)
            return
        try:
            self._execute_locked(job, stop_event)
        finally:
            try:
                self._sync_lock.release()
            except RuntimeError:
                pass

    def _execute_locked(
        self, job: Dict[str, Any], stop_event: threading.Event
    ) -> None:
        """在持有同步锁时执行作业，包含文件写入、记录持久化与状态更新。"""
        started = time.monotonic()
        outcome: Optional[SyncOutcome] = None
        target_count = 0
        previous = self._load_meta()
        try:
            if "{fileId}" not in self._url_template:
                now = datetime.now()
                self._write_meta(
                    status="failed",
                    last_started_at=now,
                    last_finished_at=now,
                    error="STRM 内容模板缺少 {fileId} 占位符，已中止同步",
                )
                logger.warning("【GDriveStrm】STRM 内容模板缺少 {fileId}，同步中止")
                return
            root = self._resolve_output_dir(self._output_dir)
            if root is None:
                now = datetime.now()
                self._write_meta(
                    status="failed",
                    last_started_at=now,
                    last_finished_at=now,
                    error="输出目录无效或不可写，请检查配置",
                )
                logger.warning(f"【GDriveStrm】输出目录无效或不可写：{self._output_dir}")
                return
            cleanup = self._cleanup
            root_changed = bool(
                previous.get("output_dir")
            ) and previous["output_dir"] != str(root)
            tree_changed = bool(
                previous.get("tree_plugin_id")
            ) and previous["tree_plugin_id"] != self._tree_plugin_id
            unknown_scope = (
                not previous.get("output_dir") and self._record_count() > 0
            )
            if root_changed or tree_changed or unknown_scope:
                logger.info(
                    "【GDriveStrm】输出目录或目录树已变化，"
                    "重置记录并跳过本次清理"
                )
                self._reset_records()
                cleanup = False
            self._write_meta(
                status="running",
                tree_plugin_id=self._tree_plugin_id,
                output_dir=str(root),
                last_started_at=datetime.now(),
                last_finished_at=None,
                error="",
            )
            kind = str(job.get("kind") or "diff")
            logger.info(
                f"【GDriveStrm】开始同步：类型={kind}，"
                f"树={self._tree_plugin_id}，输出={root}"
            )
            database = self.get_database(self._tree_plugin_id)
            records = self._load_records()
            syncer = StrmSyncer(
                root, cleanup=cleanup, stop_event=stop_event
            )
            if kind in ("full", "diff"):
                snapshot = read_snapshot(database, stop_event)
                targets = build_targets(
                    snapshot.files,
                    snapshot.root_path,
                    self._extensions,
                    self._url_template,
                )
                target_count = len(targets)
                outcome = syncer.full_sync(
                    targets, records, verify_content=(kind == "full")
                )
            else:
                payload = job.get("payload") or {}
                root_path = read_root_path(database)
                outcome = syncer.apply_changes(
                    payload.get("added") or [],
                    payload.get("updated") or [],
                    payload.get("removed") or [],
                    root_path=root_path,
                    extensions=self._extensions,
                    url_template=self._url_template,
                    records=records,
                )
                target_count = len(records)
            if outcome is None:
                return
            self._log_outcome(outcome)
            try:
                record_count = self._persist(outcome)
            except SQLAlchemyError:
                logger.exception("【GDriveStrm】写入 STRM 记录失败")
                self._write_meta(
                    status="failed",
                    last_finished_at=datetime.now(),
                    added_count=len(outcome.added),
                    updated_count=len(outcome.updated),
                    removed_count=len(outcome.removed),
                    error="STRM 文件已写入，但记录保存失败，下次同步会自动校正",
                )
                return
        except InterruptedError:
            self._write_meta(
                status="cancelled",
                last_finished_at=datetime.now(),
                error="同步已中止",
            )
            return
        except TreeSourceError as err:
            logger.warning(f"【GDriveStrm】读取目录树失败：{err}")
            self._write_meta(
                status="failed",
                last_finished_at=datetime.now(),
                error=str(err)[:500],
            )
            return
        except Exception:
            logger.exception("【GDriveStrm】同步失败")
            self._write_meta(
                status="failed",
                last_finished_at=datetime.now(),
                error="同步失败，请查看日志",
            )
            return
        duration = time.monotonic() - started
        status = "cancelled" if outcome.cancelled else "success"
        error = ""
        if outcome.errors:
            error = "；".join(outcome.errors[:5])
            if len(outcome.errors) > 5:
                error += f"（另有 {len(outcome.errors) - 5} 条错误）"
        now = datetime.now()
        fields: Dict[str, Any] = {
            "status": status,
            "last_finished_at": now,
            "target_count": target_count,
            "record_count": record_count,
            "dir_count": self._count_dirs(),
            "added_count": len(outcome.added),
            "updated_count": len(outcome.updated),
            "removed_count": len(outcome.removed),
            "skipped_count": outcome.skipped,
            "error_count": len(outcome.errors),
            "duration_seconds": round(duration, 3),
            "error": error[:500],
        }
        if status == "success":
            fields["last_success_at"] = now
        if job.get("kind") == "event":
            fields["last_event_at"] = now
        self._write_meta(**fields)
        logger.info(
            f"【GDriveStrm】同步完成：{status}，目标 {target_count}，"
            f"新增 {len(outcome.added)}，更新 {len(outcome.updated)}，"
            f"删除 {len(outcome.removed)}，跳过 {outcome.skipped}，"
            f"错误 {len(outcome.errors)}，耗时 {duration:.1f}s"
        )

    @staticmethod
    def _log_outcome(outcome: SyncOutcome) -> None:
        """逐条输出本次同步的新增/更新/删除与错误（有数量上限）。"""
        limit = 100
        for item in outcome.added[:limit]:
            logger.info(
                f"【GDriveStrm】新增 STRM：{item.rel_path} ← {item.source_path}"
            )
        if len(outcome.added) > limit:
            logger.info(
                f"【GDriveStrm】新增 STRM 另有 {len(outcome.added) - limit} 条"
            )
        for item in outcome.updated[:limit]:
            logger.info(
                f"【GDriveStrm】更新 STRM：{item.rel_path} ← {item.source_path}"
            )
        if len(outcome.updated) > limit:
            logger.info(
                f"【GDriveStrm】更新 STRM 另有 {len(outcome.updated) - limit} 条"
            )
        for relative in outcome.removed[:limit]:
            logger.info(f"【GDriveStrm】删除 STRM：{relative}")
        if len(outcome.removed) > limit:
            logger.info(
                f"【GDriveStrm】删除 STRM 另有 {len(outcome.removed) - limit} 条"
            )
        for error in outcome.errors[:20]:
            logger.warning(f"【GDriveStrm】同步错误：{error}")
        if len(outcome.errors) > 20:
            logger.warning(
                f"【GDriveStrm】同步错误另有 {len(outcome.errors) - 20} 条"
            )

    def _resolve_output_dir(self, value: Any) -> Optional[Path]:
        """解析输出目录并验证可写。"""
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = _config_root() / path
        try:
            path = path.resolve()
        except (OSError, ValueError):
            return None
        if path.parent == path:
            return None
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe_file = path / ".gdrivestrm-write-test"
            probe_file.write_text("", encoding="utf-8")
            probe_file.unlink()
        except OSError:
            return None
        return path

    def _load_records(self) -> Dict[str, StrmRecordData]:
        """读取全部 STRM 记录。"""
        try:
            with self.get_database().session() as session:
                rows = session.scalars(select(StrmRecord)).all()
        except SQLAlchemyError:
            logger.exception("【GDriveStrm】读取 STRM 记录失败")
            return {}
        return {
            row.rel_path: StrmRecordData(
                rel_path=row.rel_path,
                source_file_id=row.source_file_id or "",
                source_path=row.source_path or "",
                url=row.url or "",
                size=int(row.size or 0),
                modified_time=float(row.modified_time or 0.0),
            )
            for row in rows
        }

    def _record_count(self) -> int:
        """返回当前记录数；读取失败时按 0 处理。"""
        try:
            with self.get_database().session() as session:
                return int(
                    session.scalar(select(func.count()).select_from(StrmRecord))
                    or 0
                )
        except SQLAlchemyError:
            return 0

    def _reset_records(self) -> None:
        """清空记录（不删除磁盘文件），用于输出目录/目录树切换。"""
        try:
            with self.get_database().session() as session:
                session.execute(delete(StrmRecord))
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveStrm】重置 STRM 记录失败")

    def _persist(self, outcome: SyncOutcome) -> int:
        """把同步结果写入记录表，返回记录总数。"""
        with self.get_database().session() as session:
            existing = {
                row.rel_path: row for row in session.scalars(select(StrmRecord))
            }
            upserts: Dict[str, StrmRecordData] = {}
            for data in outcome.added + outcome.updated:
                upserts[data.rel_path] = data
            for relative in outcome.removed:
                if relative in upserts:
                    # 同一路径在同一次同步中被重新占用：保留行并原地更新，
                    # 避免同一 rel_path 同时 DELETE + INSERT 触发唯一约束
                    continue
                row = existing.pop(relative, None)
                if row is not None:
                    session.delete(row)
            now = datetime.now()
            for rel_path, data in upserts.items():
                row = existing.get(rel_path)
                if row is None:
                    row = StrmRecord(
                        rel_path=rel_path,
                        source_file_id=data.source_file_id,
                        source_path=data.source_path,
                        url=data.url,
                        size=data.size,
                        modified_time=data.modified_time,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    existing[rel_path] = row
                else:
                    row.source_file_id = data.source_file_id
                    row.source_path = data.source_path
                    row.url = data.url
                    row.size = data.size
                    row.modified_time = data.modified_time
                    row.updated_at = now
            session.commit()
            return int(
                session.scalar(select(func.count()).select_from(StrmRecord)) or 0
            )

    def _count_dirs(self) -> int:
        """统计记录涉及的父目录数量。"""
        try:
            with self.get_database().session() as session:
                paths = session.scalars(select(StrmRecord.rel_path)).all()
        except SQLAlchemyError:
            return 0
        directories = set()
        for relative in paths:
            parent = str(relative).rsplit("/", 1)[0] if "/" in str(relative) else ""
            if parent:
                directories.add(parent)
        return len(directories)

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _write_meta(self, **fields: Any) -> None:
        """写入同步状态；表不存在或写入失败只记日志。"""
        try:
            with self.get_database().session() as session:
                meta = session.get(StrmMeta, 1)
                if meta is None:
                    meta = StrmMeta(id=1)
                    session.add(meta)
                for key, value in fields.items():
                    if hasattr(meta, key):
                        setattr(meta, key, value)
                meta.updated_at = datetime.now()
                session.commit()
        except SQLAlchemyError:
            logger.exception("【GDriveStrm】写入同步状态失败")

    def _load_meta(self) -> Dict[str, Any]:
        """读取同步状态；失败返回空字典。"""
        try:
            with self.get_database().session() as session:
                meta = session.get(StrmMeta, 1)
                if meta is None:
                    return {}
                return {
                    "tree_plugin_id": meta.tree_plugin_id or "",
                    "output_dir": meta.output_dir or "",
                    "status": meta.status or "idle",
                    "last_started_at": meta.last_started_at,
                    "last_finished_at": meta.last_finished_at,
                    "last_success_at": meta.last_success_at,
                    "last_event_at": meta.last_event_at,
                    "target_count": int(meta.target_count or 0),
                    "record_count": int(meta.record_count or 0),
                    "dir_count": int(meta.dir_count or 0),
                    "added_count": int(meta.added_count or 0),
                    "updated_count": int(meta.updated_count or 0),
                    "removed_count": int(meta.removed_count or 0),
                    "skipped_count": int(meta.skipped_count or 0),
                    "error_count": int(meta.error_count or 0),
                    "duration_seconds": float(meta.duration_seconds or 0.0),
                    "error": meta.error or "",
                    "updated_at": meta.updated_at,
                }
        except SQLAlchemyError:
            logger.debug("【GDriveStrm】读取同步状态失败", exc_info=True)
            return {}

    def _source_status(self) -> Tuple[bool, str]:
        """探测目录树插件是否可用。"""
        if not self._enabled:
            return False, "插件未启用"
        try:
            probe(self.get_database(self._tree_plugin_id))
            return True, "目录树可用"
        except TreeSourceError as err:
            return False, str(err)
        except Exception as err:  # pragma: no cover - 防御性兜底
            return False, f"目录树不可用：{err}"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页操作 API。"""
        return [
            {
                "path": "/sync",
                "endpoint": self.api_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即增量同步",
            },
            {
                "path": "/sync/full",
                "endpoint": self.api_sync_full,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "全量同步",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "同步状态",
            },
            {
                "path": "/records/page",
                "endpoint": self.api_records_page,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "STRM 记录分页",
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空记录（不删文件）",
            },
        ]

    def api_sync(self) -> Dict[str, Any]:
        """入队一次增量对账。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        self._enqueue({"kind": "diff", "source": "api"})
        logger.info("【GDriveStrm】手动增量同步已入队")
        return {"success": True, "message": "增量同步已入队", "data": {"queued": True}}

    def api_sync_full(self) -> Dict[str, Any]:
        """入队一次全量同步。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        self._enqueue({"kind": "full", "source": "api"})
        logger.info("【GDriveStrm】手动全量同步已入队")
        return {"success": True, "message": "全量同步已入队", "data": {"queued": True}}

    def api_status(self) -> Dict[str, Any]:
        """返回同步状态与树源可用性。"""
        meta = self._load_meta()
        available, detail = self._source_status()
        return {
            "success": True,
            "message": "状态已刷新",
            "data": {
                "enabled": self._enabled,
                "tree_plugin_id": self._tree_plugin_id,
                "tree_available": available,
                "tree_detail": detail,
                "output_dir": meta.get("output_dir") or self._output_dir,
                "status": meta.get("status") or "idle",
                "last_started_at": _format_datetime(meta.get("last_started_at")),
                "last_finished_at": _format_datetime(meta.get("last_finished_at")),
                "last_success_at": _format_datetime(meta.get("last_success_at")),
                "last_event_at": _format_datetime(meta.get("last_event_at")),
                "target_count": int(meta.get("target_count") or 0),
                "record_count": int(meta.get("record_count") or 0),
                "dir_count": int(meta.get("dir_count") or 0),
                "added_count": int(meta.get("added_count") or 0),
                "updated_count": int(meta.get("updated_count") or 0),
                "removed_count": int(meta.get("removed_count") or 0),
                "skipped_count": int(meta.get("skipped_count") or 0),
                "error_count": int(meta.get("error_count") or 0),
                "duration_seconds": float(meta.get("duration_seconds") or 0.0),
                "error": meta.get("error") or "",
                "cleanup": self._cleanup,
                "event_watch": self._event_watch,
                "auto_sync": self._auto_sync,
            },
        }

    def api_records_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """按页返回 STRM 记录。"""
        requested = _clamp_int(
            (payload or {}).get("page"), 1, 10_000_000, 1
        )
        try:
            with self.get_database().session() as session:
                total = int(
                    session.scalar(select(func.count()).select_from(StrmRecord)) or 0
                )
                pages = max(1, math.ceil(total / self._page_size))
                page = min(max(1, requested), pages)
                rows = session.scalars(
                    select(StrmRecord)
                    .order_by(StrmRecord.rel_path, StrmRecord.id)
                    .offset((page - 1) * self._page_size)
                    .limit(self._page_size)
                ).all()
                items = [
                    {
                        "rel_path": row.rel_path,
                        "source_file_id": row.source_file_id or "",
                        "source_path": row.source_path or "",
                        "size_text": _format_size(row.size),
                        "updated_at": _format_datetime(row.updated_at),
                        "url": row.url or "",
                    }
                    for row in rows
                ]
        except SQLAlchemyError:
            logger.exception("【GDriveStrm】读取 STRM 记录失败")
            return {"success": False, "message": "读取记录失败，请查看日志", "data": None}
        self._page = page
        return {
            "success": True,
            "message": f"第 {page} / {pages} 页",
            "data": {"page": page, "pages": pages, "total": total, "items": items},
        }

    def api_clear(self) -> Dict[str, Any]:
        """清空记录与统计，不删除磁盘文件；与同步任务互斥。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        if not self._sync_lock.acquire(blocking=False):
            return {"success": False, "message": "同步进行中，暂不能清空", "data": None}
        try:
            try:
                with self.get_database().session() as session:
                    result = session.execute(delete(StrmRecord))
                    deleted = max(0, int(result.rowcount or 0))
                    meta = session.get(StrmMeta, 1)
                    if meta is not None:
                        meta.status = "idle"
                        meta.target_count = 0
                        meta.record_count = 0
                        meta.dir_count = 0
                        meta.added_count = 0
                        meta.updated_count = 0
                        meta.removed_count = 0
                        meta.skipped_count = 0
                        meta.error_count = 0
                        meta.duration_seconds = 0.0
                        meta.error = ""
                        meta.last_started_at = None
                        meta.last_finished_at = None
                        meta.last_success_at = None
                        meta.last_event_at = None
                        meta.updated_at = datetime.now()
                    session.commit()
            except SQLAlchemyError:
                logger.exception("【GDriveStrm】清空记录失败")
                return {"success": False, "message": "清空失败，请查看日志", "data": None}
            self._page = 1
            logger.info(f"【GDriveStrm】已清空 STRM 记录：{deleted} 条")
            return {
                "success": True,
                "message": f"已清空 {deleted} 条记录（磁盘文件未删除）",
                "data": {"deleted": deleted},
            }
        finally:
            try:
                self._sync_lock.release()
            except RuntimeError:
                pass

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------
    def get_page(self) -> Optional[List[dict]]:
        """返回状态、按钮与记录分页表格。"""
        contents: List[dict] = []
        if not self._enabled:
            contents.append(self._alert("warning", "插件未启用，以下为最近一次状态。"))

        available, detail = self._source_status()
        contents.append(self._alert(
            "success" if available else "error",
            f"目录树（{self._tree_plugin_id}）：{detail}。",
        ))

        meta = self._load_meta()
        status = meta.get("status") or "idle"
        status_text = {
            "idle": "空闲",
            "running": "同步中",
            "success": "成功",
            "failed": "失败",
            "cancelled": "已中止",
        }.get(status, status)
        stats_text = (
            f"状态：{status_text}；输出目录：{meta.get('output_dir') or self._output_dir}；"
            f"最后成功：{_format_datetime(meta.get('last_success_at'))}；"
            f"目标 {int(meta.get('target_count') or 0)}，"
            f"记录 {int(meta.get('record_count') or 0)}，"
            f"目录 {int(meta.get('dir_count') or 0)}；"
            f"新增 {int(meta.get('added_count') or 0)}，"
            f"更新 {int(meta.get('updated_count') or 0)}，"
            f"删除 {int(meta.get('removed_count') or 0)}，"
            f"跳过 {int(meta.get('skipped_count') or 0)}；"
            f"耗时 {float(meta.get('duration_seconds') or 0):.1f}s。"
        )
        contents.append(self._alert("info", stats_text))
        contents.append(self._alert(
            "info",
            f"事件增量：{'开启' if self._event_watch else '关闭'}；"
            f"失效清理：{'开启' if self._cleanup else '关闭'}；"
            f"定时同步：{'开启' if self._auto_sync else '关闭'}"
            f"（{self._sync_interval_minutes} 分钟）。"
        ))
        if meta.get("error"):
            contents.append(self._alert(
                "error",
                f"最近错误（{_format_datetime(meta.get('last_finished_at'))}）："
                f"{meta.get('error')}",
            ))

        page, pages, total, items = self._page_records(self._page)
        self._page = page
        buttons: List[dict] = [
            self._page_button("立即同步", "mdi-sync", "/sync"),
            self._page_button("全量同步", "mdi-database-refresh", "/sync/full"),
            self._page_button("清空记录", "mdi-delete-sweep", "/clear"),
        ]
        contents.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"},
            "content": buttons,
        })

        if items:
            headers = ["STRM 路径", "源路径", "大小", "更新时间"]
            ths = [{"component": "th", "text": header} for header in headers]
            rows = []
            for item in items:
                rows.append({
                    "component": "tr",
                    "content": [
                        self._text_cell(item.get("rel_path") or "-"),
                        self._text_cell(
                            item.get("source_path") or "-",
                            title=item.get("source_file_id"),
                        ),
                        self._text_cell(item.get("size_text") or "-"),
                        self._text_cell(item.get("updated_at") or "-"),
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
                "content": [self._alert("info", "暂无 STRM 记录，点击“立即同步”开始。")],
            })

        contents.append({
            "component": "div",
            "props": {"class": "d-flex align-center justify-center ga-2 mt-2 flex-shrink-0"},
            "content": [
                self._page_button(
                    "上一页", "mdi-chevron-left", "/records/page",
                    {"page": self._page - 1}, disabled=self._page <= 1,
                ),
                {
                    "component": "span",
                    "props": {"class": "text-body-2"},
                    "text": f"第 {self._page} / {pages} 页（共 {total} 条）",
                },
                self._page_button(
                    "下一页", "mdi-chevron-right", "/records/page",
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

    def _page_records(
        self, page: int
    ) -> Tuple[int, int, int, List[Dict[str, Any]]]:
        """读取分页记录；失败时返回空页。"""
        try:
            with self.get_database().session() as session:
                total = int(
                    session.scalar(select(func.count()).select_from(StrmRecord)) or 0
                )
                pages = max(1, math.ceil(total / self._page_size))
                current = min(max(1, int(page or 1)), pages)
                rows = session.scalars(
                    select(StrmRecord)
                    .order_by(StrmRecord.rel_path, StrmRecord.id)
                    .offset((current - 1) * self._page_size)
                    .limit(self._page_size)
                ).all()
                items = [
                    {
                        "rel_path": row.rel_path,
                        "source_file_id": row.source_file_id or "",
                        "source_path": row.source_path or "",
                        "size_text": _format_size(row.size),
                        "updated_at": _format_datetime(row.updated_at),
                    }
                    for row in rows
                ]
                return current, pages, total, items
        except SQLAlchemyError:
            logger.debug("【GDriveStrm】读取分页记录失败", exc_info=True)
            return 1, 1, 0, []

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
        event: Dict[str, Any] = {
            "api": f"plugin/{self.__class__.__name__}{path}",
            "method": "post",
        }
        if params:
            event["params"] = params
        button: Dict[str, Any] = {
            "component": "VBtn",
            "text": text,
            "props": {
                "color": "primary",
                "variant": "tonal",
                "size": "small",
                "prepend-icon": icon,
                "disabled": bool(disabled),
            },
            "events": {"click": event},
        }
        return button

    @staticmethod
    def _text_cell(text: Any, title: Any = None) -> dict:
        """构建截断显示的文本单元格。"""
        value = str(text) if text is not None else "-"
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
