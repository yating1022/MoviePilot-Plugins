"""监控本地目录，新文件先多次探测 115 秒传，全部未命中后再普通上传。"""

from __future__ import annotations

import hashlib
import math
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

from fastapi import Body
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.plugins import _PluginBase
from app.schemas.file import FileItem
from app.sdk.database import plugin_declarative_base
from app.sdk.logging import logger
from app.sdk.scheduler import add_plugin_once_job, remove_plugin_once_job
from app.sdk.services import StorageHelper

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - 宿主未安装 watchdog 时退化为定时扫描
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[assignment]

U115_CHUNK = 1024 * 1024
U115_STORAGE = "u115"
ONCE_JOB_ID = "U115AutoUpload_Once"

_PluginDBBase = plugin_declarative_base()


class U115UploadRecord(_PluginDBBase):
    """115 上传记录，用于防止重复上传并支撑详情页列表。"""

    __tablename__ = "upload_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    local_path: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    watch_dir: Mapped[str] = mapped_column(String(512), default="")
    relative_path: Mapped[str] = mapped_column(String(512), default="")
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime: Mapped[int] = mapped_column(BigInteger, default=0)
    remote_path: Mapped[str] = mapped_column(String(1024), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    method: Mapped[str] = mapped_column(String(32), default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


def _format_size(size: int) -> str:
    """把字节数格式化为易读文本。"""
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _sha1_pair(path: Path, prefix_size: int = 128 * 1024 * 1024) -> Tuple[str, str]:
    """
    一次读取文件，同时计算全量 SHA1 和前 prefix_size 字节 SHA1。

    115 秒传使用这两个值：全量 SHA1 作为 fileid，前 128MB SHA1 作为 preid。
    """
    full = hashlib.sha1()
    prefix = hashlib.sha1()
    remaining = prefix_size
    with open(path, "rb") as file:
        while True:
            chunk = file.read(U115_CHUNK)
            if not chunk:
                break
            full.update(chunk)
            if remaining > 0:
                part = chunk[:remaining]
                if part:
                    prefix.update(part)
                    remaining -= len(part)
    return full.hexdigest(), prefix.hexdigest()


def _get_115_api():
    """取得宿主 115 适配器实例；当前版本不兼容时返回 None。"""
    try:
        from app.modules.filemanager.storages.u115 import U115Pan
    except ImportError as err:
        logger.error(f"当前 MoviePilot 没有 115 存储适配器：{err}")
        return None
    api = U115Pan()
    if not all(
        hasattr(api, name)
        for name in ("_request_api", "get_item", "get_folder", "upload")
    ):
        logger.error("115 适配器接口已变化，插件需要适配当前 MoviePilot 版本")
        return None
    logger.debug("【115自动上传】已取得宿主 115 适配器实例")
    return api


def probe_rapid_upload_once(
    api,
    target_dir: FileItem,
    local_path: Path,
    new_name: Optional[str] = None,
    *,
    file_sha1: Optional[str] = None,
    file_preid: Optional[str] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Optional[FileItem]:
    """只执行一次 115 秒传探测，不触发普通上传。"""
    if diagnostics is None:
        diagnostics = {}
    if not target_dir.fileid:
        raise ValueError("115 目标目录缺少 fileid")

    target_name = new_name or local_path.name
    target_path = Path(target_dir.path) / target_name
    file_size = local_path.stat().st_size
    if file_sha1 is None or file_preid is None:
        computed_sha1, computed_preid = _sha1_pair(local_path)
        file_sha1 = file_sha1 or computed_sha1
        file_preid = file_preid or computed_preid

    init_data = {
        "file_name": target_name,
        "file_size": file_size,
        "target": f"U_1_{target_dir.fileid}",
        "fileid": file_sha1,
        "preid": file_preid,
    }
    init_resp = api._request_api(
        "POST", "/open/upload/init", data=init_data, retry_limit=0
    )
    if not isinstance(init_resp, dict):
        diagnostics.update(reason="init_failed", code=None)
        logger.debug("【115秒传】初始化请求失败：响应不是字典")
        return None
    if not init_resp.get("state"):
        diagnostics.update(reason="init_failed", code=init_resp.get("code"))
        logger.debug(
            f"【115秒传】初始化失败：state={init_resp.get('state')} "
            f"code={init_resp.get('code')} message={init_resp.get('message')}"
        )
        return None
    init_result = init_resp.get("data") or {}

    # 115 对部分文件要求二次内容校验，这仍属于秒传探测。
    if init_result.get("code") in (700, 701) and init_result.get("sign_check"):
        start_text, end_text = str(init_result["sign_check"]).split("-", 1)
        start, end = int(start_text), int(end_text)
        with open(local_path, "rb") as file:
            file.seek(start)
            sign_val = hashlib.sha1(file.read(end - start + 1)).hexdigest().upper()
        init_data.update(
            {
                "pick_code": init_result.get("pick_code"),
                "sign_key": init_result.get("sign_key"),
                "sign_val": sign_val,
            }
        )
        init_resp = api._request_api(
            "POST", "/open/upload/init", data=init_data, retry_limit=0
        )
        if not isinstance(init_resp, dict):
            diagnostics.update(reason="sign_failed", code=None)
            logger.debug("【115秒传】二次认证请求失败：响应不是字典")
            return None
        if not init_resp.get("state"):
            diagnostics.update(reason="sign_failed", code=init_resp.get("code"))
            logger.debug(
                f"【115秒传】二次认证失败：state={init_resp.get('state')} "
                f"code={init_resp.get('code')} message={init_resp.get('message')}"
            )
            return None
        init_result = init_resp.get("data") or {}

    diagnostics.update(
        code=init_result.get("code"),
        status=init_result.get("status"),
    )
    logger.debug(
        f"【115秒传】code={init_result.get('code')} "
        f"status={init_result.get('status')} "
        f"sign_check={bool(init_result.get('sign_check'))}"
    )
    if init_result.get("status") != 2:
        diagnostics["reason"] = "not_rapid"
        return None

    file_id = init_result.get("file_id")
    if file_id:
        info_resp = api._request_api(
            "GET",
            "/open/folder/get_info",
            "data",
            params={"file_id": int(file_id)},
            retry_limit=0,
        )
        if isinstance(info_resp, dict):
            diagnostics.update(
                reason="hit",
                fileid=str(info_resp.get("file_id") or ""),
            )
            is_file = info_resp.get("file_category") == "1"
            return FileItem(
                storage=U115_STORAGE,
                fileid=str(info_resp["file_id"]),
                path=target_path.as_posix() + ("" if is_file else "/"),
                type="file" if is_file else "dir",
                name=info_resp["file_name"],
                basename=Path(info_resp["file_name"]).stem,
                extension=Path(info_resp["file_name"]).suffix[1:]
                if is_file
                else None,
                pickcode=info_resp.get("pick_code"),
                size=info_resp.get("size_byte") if is_file else None,
                modify_time=info_resp.get("utime"),
            )

    for _ in range(3):
        item = api.get_item(target_path)
        if item:
            diagnostics["reason"] = "hit"
            return item
        time.sleep(1)
    diagnostics["reason"] = "index_timeout"
    return None


def rapid_upload_with_retry(
    api,
    target_dir: FileItem,
    local_path: Path,
    new_name: Optional[str] = None,
    *,
    retry_times: int = 3,
    retry_interval: float = 5.0,
    hash_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[FileItem], str]:
    """
    先探测 retry_times 次秒传，全部未命中后再调用宿主普通上传。

    :return: (上传后的文件项或 None, 方式 rapid/normal/failed)
    """
    stat = local_path.stat()
    cache_key = str(local_path)
    cached = hash_cache.get(cache_key) if hash_cache is not None else None

    # 115 秒传用的是 SHA1：全文件 SHA1 作为 fileid，前 128MB SHA1 作为 preid。
    # 第一次探测前必须算出这两个值；算完立即缓存，后续探测直接复用，不再读文件。
    if (
        isinstance(cached, dict)
        and cached.get("sha1")
        and cached.get("preid")
        and cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
    ):
        file_sha1 = cached["sha1"]
        file_preid = cached["preid"]
        logger.debug(f"【115秒传】复用 SHA1 缓存：{local_path}")
    else:
        hash_started = time.monotonic()
        logger.debug(f"【115秒传】开始计算 SHA1：{local_path}")
        file_sha1, file_preid = _sha1_pair(local_path)
        hash_elapsed = time.monotonic() - hash_started
        if hash_elapsed >= 5:
            logger.info(
                f"【115秒传】SHA1 计算完成：{local_path} "
                f"({stat.st_size} 字节)，耗时 {hash_elapsed:.1f}s"
            )
        else:
            logger.debug(
                f"【115秒传】SHA1 计算完成：{local_path}，耗时 {hash_elapsed:.1f}s"
            )
        if hash_cache is not None:
            if len(hash_cache) >= 1000:
                hash_cache.pop(next(iter(hash_cache)), None)
            hash_cache[cache_key] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha1": file_sha1,
                "preid": file_preid,
            }

    for attempt in range(1, retry_times + 1):
        diagnostics: Dict[str, Any] = {}
        item = probe_rapid_upload_once(
            api,
            target_dir,
            local_path,
            new_name,
            file_sha1=file_sha1,
            file_preid=file_preid,
            diagnostics=diagnostics,
        )
        if item:
            logger.info(
                f"【115秒传】第 {attempt}/{retry_times} 次探测命中：{item.path}"
            )
            return item, "rapid"

        logger.info(
            f"【115秒传】第 {attempt}/{retry_times} 次未命中："
            f"reason={diagnostics.get('reason', 'unknown')} "
            f"code={diagnostics.get('code')} "
            f"status={diagnostics.get('status')}"
        )
        # 这里的 sleep 在本次探测失败返回之后才执行，
        # 计时起点是失败时刻，不是探测发起时刻。
        if attempt < retry_times:
            logger.info(f"【115秒传】等待 {retry_interval:g}s 后重试")
            time.sleep(retry_interval)

    logger.info("【115秒传】探测次数已用尽，回退到宿主普通上传")
    uploaded = api.upload(target_dir, local_path, new_name)
    if uploaded:
        logger.info(f"【115秒传】宿主普通上传返回：{uploaded.path}")
        return uploaded, "normal"
    logger.error("【115秒传】宿主普通上传失败")
    return None, "failed"


class _WatchdogHandler(FileSystemEventHandler):
    """把 watchdog 文件事件转成插件内部队列投递。"""

    def __init__(self, callback) -> None:
        """保存文件事件回调。"""
        super().__init__()
        self._callback = callback

    def on_created(self, event) -> None:
        """文件创建时投递。"""
        if not event.is_directory:
            self._callback(Path(event.src_path))

    def on_modified(self, event) -> None:
        """文件修改时投递，由队列去重。"""
        if not event.is_directory:
            self._callback(Path(event.src_path))

    def on_closed(self, event) -> None:
        """文件关闭时投递，部分平台仅提供 close 事件。"""
        if not event.is_directory:
            self._callback(Path(event.src_path))

    def on_moved(self, event) -> None:
        """文件移动/重命名完成后投递目标路径。"""
        if not event.is_directory:
            self._callback(Path(event.dest_path))


class U115AutoUpload(_PluginBase):
    """监控本地目录，新增文件先多次探测 115 秒传，失败后普通上传。"""

    plugin_name = "115 目录秒传上传"
    plugin_desc = "实时监控本地目录，新文件先探测 115 秒传，重试 x 次后回退普通上传。"
    plugin_version = "1.1.1"
    plugin_label = "网盘"
    plugin_author = "local"
    plugin_config_prefix = "u115autoupload_"
    plugin_order = 100
    auth_level = 1

    _ignore_suffixes = {
        ".part",
        ".tmp",
        ".temp",
        ".!qb",
        ".aria2",
        ".downloading",
        ".crdownload",
    }

    _enabled = False
    _watch_dir = ""
    _target_dir = ""
    _recursive = True
    _scan_interval = 60
    _rapid_retry = 3
    _rapid_interval = 5.0
    _stable_seconds = 30
    _delete_after_upload = False
    _upload_existing = False
    _watchdog_enabled = True
    _onlyonce = False
    _page = 1
    _page_size = 10

    def __init__(self) -> None:
        """初始化扫描锁、数据库缓存和实时事件队列。"""
        super().__init__()
        self._scan_lock = threading.Lock()
        self._upload_lock = threading.Lock()
        self._inflight_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._scan_state: Dict[str, Tuple[int, float]] = {}
        self._hash_cache: Dict[str, Dict[str, Any]] = {}
        self._inflight: set[str] = set()
        self._pending_events: set[str] = set()
        self._event_queue: queue.Queue[str] = queue.Queue(maxsize=10000)
        self._stop_event = threading.Event()
        self._observer = None
        self._event_thread: Optional[threading.Thread] = None
        self._last_scan = ""
        self._extension_set: set[str] = set()

    def init_plugin(self, config: dict = None) -> None:
        """读取配置，初始化记录表、目录状态和实时监控。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        raw_watch_dir = str(config.get("watch_dir") or "").strip()
        self._watch_dir = (
            str(Path(raw_watch_dir).expanduser().absolute()) if raw_watch_dir else ""
        )
        self._target_dir = str(config.get("target_dir") or "").strip()
        self._recursive = bool(config.get("recursive", True))
        self._scan_interval = max(10, int(config.get("scan_interval") or 60))
        self._rapid_retry = max(0, int(config.get("rapid_retry") or 0))
        self._rapid_interval = max(0.0, float(config.get("rapid_interval") or 5))
        self._stable_seconds = max(0, int(config.get("stable_seconds") or 30))
        self._delete_after_upload = bool(config.get("delete_after_upload"))
        self._upload_existing = bool(config.get("upload_existing"))
        self._watchdog_enabled = bool(config.get("watchdog", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._extension_set = self.__parse_extensions(config.get("extensions"))
        self._scan_state = {}
        self._last_scan = ""

        self.__ensure_table()

        if self._enabled:
            logger.info(
                f"【115自动上传】插件已启用 v{self.plugin_version}："
                f"监控目录={self._watch_dir or '未设置'}，"
                f"目标目录={self._target_dir or '未设置'}，"
                f"扫描间隔={self._scan_interval}s，"
                f"秒传探测={self._rapid_retry}次/间隔{self._rapid_interval:g}s，"
                f"稳定时间={self._stable_seconds}s，"
                f"递归={self._recursive}，实时监控={self._watchdog_enabled}，"
                f"删除本地={self._delete_after_upload}，"
                f"上传已有={self._upload_existing}"
            )
            if not self._watch_dir or not self._target_dir:
                logger.warn("【115自动上传】已启用，但监控目录或 115 目标目录未设置")
            elif Path(self._watch_dir).is_dir():
                self.__migrate_legacy(Path(self._watch_dir))
                self.__initialize_watch_dir(Path(self._watch_dir))
                self.__ensure_runtime()
            else:
                logger.error(f"【115自动上传】监控目录不存在：{self._watch_dir}")

            if self._onlyonce:
                self.__schedule_once()
        else:
            logger.info("【115自动上传】插件未启用")

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """注册扫描、重试、删除和清理记录 API。"""
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描一次监控目录",
            },
            {
                "path": "/records/retry",
                "endpoint": self.api_retry_record,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试一条上传记录",
            },
            {
                "path": "/records/delete",
                "endpoint": self.api_delete_record,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除一条上传记录",
            },
            {
                "path": "/records/clear_success",
                "endpoint": self.api_clear_success,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理全部成功记录",
            },
            {
                "path": "/records/page",
                "endpoint": self.api_records_page,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换记录列表页码",
            },
        ]

    def get_database_models(self) -> List[Type[Any]]:
        """声明插件自有上传记录表。"""
        return [U115UploadRecord]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回插件配置表单和默认值。"""
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
                            "text": "实时监控使用 watchdog；网络盘可能收不到事件，仍由定时扫描兜底。勾选“立即运行一次”并启用插件后保存，会马上扫描一次。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "保存后立即运行一次",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "watchdog",
                                            "label": "启用实时监控",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "watch_dir",
                                            "label": "本地监控目录",
                                            "placeholder": "/downloads/watch",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "target_dir",
                                            "label": "115 目标目录",
                                            "placeholder": "/我的备份",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "recursive",
                                            "label": "递归子目录并保留目录结构",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "delete_after_upload",
                                            "label": "上传成功后删除本地文件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "upload_existing",
                                            "label": "首次启用时也上传已有文件",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "scan_interval",
                                            "label": "扫描间隔（秒）",
                                            "type": "number",
                                            "min": 10,
                                            "step": 1,
                                            "hint": "定时兜底扫描的间隔",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "rapid_retry",
                                            "label": "秒传探测次数",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hint": "每次上传前最多探测几次秒传",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "rapid_interval",
                                            "label": "失败后重试间隔（秒）",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hint": "本次探测失败返回后，等待这么久再探测下一次",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "stable_seconds",
                                            "label": "文件稳定时间（秒）",
                                            "type": "number",
                                            "min": 0,
                                            "step": 1,
                                            "hint": "文件修改时间超过这个时间才会上传，避免上传未写完的文件",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "extensions",
                                            "label": "只处理这些后缀（逗号分隔，留空为全部）",
                                            "placeholder": ".mkv,.mp4,.iso",
                                            "hint": "例如 .mkv,.mp4,.iso；留空表示全部文件",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "watchdog": True,
            "watch_dir": "",
            "target_dir": "",
            "recursive": True,
            "scan_interval": 60,
            "rapid_retry": 3,
            "rapid_interval": 5,
            "stable_seconds": 30,
            "extensions": "",
            "delete_after_upload": False,
            "upload_existing": False,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回固定高度、可分页的上传记录列表和操作页面。"""
        summary = self.__record_summary()
        success = summary.get("success", 0)
        failed = summary.get("failed", 0)
        pending = summary.get("pending", 0)
        total = success + failed + pending
        pages = max(1, math.ceil(total / self._page_size))
        self._page = min(max(1, self._page), pages)
        records = self.__list_records(
            limit=self._page_size,
            offset=(self._page - 1) * self._page_size,
        )

        contents: List[dict] = []
        if not self._enabled:
            contents.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "class": "mb-3 flex-shrink-0",
                        "text": "插件未启用，以下为历史记录。",
                    },
                }
            )
        contents.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "class": "mb-3 flex-shrink-0",
                    "text": (
                        f"共 {total} 条记录：成功 {success}，失败 {failed}，"
                        f"待处理 {pending}。第 {self._page}/{pages} 页，"
                        f"每页 {self._page_size} 条。"
                    ),
                },
            }
        )
        contents.append(
            {
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "class": "mb-3 flex-shrink-0",
                    "text": "删除记录不会删除网盘文件；如果本地文件仍在，下一轮扫描会重新上传。重试在后台执行，稍后刷新查看结果。",
                },
            }
        )
        contents.append(
            {
                "component": "div",
                "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"},
                "content": [
                    self.__page_button("立即扫描", "mdi-play", "/scan"),
                    self.__page_button(
                        "清理成功记录", "mdi-broom", "/records/clear_success"
                    ),
                ],
            }
        )

        if records:
            headers = [
                "状态",
                "本地文件",
                "大小",
                "远端路径",
                "方式",
                "次数",
                "错误",
                "更新时间",
                "操作",
            ]
            ths = [{"component": "th", "text": header} for header in headers]
            rows = []
            for record in records:
                rows.append(
                    {
                        "component": "tr",
                        "content": [
                            {
                                "component": "td",
                                "content": [
                                    self.__status_chip(
                                        str(record.get("status") or "")
                                    )
                                ],
                            },
                            self.__text_cell(
                                record.get("relative_path")
                                or record.get("local_path"),
                                title=record.get("local_path"),
                            ),
                            self.__text_cell(
                                _format_size(int(record.get("file_size") or 0))
                            ),
                            self.__text_cell(
                                record.get("remote_path") or "-",
                                title=record.get("remote_path"),
                            ),
                            self.__text_cell(
                                self.__method_text(record.get("method"))
                            ),
                            self.__text_cell(str(record.get("retry_count") or 0)),
                            self.__text_cell(
                                record.get("error") or "-",
                                title=record.get("error"),
                            ),
                            self.__text_cell(record.get("updated_at") or "-"),
                            {
                                "component": "td",
                                "content": self.__record_actions(record),
                            },
                        ],
                    }
                )

            contents.append(
                {
                    "component": "div",
                    "props": {
                        "class": "flex-grow-1",
                        "style": "min-height: 0; overflow: auto;",
                    },
                    "content": [
                        {
                            "component": "VTable",
                            "props": {"density": "compact", "hover": True},
                            "content": [
                                {
                                    "component": "thead",
                                    "props": {
                                        "style": "position: sticky; top: 0; z-index: 1; background: rgb(var(--v-theme-surface));",
                                    },
                                    "content": [
                                        {"component": "tr", "content": ths}
                                    ],
                                },
                                {"component": "tbody", "content": rows},
                            ],
                        }
                    ],
                }
            )
        else:
            contents.append(
                {
                    "component": "div",
                    "props": {
                        "class": "d-flex align-center justify-center flex-grow-1",
                        "style": "min-height: 0;",
                    },
                    "content": [
                        {
                            "component": "VAlert",
                            "props": {
                                "type": "info",
                                "variant": "tonal",
                                "text": "暂无处理记录。",
                            },
                        }
                    ],
                }
            )

        contents.append(
            {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center ga-2 mt-2 flex-shrink-0",
                },
                "content": [
                    self.__page_button(
                        "上一页",
                        "mdi-chevron-left",
                        "/records/page",
                        {"page": self._page - 1},
                        disabled=self._page <= 1,
                    ),
                    {
                        "component": "span",
                        "props": {"class": "text-body-2"},
                        "text": f"第 {self._page} / {pages} 页",
                    },
                    self.__page_button(
                        "下一页",
                        "mdi-chevron-right",
                        "/records/page",
                        {"page": self._page + 1},
                        disabled=self._page >= pages,
                    ),
                ],
            }
        )

        return [
            {
                "component": "div",
                "props": {
                    "class": "d-flex flex-column",
                    "style": "height: 72vh; min-height: 520px; max-height: 800px; padding-bottom: 84px; box-sizing: border-box;",
                },
                "content": contents,
            }
        ]

    def get_service(self) -> Optional[List[Dict[str, Any]]]:
        """注册定时兜底扫描服务。"""
        if not self._enabled or not self._watch_dir or not self._target_dir:
            return []
        logger.debug(f"【115自动上传】注册定时兜底扫描：每 {self._scan_interval}s")
        return [
            {
                "id": "U115AutoUpload",
                "name": "115 目录秒传上传（定时兜底）",
                "trigger": "interval",
                "func": self.scan_once,
                "kwargs": {"seconds": self._scan_interval},
            }
        ]

    def stop_service(self) -> None:
        """停止实时监控、事件线程和一次性任务。"""
        self._stop_event.set()
        self.__stop_watchdog()
        thread = self._event_thread
        self._event_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warn("【115自动上传】事件处理线程未在 5 秒内退出")
        try:
            remove_plugin_once_job(self.__class__.__name__, ONCE_JOB_ID)
        except Exception:
            logger.debug("【115自动上传】取消一次性扫描任务失败", exc_info=True)
        self._scan_state.clear()
        with self._event_lock:
            self._pending_events.clear()
        with self._inflight_lock:
            self._inflight.clear()
        self._stop_event = threading.Event()
        self._event_queue = queue.Queue(maxsize=10000)
        self._last_scan = ""

    def api_scan(self) -> Dict[str, Any]:
        """手动触发一次扫描，立即返回。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        logger.info("【115自动上传】手动触发扫描")
        threading.Thread(target=self.scan_once, daemon=True).start()
        return {"success": True, "message": "扫描已触发"}

    def api_retry_record(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """把一条失败记录重新加入上传队列。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        record_id = int(payload.get("id") or 0)
        ok, message, local_path = self.__mark_record_pending(record_id)
        if not ok:
            logger.warn(f"【115自动上传】重试记录 {record_id} 失败：{message}")
            return {"success": False, "message": message}
        logger.info(f"【115自动上传】重试记录 {record_id}：{local_path}")
        self.__enqueue_event(Path(local_path))
        return {"success": True, "message": message}

    def api_delete_record(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """删除一条上传记录，不删除网盘文件。"""
        record_id = int(payload.get("id") or 0)
        if record_id <= 0:
            return {"success": False, "message": "记录 ID 无效"}
        try:
            with self.get_database().session() as session:
                record = session.get(U115UploadRecord, record_id)
                if record is None:
                    return {"success": False, "message": "记录不存在"}
                session.delete(record)
                session.commit()
            logger.info(f"【115自动上传】已删除上传记录：id={record_id}")
            return {"success": True, "message": "记录已删除"}
        except Exception:
            logger.exception(f"【115自动上传】删除记录 {record_id} 失败")
            return {"success": False, "message": "数据库操作失败"}

    def api_clear_success(self) -> Dict[str, Any]:
        """清理全部成功记录。"""
        try:
            with self.get_database().session() as session:
                result = session.execute(
                    delete(U115UploadRecord).where(
                        U115UploadRecord.status == "success"
                    )
                )
                session.commit()
                count = max(0, result.rowcount or 0)
            logger.info(f"【115自动上传】已清理 {count} 条成功记录")
            return {"success": True, "message": f"已清理 {count} 条成功记录"}
        except Exception:
            logger.exception("【115自动上传】清理成功记录失败")
            return {"success": False, "message": "数据库操作失败"}

    def api_records_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """切换记录列表页码，页面重新加载后生效。"""
        try:
            page = int(payload.get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        self._page = max(1, page)
        logger.debug(f"【115自动上传】记录列表切换到第 {self._page} 页")
        return {"success": True, "message": f"第 {self._page} 页"}

    def scan_once(self) -> None:
        """执行一次目录扫描；上一轮未结束时直接跳过。"""
        if not self._enabled:
            return
        if not self._scan_lock.acquire(blocking=False):
            logger.info("【115自动上传】上一轮扫描仍在执行，跳过本轮")
            return
        try:
            self.__scan()
        except Exception:
            logger.exception("【115自动上传】扫描异常")
        finally:
            self._scan_lock.release()

    def __scan(self) -> None:
        """扫描目录并上传尚未成功的文件。"""
        watch_dir = Path(self._watch_dir) if self._watch_dir else None
        if watch_dir is None or not watch_dir.is_dir():
            logger.error(f"【115自动上传】监控目录不存在：{self._watch_dir}")
            return
        if not self._target_dir:
            logger.error("【115自动上传】未设置 115 目标目录")
            return

        conf = StorageHelper().get_storage(U115_STORAGE)
        if not conf or not conf.config:
            logger.error("【115自动上传】主程序未配置 115，请先在 设置 -> 存储 中登录")
            return
        logger.debug("【115自动上传】已读取主程序 115 存储配置")

        self.__ensure_runtime()
        self.__migrate_legacy(watch_dir)
        self.__initialize_watch_dir(watch_dir)

        started = time.monotonic()
        candidate_count = 0
        new_count = 0
        success_count = 0
        fail_count = 0
        skip_count = 0
        logger.info(f"【115自动上传】开始扫描：{watch_dir} -> {self._target_dir}")

        self._last_scan = time.strftime("%Y-%m-%d %H:%M:%S")
        for local_path in self.__iter_files(watch_dir):
            candidate_count += 1
            if not self.__is_ready(local_path):
                skip_count += 1
                logger.debug(f"【115自动上传】跳过未稳定文件：{local_path}")
                continue
            try:
                stat = local_path.stat()
            except OSError:
                skip_count += 1
                logger.debug(f"【115自动上传】跳过无法读取的文件：{local_path}")
                continue
            record = self.__get_record(str(local_path))
            if (
                record
                and record["status"] in {"success", "skipped"}
                and record["file_size"] == stat.st_size
                and record["mtime"] == int(stat.st_mtime)
            ):
                skip_count += 1
                logger.debug(f"【115自动上传】跳过已处理文件：{local_path}")
                continue

            new_count += 1
            result = self.__process_path(local_path, watch_dir, "scan")
            if result == "success":
                success_count += 1
            elif result == "failed":
                fail_count += 1
            else:
                skip_count += 1

        logger.info(
            f"【115自动上传】扫描完成：耗时 {time.monotonic() - started:.1f}s，"
            f"候选 {candidate_count}，新文件/变化 {new_count}，成功 {success_count}，"
            f"失败 {fail_count}，跳过 {skip_count}"
        )

    def __process_path(self, local_path: Path, watch_dir: Path, source: str) -> str:
        """
        处理一个文件：查重、解析目标目录、秒传探测、普通上传回退并写记录。

        :return: success / failed / skipped
        """
        if not self._enabled:
            return "skipped"
        key = str(local_path)
        with self._inflight_lock:
            if key in self._inflight:
                logger.debug(f"【115自动上传】文件正在处理中，跳过：{local_path}")
                return "skipped"
            self._inflight.add(key)
        try:
            try:
                stat = local_path.stat()
                relative_path = local_path.relative_to(watch_dir).as_posix()
            except (OSError, ValueError):
                return "skipped"

            record = self.__get_record(key)
            if (
                record
                and record["status"] in {"success", "skipped"}
                and record["file_size"] == stat.st_size
                and record["mtime"] == int(stat.st_mtime)
            ):
                logger.debug(f"【115自动上传】已处理且未变化，跳过：{local_path}")
                return "skipped"
            if record:
                if record["status"] == "success":
                    logger.info(f"【115自动上传】文件内容变化，重新上传：{local_path}")
                elif record["status"] == "failed":
                    logger.info(f"【115自动上传】重试失败文件：{local_path}")
                elif record["status"] == "pending":
                    logger.info(f"【115自动上传】处理待重试文件：{local_path}")
                elif record["status"] == "skipped":
                    logger.info(f"【115自动上传】已跳过文件发生变化，开始上传：{local_path}")
            else:
                logger.info(
                    f"【115自动上传】发现新文件（{source}）：{local_path}"
                    f"（{stat.st_size} 字节）"
                )

            remote_dir = self.__remote_dir(watch_dir, local_path)
            file_started = time.monotonic()
            api = _get_115_api()
            if api is None:
                self.__save_record(
                    local_path=key,
                    watch_dir=str(watch_dir),
                    relative_path=relative_path,
                    size=stat.st_size,
                    mtime=int(stat.st_mtime),
                    uploaded=None,
                    status="failed",
                    method="",
                    error="115 适配器不可用",
                )
                return "failed"

            try:
                with self._upload_lock:
                    target_item = self.__resolve_folder(api, remote_dir)
                    if target_item is None:
                        raise RuntimeError(f"无法获取/创建 115 目录：{remote_dir}")
                    uploaded, method = rapid_upload_with_retry(
                        api,
                        target_item,
                        local_path,
                        local_path.name,
                        retry_times=self._rapid_retry,
                        retry_interval=self._rapid_interval,
                        hash_cache=self._hash_cache,
                    )
            except Exception as err:
                logger.exception(f"【115自动上传】上传 {local_path} 失败")
                self.__save_record(
                    local_path=key,
                    watch_dir=str(watch_dir),
                    relative_path=relative_path,
                    size=stat.st_size,
                    mtime=int(stat.st_mtime),
                    uploaded=None,
                    status="failed",
                    method="",
                    error=str(err),
                )
                return "failed"

            if not uploaded:
                logger.error(f"【115自动上传】上传 {local_path} 失败，等待下次重试")
                self.__save_record(
                    local_path=key,
                    watch_dir=str(watch_dir),
                    relative_path=relative_path,
                    size=stat.st_size,
                    mtime=int(stat.st_mtime),
                    uploaded=None,
                    status="failed",
                    method="failed",
                    error="秒传和普通上传均未成功",
                )
                return "failed"

            self.__save_record(
                local_path=key,
                watch_dir=str(watch_dir),
                relative_path=relative_path,
                size=stat.st_size,
                mtime=int(stat.st_mtime),
                uploaded=uploaded,
                status="success",
                method=method,
                error="",
            )
            self._hash_cache.pop(key, None)
            logger.info(
                f"【115自动上传】上传成功：{local_path} -> {uploaded.path}，"
                f"方式={method}，耗时 {time.monotonic() - file_started:.1f}s"
            )

            if self._delete_after_upload:
                try:
                    local_path.unlink()
                    logger.info(f"【115自动上传】已删除本地文件：{local_path}")
                except Exception as err:
                    logger.warn(
                        f"【115自动上传】删除本地文件失败：{local_path} - {err}"
                    )
            return "success"
        finally:
            with self._inflight_lock:
                self._inflight.discard(key)

    def __ensure_runtime(self) -> None:
        """确保事件线程和 watchdog 在需要时启动。"""
        if not self._enabled:
            return
        if self._event_thread is None or not self._event_thread.is_alive():
            self.__start_event_worker()
        if self._watchdog_enabled and self._observer is None:
            self.__start_watchdog()

    def __start_event_worker(self) -> None:
        """启动 watchdog 事件处理线程。"""
        try:
            thread = threading.Thread(
                target=self.__event_worker,
                name="U115AutoUpload-events",
                daemon=True,
            )
            self._event_thread = thread
            thread.start()
            logger.info("【115自动上传】事件处理线程已启动")
        except Exception:
            logger.exception("【115自动上传】启动事件处理线程失败")

    def __start_watchdog(self) -> None:
        """启动 watchdog 目录观察器。"""
        if Observer is None:
            logger.warn("【115自动上传】watchdog 不可用，仅使用定时扫描")
            return
        watch_dir = Path(self._watch_dir) if self._watch_dir else None
        if watch_dir is None or not watch_dir.is_dir():
            logger.debug("【115自动上传】监控目录尚不存在，暂不启动实时监控")
            return
        try:
            handler = _WatchdogHandler(self.__enqueue_event)
            observer = Observer()
            observer.schedule(handler, str(watch_dir), recursive=self._recursive)
            observer.daemon = True
            observer.start()
            self._observer = observer
            logger.info(
                f"【115自动上传】实时监控已启动：{watch_dir}（递归={self._recursive}）"
            )
        except Exception:
            logger.exception("【115自动上传】实时监控启动失败，依赖定时扫描")

    def __stop_watchdog(self) -> None:
        """停止并释放 watchdog 观察器。"""
        observer = self._observer
        self._observer = None
        if observer is None:
            return
        try:
            observer.stop()
            observer.join(timeout=5)
            if observer.is_alive():
                logger.warn("【115自动上传】实时监控线程未在 5 秒内退出")
        except Exception:
            logger.exception("【115自动上传】停止实时监控失败")

    def __event_worker(self) -> None:
        """消费 watchdog 事件，等待文件稳定后调用统一处理流程。"""
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                key = self._event_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                path = Path(key)
                if not path.is_file():
                    logger.debug(f"【115自动上传】事件文件已不存在：{path}")
                    continue
                if not self.__wait_until_stable(path):
                    logger.info(f"【115自动上传】文件未稳定，交给定时扫描：{path}")
                    continue
                result = self.__process_path(path, Path(self._watch_dir), "watchdog")
                if result != "skipped":
                    logger.info(f"【115自动上传】实时处理结果：{path} -> {result}")
            except Exception:
                logger.exception(f"【115自动上传】实时处理失败：{key}")
            finally:
                with self._event_lock:
                    self._pending_events.discard(key)
                self._event_queue.task_done()
        logger.info("【115自动上传】事件处理线程已停止")

    def __enqueue_event(self, path: Path) -> None:
        """把文件事件去重后放入队列。"""
        if not self._enabled:
            return
        try:
            path.relative_to(Path(self._watch_dir))
        except ValueError:
            return
        if not self.__match_extension(path):
            return
        key = str(path)
        with self._event_lock:
            if key in self._pending_events:
                return
            self._pending_events.add(key)
        try:
            self._event_queue.put_nowait(key)
        except queue.Full:
            with self._event_lock:
                self._pending_events.discard(key)
            logger.warn("【115自动上传】实时事件队列已满，依赖定时扫描兜底")

    def __wait_until_stable(self, path: Path) -> bool:
        """等待文件大小和修改时间稳定，超时后交给定时扫描。"""
        deadline = time.monotonic() + max(self._stable_seconds, 1) + 60
        previous: Optional[Tuple[int, float]] = None
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            try:
                stat = path.stat()
            except OSError:
                return False
            current = (stat.st_size, stat.st_mtime)
            if current == previous and time.time() - stat.st_mtime >= self._stable_seconds:
                return True
            previous = current
            time.sleep(1)
        return False

    def __iter_files(self, watch_dir: Path):
        """按递归配置遍历候选文件。"""
        iterator = watch_dir.rglob("*") if self._recursive else watch_dir.iterdir()
        for path in iterator:
            if not path.is_file():
                continue
            if not self.__match_extension(path):
                continue
            yield path

    def __match_extension(self, path: Path) -> bool:
        """判断文件后缀是否需要处理。"""
        if path.name.startswith("."):
            return False
        suffix = path.suffix.lower()
        if suffix in self._ignore_suffixes:
            return False
        if not self._extension_set:
            return True
        return suffix in self._extension_set

    def __is_ready(self, path: Path) -> bool:
        """判断文件是否已写入稳定，避免上传正在复制的文件。"""
        try:
            stat = path.stat()
        except OSError:
            return False
        if time.time() - stat.st_mtime < self._stable_seconds:
            return False
        previous = self._scan_state.get(str(path))
        self._scan_state[str(path)] = (stat.st_size, stat.st_mtime)
        if previous and previous[0] != stat.st_size:
            return False
        return True

    def __remote_dir(self, watch_dir: Path, path: Path) -> str:
        """计算文件对应的 115 目录，递归时保留相对目录结构。"""
        target = self._target_dir.strip()
        if not target.startswith("/"):
            target = "/" + target
        target = target.rstrip("/") or "/"
        if not self._recursive:
            return target
        relative_parent = path.relative_to(watch_dir).parent
        if relative_parent.parts:
            return (Path(target) / relative_parent).as_posix()
        return target

    @staticmethod
    def __resolve_folder(api, remote_dir: str) -> Optional[FileItem]:
        """获取或创建 115 目标目录；根目录使用 fileid=0。"""
        folder = api.get_folder(Path(remote_dir))
        if folder and folder.fileid:
            logger.debug(
                f"【115自动上传】目标目录：{remote_dir} -> fileid={folder.fileid}"
            )
            return folder
        if remote_dir.strip("/") == "":
            logger.debug("【115自动上传】目标目录使用 115 根目录")
            return FileItem(
                storage=U115_STORAGE,
                path="/",
                type="dir",
                fileid="0",
            )
        return folder

    @staticmethod
    def __parse_extensions(raw: Any) -> set[str]:
        """解析逗号分隔的后缀配置。"""
        result: set[str] = set()
        for value in str(raw or "").replace("，", ",").split(","):
            value = value.strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = "." + value
            result.add(value)
        return result

    def __schedule_once(self) -> None:
        """安排保存配置后立即扫描一次，并自动关闭开关。"""
        logger.info("【115自动上传】保存后立即运行一次扫描")
        scheduled = False
        try:
            scheduled = add_plugin_once_job(
                self.__class__.__name__,
                ONCE_JOB_ID,
                self.scan_once,
                "115 目录秒传上传：立即扫描",
                delay_seconds=2,
            )
        except Exception:
            logger.exception("【115自动上传】注册一次性扫描任务失败")
        if not scheduled:
            logger.warn("【115自动上传】宿主调度器不可用，改用后台线程立即扫描")
            threading.Timer(2.0, self.scan_once).start()
        self._onlyonce = False
        self.__update_config()

    def __update_config(self) -> None:
        """把立即运行开关复位并保存当前配置。"""
        self.update_config(
            {
                "enabled": self._enabled,
                "onlyonce": False,
                "watchdog": self._watchdog_enabled,
                "watch_dir": self._watch_dir,
                "target_dir": self._target_dir,
                "recursive": self._recursive,
                "scan_interval": self._scan_interval,
                "rapid_retry": self._rapid_retry,
                "rapid_interval": self._rapid_interval,
                "stable_seconds": self._stable_seconds,
                "extensions": ",".join(sorted(self._extension_set)),
                "delete_after_upload": self._delete_after_upload,
                "upload_existing": self._upload_existing,
            }
        )

    def __ensure_table(self) -> None:
        """确保插件自有上传记录表存在。"""
        try:
            U115UploadRecord.metadata.create_all(bind=self.get_database().engine)
            logger.debug("【115自动上传】上传记录表已就绪")
        except Exception:
            logger.exception("【115自动上传】创建上传记录表失败")

    def __migrate_legacy(self, watch_dir: Path) -> None:
        """把旧版 JSON 处理记录迁移到数据库，避免升级后重复上传。"""
        if self.get_data("db_migrated"):
            return
        legacy = self.get_data("processed")
        if not isinstance(legacy, dict) or not legacy:
            self.save_data("db_migrated", True)
            return
        count = 0
        now = datetime.now()
        try:
            with self.get_database().session() as session:
                existing = {
                    row[0]
                    for row in session.execute(select(U115UploadRecord.local_path)).all()
                }
                for relative, record in legacy.items():
                    if not isinstance(record, dict):
                        continue
                    if not record.get("uploaded_at") or not record.get("remote_path"):
                        continue
                    local_path = str(watch_dir / relative)
                    if local_path in existing:
                        continue
                    session.add(
                        U115UploadRecord(
                            local_path=local_path,
                            watch_dir=str(watch_dir),
                            relative_path=str(relative),
                            file_size=int(record.get("size") or 0),
                            mtime=int(record.get("mtime") or 0),
                            remote_path=str(record.get("remote_path") or ""),
                            status="success",
                            method="legacy",
                            retry_count=0,
                            error="",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    existing.add(local_path)
                    count += 1
                session.commit()
            self.save_data("db_migrated", True)
            logger.info(f"【115自动上传】已从旧版插件数据迁移 {count} 条上传记录")
        except Exception:
            logger.exception("【115自动上传】迁移旧版上传记录失败")

    def __initialize_watch_dir(self, watch_dir: Path) -> None:
        """按 upload_existing 策略记录当前目录已有文件，防止首次启用重复上传。"""
        state = self.get_data("init_state")
        if (
            isinstance(state, dict)
            and state.get("watch_dir") == str(watch_dir)
            and bool(state.get("upload_existing")) == self._upload_existing
        ):
            return
        logger.info("【115自动上传】初始化监控目录状态")
        if not self._upload_existing:
            count = 0
            now = datetime.now()
            try:
                with self.get_database().session() as session:
                    existing = {
                        row[0]
                        for row in session.execute(
                            select(U115UploadRecord.local_path)
                        ).all()
                    }
                    for local_path in self.__iter_files(watch_dir):
                        try:
                            stat = local_path.stat()
                        except OSError:
                            continue
                        if time.time() - stat.st_mtime < self._stable_seconds:
                            continue
                        key = str(local_path)
                        if key in existing:
                            continue
                        session.add(
                            U115UploadRecord(
                                local_path=key,
                                watch_dir=str(watch_dir),
                                relative_path=local_path.relative_to(
                                    watch_dir
                                ).as_posix(),
                                file_size=stat.st_size,
                                mtime=int(stat.st_mtime),
                                remote_path="",
                                status="skipped",
                                method="",
                                retry_count=0,
                                error="",
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        existing.add(key)
                        count += 1
                    session.commit()
                logger.info(
                    f"【115自动上传】已记录现有文件 {count} 个，不执行上传"
                )
            except Exception:
                logger.exception("【115自动上传】记录现有文件失败")
                return
        else:
            logger.info("【115自动上传】首次扫描将上传目录中已有文件")
        self.save_data(
            "init_state",
            {
                "watch_dir": str(watch_dir),
                "upload_existing": self._upload_existing,
                "at": int(time.time()),
            },
        )

    def __get_record(self, local_path: str) -> Optional[Dict[str, Any]]:
        """按本地绝对路径读取上传记录摘要。"""
        try:
            with self.get_database().session() as session:
                record = session.execute(
                    select(U115UploadRecord).where(
                        U115UploadRecord.local_path == local_path
                    )
                ).scalar_one_or_none()
                if record is None:
                    return None
                return {
                    "id": record.id,
                    "status": record.status,
                    "file_size": record.file_size,
                    "mtime": record.mtime,
                    "remote_path": record.remote_path,
                    "retry_count": record.retry_count,
                }
        except Exception:
            logger.exception("【115自动上传】读取上传记录失败")
            return None

    def __save_record(
        self,
        *,
        local_path: str,
        watch_dir: str,
        relative_path: str,
        size: int,
        mtime: int,
        uploaded: Optional[FileItem],
        status: str,
        method: str,
        error: str,
    ) -> None:
        """新增或更新一条上传记录。"""
        now = datetime.now()
        try:
            with self.get_database().session() as session:
                record = session.execute(
                    select(U115UploadRecord).where(
                        U115UploadRecord.local_path == local_path
                    )
                ).scalar_one_or_none()
                if record is None:
                    record = U115UploadRecord(
                        local_path=local_path,
                        created_at=now,
                    )
                    session.add(record)
                record.watch_dir = watch_dir
                record.relative_path = relative_path
                record.file_size = size
                record.mtime = mtime
                record.remote_path = uploaded.path if uploaded else ""
                record.status = status
                record.method = method or ""
                record.updated_at = now
                if status == "failed":
                    record.retry_count = int(record.retry_count or 0) + 1
                    record.error = (error or "")[:2000]
                else:
                    record.error = ""
                session.commit()
        except Exception:
            logger.exception(f"【115自动上传】保存上传记录失败：{local_path}")

    def __list_records(
        self, limit: int = 200, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """查询分页的上传记录，跳过初始化时产生的 skipped 记录。"""
        try:
            with self.get_database().session() as session:
                statement = (
                    select(U115UploadRecord)
                    .where(U115UploadRecord.status != "skipped")
                    .order_by(U115UploadRecord.updated_at.desc())
                    .offset(max(0, offset))
                    .limit(max(1, limit))
                )
                return [
                    self.__record_to_dict(record)
                    for record in session.execute(statement).scalars()
                ]
        except Exception:
            logger.exception("【115自动上传】查询上传记录失败")
            return []

    def __record_summary(self) -> Dict[str, int]:
        """统计非 skipped 记录的各状态数量。"""
        try:
            with self.get_database().session() as session:
                rows = session.execute(
                    select(U115UploadRecord.status, func.count())
                    .where(U115UploadRecord.status != "skipped")
                    .group_by(U115UploadRecord.status)
                ).all()
                return {str(status): int(count) for status, count in rows}
        except Exception:
            logger.exception("【115自动上传】统计上传记录失败")
            return {}

    def __mark_record_pending(self, record_id: int) -> Tuple[bool, str, str]:
        """把记录标记为待处理，并返回本地文件路径。"""
        if record_id <= 0:
            return False, "记录 ID 无效", ""
        try:
            with self.get_database().session() as session:
                record = session.get(U115UploadRecord, record_id)
                if record is None:
                    return False, "记录不存在", ""
                local_path = record.local_path
                if not Path(local_path).is_file():
                    return False, "本地文件不存在，无法重试", local_path
                try:
                    Path(local_path).relative_to(Path(self._watch_dir))
                except ValueError:
                    return False, "文件不在当前监控目录", local_path
                record.status = "pending"
                record.error = ""
                record.updated_at = datetime.now()
                session.commit()
                return True, "已加入重试队列", local_path
        except Exception:
            logger.exception(f"【115自动上传】标记记录 {record_id} 待重试失败")
            return False, "数据库操作失败", ""

    @staticmethod
    def __record_to_dict(record: U115UploadRecord) -> Dict[str, Any]:
        """把 ORM 记录转换成页面使用的字典。"""
        return {
            "id": record.id,
            "local_path": record.local_path,
            "watch_dir": record.watch_dir,
            "relative_path": record.relative_path,
            "file_size": record.file_size,
            "mtime": record.mtime,
            "remote_path": record.remote_path,
            "status": record.status,
            "method": record.method,
            "retry_count": record.retry_count,
            "error": record.error,
            "created_at": record.created_at.strftime("%Y-%m-%d %H:%M:%S")
            if record.created_at
            else "",
            "updated_at": record.updated_at.strftime("%Y-%m-%d %H:%M:%S")
            if record.updated_at
            else "",
        }

    def __page_button(
        self,
        text: str,
        icon: str,
        path: str,
        params: Optional[dict] = None,
        disabled: bool = False,
    ) -> dict:
        """构造带 API 点击事件的页面按钮。"""
        event: Dict[str, Any] = {
            "api": f"plugin/{self.__class__.__name__}{path}",
            "method": "post",
        }
        if params is not None:
            event["params"] = params
        props: Dict[str, Any] = {
            "size": "small",
            "variant": "tonal",
            "prepend-icon": icon,
            "class": "ma-1",
        }
        if disabled:
            props["disabled"] = True
        return {
            "component": "VBtn",
            "props": props,
            "text": text,
            "events": {"click": event},
        }

    def __record_actions(self, record: Dict[str, Any]) -> List[dict]:
        """构造一条记录的重试和删除按钮。"""
        actions = []
        if record.get("status") in {"failed", "pending"}:
            actions.append(
                self.__page_button(
                    "重试",
                    "mdi-replay",
                    "/records/retry",
                    {"id": record.get("id")},
                )
            )
        actions.append(
            self.__page_button(
                "删除",
                "mdi-delete-outline",
                "/records/delete",
                {"id": record.get("id")},
            )
        )
        return actions

    @staticmethod
    def __status_chip(status: str) -> dict:
        """构造状态标签。"""
        mapping = {
            "success": ("成功", "success"),
            "failed": ("失败", "error"),
            "pending": ("待处理", "warning"),
            "skipped": ("已跳过", "grey"),
        }
        text, color = mapping.get(status, (status or "未知", "grey"))
        return {
            "component": "VChip",
            "props": {"size": "small", "variant": "tonal", "color": color},
            "text": text,
        }

    @staticmethod
    def __text_cell(value: Any, title: Any = None) -> dict:
        """构造带截断和悬浮提示的表格文本单元格。"""
        props: Dict[str, Any] = {
            "class": "text-truncate",
            "style": "max-width: 280px;",
        }
        if title:
            props["title"] = str(title)
        return {
            "component": "td",
            "props": props,
            "text": str(value or "-"),
        }

    @staticmethod
    def __method_text(method: Any) -> str:
        """把上传方式标识转换成中文。"""
        return {
            "rapid": "秒传",
            "normal": "普通上传",
            "legacy": "旧版记录",
        }.get(str(method or ""), "-")
