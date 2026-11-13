"""Google Drive 存储插件。

插件把 Google Drive 作为 MoviePilot V3 的自定义存储接入：
- 通过 ``StorageHelper`` 注册 ``gdrive`` 存储类型；
- 通过 ``StorageOperSelection`` 为整理链提供源/目标适配器；
- 通过 ``get_module()`` 提供文件管理模块方法；
- 提供 OAuth2 配置页、详情页和操作日志。
"""

from __future__ import annotations

import faulthandler
import math
import mimetypes
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import Body

from app.schemas.file import FileItem, FileItemQueryResult, StorageUsage
from app.schemas.types import ChainEventType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import StorageHelper

from .gdrive_client import (
    FOLDER_MIME,
    DriveItem,
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveNotFound,
    format_size,
)

STORAGE_TYPE = "gdrive"
DEFAULT_DISPLAY_NAME = "Google Drive"
DEFAULT_ROOT_PATH = "/"
DEFAULT_DELETE_MODE = "trash"
DEFAULT_CHUNK_SIZE_MB = 8
DEFAULT_TIMEOUT = 60
MAX_LOGS = 100
OAUTH_STATE_TTL = 600

# 配置键顺序同时用于表单默认值和系统存储保存时的白名单
FORM_KEYS = (
    "enabled",
    "display_name",
    "register_storage",
    "client_id",
    "client_secret",
    "refresh_token",
    "redirect_uri",
    "oauth_code",
    "root_folder_id",
    "root_path",
    "test_path",
    "use_shared_drive",
    "shared_drive_id",
    "delete_mode",
    "chunk_size_mb",
    "timeout",
)


def _now_text() -> str:
    """返回本地时间文本。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    """把配置值限制在闭区间内。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


class GDriveStorageProvider:
    """Google Drive 存储适配器（整理链与模块方法共用）。"""

    transtype = {"copy": "复制", "move": "移动"}

    def __init__(
            self,
            client: GoogleDriveClient,
            log: Callable[[str, str, bool, str], None],
            trace: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        保存客户端和操作日志回调。

        :param client: Google Drive 客户端
        :param log: ``(action, path, ok, detail)`` 日志回调
        :param trace: 诊断文件日志回调
        """
        self.client = client
        self._log = log
        self._trace = trace or (lambda _message: None)

    # ------------------------------------------------------------------
    # 数据映射
    # ------------------------------------------------------------------
    def to_file_item(self, item: DriveItem) -> FileItem:
        """
        把客户端中立模型转换为宿主 FileItem。

        :param item: Google Drive 条目
        :return: 宿主文件项
        """
        name = item.name or ""
        suffix = PurePosixPath(name).suffix
        path = item.path or "/"
        if item.is_dir and not path.endswith("/"):
            path += "/"
        return FileItem(
            storage=STORAGE_TYPE,
            type="dir" if item.is_dir else "file",
            path=path,
            name=name,
            basename=name if item.is_dir else (name[:-len(suffix)] if suffix else name),
            extension=None if item.is_dir or not suffix else suffix[1:].lower(),
            size=None if item.is_dir else item.size,
            modify_time=item.modified_time,
            fileid=item.id,
            parent_fileid=item.parent_id,
            url=item.web_view_link,
            thumbnail=item.thumbnail_link,
        )

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------
    def list(self, fileitem: FileItem) -> List[FileItem]:
        """
        列举目录直接子项。

        :param fileitem: 目录项；文件项返回自身
        :return: 子项列表；失败返回空列表
        """
        if fileitem.type == "file":
            return [fileitem]
        self._trace(f"provider.list entry {fileitem.path} fileid={fileitem.fileid}")
        try:
            folder_id = fileitem.fileid or self.client.resolve_folder_id(fileitem.path)
            if not folder_id:
                self._trace(f"provider.list no-folder {fileitem.path}")
                return []
            result = [
                self.to_file_item(item)
                for item in self.client.list(folder_id, fileitem.path or "/")
            ]
            self._trace(f"provider.list done {fileitem.path} count={len(result)}")
            return result
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】列举目录失败：{fileitem.path} - {err}")
            self._trace(f"provider.list error {fileitem.path} {err}")
            return []

    def list_strict(self, fileitem: FileItem) -> List[FileItem]:
        """
        严格列举目录，查询失败时抛出异常。

        :param fileitem: 目录项
        :return: 子项列表
        :raises GoogleDriveError: 查询失败
        """
        folder_id = fileitem.fileid or self.client.resolve_folder_id(fileitem.path)
        if not folder_id:
            raise GoogleDriveNotFound(f"目录不存在：{fileitem.path}")
        return [self.to_file_item(item) for item in self.client.list(folder_id, fileitem.path or "/")]

    def get_item(self, path: Any) -> Optional[FileItem]:
        """按路径查询文件或目录。"""
        self._trace(f"provider.get_item entry {path}")
        try:
            item = self.client.get_item(path)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】查询路径失败：{path} - {err}")
            self._trace(f"provider.get_item error {path} {err}")
            return None
        self._trace(f"provider.get_item done {path} -> {item.path if item else 'None'}")
        return self.to_file_item(item) if item else None

    def get_item_strict(self, path: Any) -> Optional[FileItem]:
        """
        严格查询路径，供整理链直接使用。

        :param path: 对外路径
        :return: 文件项；确认不存在返回 None；API 故障向上抛出
        """
        self._trace(f"provider.get_item_strict entry {path}")
        try:
            item = self.client.get_item(path)
        except Exception as err:
            self._trace(f"provider.get_item_strict error {path} {err}")
            raise
        self._trace(
            f"provider.get_item_strict done {path} -> {item.path if item else 'None'}"
        )
        return self.to_file_item(item) if item else None

    def get_folder(self, path: Any, create: bool = True) -> Optional[FileItem]:
        """解析目录，缺失时按需递归创建。"""
        self._trace(f"provider.get_folder entry {path} create={create}")
        try:
            folder_id = self.client.resolve_folder_id(path, create=create)
            if not folder_id:
                return None
            item = self.client.get_item(path)
            if item:
                return self.to_file_item(item)
            # 根目录无法访问时不能伪造目录项，否则上传只会得到难懂的 404
            if self.client.is_mount_root(path):
                logger.warning(
                    f"【GDrive】根目录不可访问：{path}（目录 ID={folder_id}），"
                    "请检查 root_folder_id 或共享盘权限"
                )
                return None
            normalized = GoogleDriveClient.normalize_virtual_path(path)
            name = PurePosixPath(normalized).name or "/"
            self._trace(f"provider.get_folder synthetic-dir {path} id={folder_id}")
            return FileItem(
                storage=STORAGE_TYPE,
                type="dir",
                path=normalized.rstrip("/") + "/",
                name=name,
                basename=name,
                fileid=folder_id,
            )
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】获取/创建目录失败：{path} - {err}")
            self._trace(f"provider.get_folder error {path} {err}")
            return None

    def get_parent(self, fileitem: FileItem) -> Optional[FileItem]:
        """返回父目录项。"""
        path = str(fileitem.path or "/").rstrip("/")
        parent = str(PurePosixPath(path).parent)
        return self.get_item(parent or "/")

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        """按 ID 重新读取详情。"""
        if not fileitem.fileid:
            return self.get_item(fileitem.path)
        try:
            item = self.client.get_item_by_id(fileitem.fileid, fileitem.path)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】读取详情失败：{fileitem.path} - {err}")
            return None
        return self.to_file_item(item) if item else None

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------
    def create_folder(self, parent: FileItem, name: str) -> Optional[FileItem]:
        """在父目录下创建文件夹。"""
        if not name:
            return None
        try:
            parent_id = parent.fileid or self.client.resolve_folder_id(parent.path, create=True)
            if not parent_id:
                return None
            item = self.client.create_folder(parent_id, parent.path or "/", name)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】创建目录失败：{parent.path}/{name} - {err}")
            self._log("create_folder", f"{parent.path}/{name}", False, str(err))
            return None
        if not item:
            return None
        self._log("create_folder", item.path, True, "")
        return self.to_file_item(item)

    def upload(
            self,
            folder: FileItem,
            path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        """上传本地文件到目标目录。"""
        try:
            folder_id = folder.fileid or self.client.resolve_folder_id(folder.path, create=True)
            if not folder_id:
                message = f"无法解析目标目录 ID：{folder.path}"
                logger.error(f"【GDrive】上传失败：{path} - {message}")
                self._log("upload", str(path), False, message)
                return None
            logger.info(
                f"【GDrive】开始上传：{path} -> {folder.path}"
                f"（目录 ID={folder_id}，大小={Path(path).stat().st_size if Path(path).is_file() else '未知'}）"
            )
            item = self.client.upload(folder_id, folder.path or "/", Path(path), new_name)
        except (GoogleDriveError, OSError, ValueError) as err:
            detail = str(err)
            if getattr(err, "status_code", None) == 404:
                detail += (
                    f"；目标目录 ID={folder_id} 不存在或当前账号无权限。"
                    "若通过团队盘快捷方式访问，请保持 root_folder_id=root，"
                    "并把目录写到 /快捷方式名/... 下；不要在根目录 ID 填共享盘 ID。"
                )
                try:
                    access = self.client.describe_folder_access(folder_id)
                    if access.get("is_shared_drive"):
                        detail += (
                            " 该 ID 是共享盘 ID，不是共享盘内的文件夹 ID；"
                            "请改用共享盘内实际文件夹 ID，或使用“我的云端硬盘”里的快捷方式。"
                        )
                except Exception:
                    pass
            logger.error(f"【GDrive】上传失败：{path} - {detail}", exc_info=True)
            self._log("upload", str(path), False, detail)
            return None
        if not item:
            message = "Google Drive 未返回上传结果"
            logger.error(f"【GDrive】上传失败：{path} - {message}")
            self._log("upload", str(path), False, message)
            return None
        self._log("upload", item.path, True, "")
        return self.to_file_item(item)

    def download(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """下载远端文件到本地目录。"""
        if not fileitem.fileid:
            return None
        try:
            detail = self.client.get_item_by_id(fileitem.fileid, fileitem.path)
            mime_type = detail.mime_type if detail else (
                mimetypes.guess_type(fileitem.name or "")[0] or "application/octet-stream"
            )
            local_dir = Path(path) if path else Path(tempfile.mkdtemp(prefix="moviepilot_gdrive_"))
            result = self.client.download(
                fileitem.fileid,
                fileitem.name or "",
                mime_type,
                local_dir,
            )
        except (GoogleDriveError, OSError, ValueError) as err:
            logger.warning(f"【GDrive】下载失败：{fileitem.path} - {err}")
            self._log("download", fileitem.path or "", False, str(err))
            return None
        if result:
            self._log("download", fileitem.path or "", True, str(result))
        return result

    def _shortcut_info(self, fileitem: FileItem) -> Optional[Dict[str, Any]]:
        """返回文件项路径对应的快捷方式信息（如果它是快捷方式挂载点）。"""
        getter = getattr(self.client, "shortcut_info_for_path", None)
        if not callable(getter):
            return None
        try:
            return getter(fileitem.path)
        except Exception:
            return None

    def _forget_shortcut(self, fileitem: FileItem) -> None:
        """快捷方式被删除/改名/移动后清理其旧路径缓存。"""
        forget = getattr(self.client, "forget_shortcut", None)
        if not callable(forget):
            return
        try:
            forget(fileitem.path)
        except Exception:
            pass

    def copy(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """复制文件到同一存储的目标目录。"""
        try:
            target_id = self.client.resolve_folder_id(path, create=True)
            shortcut = self._shortcut_info(fileitem)
            # 目录快捷方式复制快捷方式本身；文件快捷方式复制目标文件内容
            if shortcut and shortcut.get("target_mime") == FOLDER_MIME:
                source_id = shortcut["id"]
            else:
                source_id = fileitem.fileid
            if not target_id or not source_id:
                return False
            ok = self.client.copy(source_id, target_id, new_name)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】复制失败：{fileitem.path} -> {path} - {err}")
            self._log("copy", f"{fileitem.path} -> {path}", False, str(err))
            return False
        self._log("copy", f"{fileitem.path} -> {path}", ok, "")
        return ok

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        """移动文件到目标目录，可同时改名。"""
        try:
            target_id = self.client.resolve_folder_id(path, create=True)
            shortcut = self._shortcut_info(fileitem)
            source_id = shortcut["id"] if shortcut else fileitem.fileid
            old_parent = (
                (shortcut.get("parents") or [None])[0]
                if shortcut else fileitem.parent_fileid
            )
            if not old_parent and source_id:
                detail = self.client.get_item_by_id(source_id, fileitem.path)
                old_parent = detail.parent_id if detail else None
            if not target_id or not source_id:
                return False
            ok = self.client.move(source_id, old_parent or "", target_id, new_name)
            if ok and shortcut:
                self._forget_shortcut(fileitem)
        except (GoogleDriveError, ValueError) as err:
            logger.warning(f"【GDrive】移动失败：{fileitem.path} -> {path} - {err}")
            self._log("move", f"{fileitem.path} -> {path}", False, str(err))
            return False
        self._log("move", f"{fileitem.path} -> {path}", ok, "")
        return ok

    def rename(self, fileitem: FileItem, name: str) -> bool:
        """重命名文件或目录。"""
        shortcut = self._shortcut_info(fileitem)
        target_id = shortcut["id"] if shortcut else fileitem.fileid
        if not target_id:
            return False
        try:
            ok = self.client.rename(target_id, name)
            if ok and shortcut:
                self._forget_shortcut(fileitem)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】重命名失败：{fileitem.path} - {err}")
            self._log("rename", fileitem.path or "", False, str(err))
            return False
        self._log("rename", fileitem.path or "", ok, name)
        return ok

    def delete(self, fileitem: FileItem) -> bool:
        """删除文件或目录；删除快捷方式挂载点时只删除快捷方式本身。"""
        shortcut = self._shortcut_info(fileitem)
        target_id = shortcut["id"] if shortcut else fileitem.fileid
        if not target_id:
            return False
        try:
            ok = self.client.delete(target_id)
            if ok and shortcut:
                self._forget_shortcut(fileitem)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】删除失败：{fileitem.path} - {err}")
            self._log("delete", fileitem.path or "", False, str(err))
            return False
        self._log("delete", fileitem.path or "", ok, "")
        return ok

    # ------------------------------------------------------------------
    # 管理
    # ------------------------------------------------------------------
    def usage(self) -> Optional[StorageUsage]:
        """返回容量信息。"""
        try:
            quota = self.client.usage()
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】查询容量失败：{err}")
            return None
        return StorageUsage(total=float(quota.total), available=float(quota.available))

    def check(self) -> bool:
        """测试凭据与根目录可访问。"""
        try:
            self.client.check()
            return True
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】连接测试失败：{err}")
            return False

    def is_support_transtype(self, transtype: str) -> bool:
        """判断是否支持指定整理方式。"""
        return transtype in self.transtype


class GDriveStorage(_PluginBase):
    """Google Drive 存储插件。"""

    plugin_name = "Google Drive 存储"
    plugin_desc = "把 Google Drive 接入 MoviePilot 存储体系，支持浏览、上传、下载、移动、复制、删除和容量查询。"
    plugin_version = "1.1.1"
    plugin_label = "网盘"
    plugin_author = "local"
    plugin_config_prefix = "gdrivestorage_"
    plugin_order = 100
    auth_level = 1

    _enabled = False
    _display_name = DEFAULT_DISPLAY_NAME
    _register_storage = True
    _page = 1
    _page_size = 10
    _config: Dict[str, Any] = {}
    _client: Optional[GoogleDriveClient] = None
    _provider: Optional[GDriveStorageProvider] = None
    _logs: List[Dict[str, Any]] = []
    _last_quota: Dict[str, Any] = {}
    _client_lock: threading.RLock

    def __init__(self) -> None:
        """初始化线程锁和运行时状态。"""
        super().__init__()
        self._client_lock = threading.RLock()
        self._trace_lock = threading.Lock()
        self._config = {}
        self._logs = []
        self._last_quota = {}
        self._client = None
        self._provider = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建客户端、适配器和系统存储注册。"""
        self.stop_service()
        cfg = self._normalize_config(config or {})
        self._config = cfg
        self._enabled = bool(cfg.get("enabled"))
        self._display_name = cfg["display_name"]
        self._register_storage = bool(cfg.get("register_storage", True))
        self._trace(
            f"init v{self.plugin_version} enabled={self._enabled} "
            f"root_id={cfg.get('root_folder_id')} root_path={cfg.get('root_path')}"
        )
        self._page = 1
        stored_logs = self.get_data("logs")
        self._logs = list(stored_logs)[-MAX_LOGS:] if isinstance(stored_logs, list) else []
        stored_quota = self.get_data("quota")
        self._last_quota = stored_quota if isinstance(stored_quota, dict) else {}
        self._client = self._build_client(cfg)
        self._provider = (
            GDriveStorageProvider(self._client, self._record_log, self._trace)
            if self._client and cfg.get("refresh_token") else None
        )
        if self._enabled and self._register_storage:
            self._ensure_storage_registered()
        if self._enabled and not self._client:
            logger.warning("【GDrive】插件已启用，但 OAuth 客户端凭据不完整，存储暂不可用")
        elif self._enabled and not cfg.get("refresh_token"):
            logger.warning("【GDrive】插件已启用，但尚未完成授权，存储暂不可读写")
        elif self._enabled:
            logger.info(
                f"【GDrive】插件已启用 v{self.plugin_version}：存储={self._display_name}，"
                f"根路径={cfg['root_path']}，共享盘={cfg['use_shared_drive']}"
            )

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """不注册远程命令。"""
        return []

    def stop_service(self) -> None:
        """关闭 HTTP 客户端和适配器，可重复调用。"""
        with self._client_lock:
            client = self._client
            self._client = None
            self._provider = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("【GDrive】关闭 HTTP 客户端失败", exc_info=True)

    def _build_client(self, cfg: Dict[str, Any]) -> Optional[GoogleDriveClient]:
        """凭据完整时构建客户端；不发起网络请求。"""
        # 授权码交换阶段允许暂时没有 refresh_token，因此只要求客户端凭据
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            return None
        try:
            chunk_size = int(cfg.get("chunk_size_mb") or DEFAULT_CHUNK_SIZE_MB) * 1024 * 1024
            return GoogleDriveClient(
                client_id=cfg["client_id"],
                client_secret=cfg["client_secret"],
                refresh_token=cfg["refresh_token"],
                root_folder_id=cfg.get("root_folder_id") or "root",
                root_path=cfg.get("root_path") or DEFAULT_ROOT_PATH,
                shared_drive_id=cfg.get("shared_drive_id") or "",
                use_shared_drive=bool(cfg.get("use_shared_drive")),
                delete_mode=cfg.get("delete_mode") or DEFAULT_DELETE_MODE,
                chunk_size=chunk_size,
                timeout=float(cfg.get("timeout") or DEFAULT_TIMEOUT),
            )
        except Exception as err:
            logger.error(f"【GDrive】初始化 Google Drive 客户端失败：{err}")
            return None

    def _rebuild_client(self) -> None:
        """配置变化后重建客户端和适配器。"""
        with self._client_lock:
            old = self._client
            self._client = self._build_client(self._config)
            self._provider = (
                GDriveStorageProvider(self._client, self._record_log, self._trace)
                if self._client and self._config.get("refresh_token") else None
            )
        if old is not None and old is not self._client:
            try:
                old.close()
            except Exception:
                logger.debug("【GDrive】关闭旧客户端失败", exc_info=True)

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def _default_redirect_uri(self) -> str:
        """返回当前实例默认的 OAuth 回调地址。"""
        return (
            f"http://localhost:3000/api/v1/plugin/"
            f"{self.__class__.__name__}/oauth/callback"
        )

    def _normalize_config(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """把用户配置归一化为稳定类型和默认值。"""
        root_path = str(cfg.get("root_path") or DEFAULT_ROOT_PATH).strip() or DEFAULT_ROOT_PATH
        try:
            root_path = GoogleDriveClient.normalize_virtual_path(root_path)
        except ValueError:
            logger.warning(f"【GDrive】root_path 无效，回退为 {DEFAULT_ROOT_PATH}")
            root_path = DEFAULT_ROOT_PATH
        raw_test_path = str(cfg.get("test_path") or "/").strip() or "/"
        try:
            test_path = GoogleDriveClient.normalize_virtual_path(raw_test_path)
        except ValueError:
            test_path = "/"
        delete_mode = "permanent" if cfg.get("delete_mode") == "permanent" else "trash"
        return {
            "enabled": bool(cfg.get("enabled")),
            "display_name": str(cfg.get("display_name") or DEFAULT_DISPLAY_NAME).strip() or DEFAULT_DISPLAY_NAME,
            "register_storage": bool(cfg.get("register_storage", True)),
            "client_id": str(cfg.get("client_id") or "").strip(),
            "client_secret": str(cfg.get("client_secret") or "").strip(),
            "refresh_token": str(cfg.get("refresh_token") or "").strip(),
            "redirect_uri": str(cfg.get("redirect_uri") or self._default_redirect_uri()).strip(),
            "oauth_code": str(cfg.get("oauth_code") or "").strip(),
            "root_folder_id": str(cfg.get("root_folder_id") or "root").strip() or "root",
            "root_path": root_path,
            "test_path": test_path,
            "use_shared_drive": bool(cfg.get("use_shared_drive")),
            "shared_drive_id": str(cfg.get("shared_drive_id") or "").strip(),
            "delete_mode": delete_mode,
            "chunk_size_mb": _clamp_int(cfg.get("chunk_size_mb"), 1, 1024, DEFAULT_CHUNK_SIZE_MB),
            "timeout": _clamp_int(cfg.get("timeout"), 5, 600, DEFAULT_TIMEOUT),
        }

    def _save_config(self, updates: Dict[str, Any]) -> None:
        """合并并持久化插件配置。"""
        merged = dict(self._config)
        merged.update(updates)
        self._config = self._normalize_config(merged)
        try:
            self.update_config(self._config)
        except Exception:
            logger.exception("【GDrive】保存插件配置失败")

    # ------------------------------------------------------------------
    # 系统存储注册
    # ------------------------------------------------------------------
    def _ensure_storage_registered(self) -> None:
        """确保系统存储中存在 gdrive 条目。"""
        try:
            helper = StorageHelper()
            existing = helper.get_storage(STORAGE_TYPE)
            if existing is None:
                helper.add_storage(STORAGE_TYPE, self._display_name, {})
                logger.info(f"【GDrive】系统存储已注册：{self._display_name}")
            else:
                # 已有条目时绝不重复写 SystemConfig：宿主即使值未变化也会发布配置变更，
                # 可能触发 TransferChain 反复 config_reload，阻塞整理任务。
                logger.debug(f"【GDrive】系统存储已存在，跳过写入：{self._display_name}")
        except Exception as err:
            logger.warning(f"【GDrive】注册系统存储失败：{err}")

    def _storage_registered(self) -> bool:
        """查询系统存储中是否存在 gdrive。"""
        try:
            return StorageHelper().get_storage(STORAGE_TYPE) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    @eventmanager.register(ChainEventType.StorageOperSelection)
    def storage_oper_selection(self, event: Event) -> None:
        """仅接管 gdrive 存储的整理适配器选择。"""
        if not self._enabled or not self._provider:
            return
        try:
            data = event.event_data
        except Exception:
            return
        storage = getattr(data, "storage", None)
        if storage is None and isinstance(data, dict):
            storage = data.get("storage")
        if storage not in {STORAGE_TYPE, self._display_name}:
            return
        try:
            data.storage_oper = self._provider
        except Exception:
            logger.debug("【GDrive】无法写入整理适配器", exc_info=True)

    # ------------------------------------------------------------------
    # 模块方法
    # ------------------------------------------------------------------
    def get_module(self) -> Dict[str, Any]:
        """声明插件接管的文件管理模块方法。"""
        return {
            "list_files": self.list_files,
            "any_files": self.any_files,
            "create_folder": self.create_folder,
            "get_folder": self.get_folder,
            "download_file": self.download_file,
            "upload_file": self.upload_file,
            "delete_file": self.delete_file,
            "rename_file": self.rename_file,
            "get_file_item": self.get_file_item,
            "get_file_item_strict": self.get_file_item_strict,
            "get_parent_item": self.get_parent_item,
            "snapshot_storage": self.snapshot_storage,
            "storage_manage": self.storage_manage,
            "storage_usage": self.storage_usage,
            "support_transtype": self.support_transtype,
            "exists": self.exists,
            "get_item": self.get_item,
        }

    def _is_gdrive(self, storage: Optional[str]) -> bool:
        """判断存储标识是否为当前插件（兼容类型 gdrive 与显示名）。"""
        value = str(storage or "").strip()
        return bool(value) and value in {STORAGE_TYPE, self._display_name}

    def _provider_or_none(self) -> Optional[GDriveStorageProvider]:
        """返回可用适配器。"""
        return self._provider if self._enabled else None

    def list_files(
            self,
            fileitem: FileItem,
            recursion: bool = False,
    ) -> Optional[List[FileItem]]:
        """列举目录；递归时只返回文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        items = provider.list(fileitem)
        if not recursion:
            return items
        files: List[FileItem] = []
        for item in items:
            if item.type == "dir":
                files.extend(self.list_files(item, True) or [])
            else:
                files.append(item)
        return files

    def any_files(
            self,
            fileitem: FileItem,
            extensions: Optional[List[str]] = None,
    ) -> Optional[bool]:
        """递归判断目录下是否存在指定扩展名的文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        wanted = {
            str(ext).lower().lstrip(".")
            for ext in (extensions or [])
            if str(ext).strip()
        }

        def walk(item: FileItem) -> bool:
            for child in provider.list(item):
                if child.type == "file":
                    if not wanted or (child.extension or "").lower() in wanted:
                        return True
                elif child.type == "dir" and walk(child):
                    return True
            return False

        return walk(fileitem)

    def create_folder(self, fileitem: FileItem, name: str) -> Optional[FileItem]:
        """创建目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.create_folder(fileitem, name) if provider else None

    def get_folder(self, storage: str, path: Path) -> Optional[FileItem]:
        """获取目录，缺失时创建。"""
        is_gdrive = self._is_gdrive(storage)
        diag = (
            f"get_folder storage={storage} gdrive={is_gdrive} "
            f"thread={threading.current_thread().name} path={path}"
        )
        self._trace(diag)
        if is_gdrive:
            logger.warning(f"【GDrive】DIAG {diag}")
        else:
            logger.debug(f"【GDrive】DIAG {diag}")
        if not is_gdrive:
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        started = time.monotonic()
        logger.info(f"【GDrive】get_folder 开始：{path}")
        done = self._stack_watchdog(f"get_folder {path}")
        try:
            result = provider.get_folder(path, create=True)
        finally:
            done.set()
        elapsed = time.monotonic() - started
        self._trace(
            f"get_folder done {path} -> {result.path if result else 'None'} "
            f"elapsed={elapsed:.2f}s"
        )
        logger.info(
            f"【GDrive】get_folder 完成：{path} -> "
            f"{result.path if result else 'None'}（{elapsed:.2f}s）"
        )
        return result

    def download_file(self, fileitem: FileItem, path: Optional[Path] = None) -> Optional[Path]:
        """下载文件到本地。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.download(fileitem, path) if provider else None

    def upload_file(
            self,
            fileitem: FileItem,
            path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        """上传本地文件。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.upload(fileitem, path, new_name) if provider else None

    def delete_file(self, fileitem: FileItem) -> Optional[bool]:
        """删除文件或目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.delete(fileitem) if provider else None

    def rename_file(self, fileitem: FileItem, name: str) -> Optional[bool]:
        """重命名文件或目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.rename(fileitem, name) if provider else None

    @staticmethod
    def _stack_watchdog(label: str, timeout: float = 30.0) -> threading.Event:
        """查询超过 timeout 秒时打印所有线程堆栈，便于定位卡死位置。"""
        done = threading.Event()

        def watch() -> None:
            if not done.wait(timeout):
                logger.error(
                    f"【GDrive】{label} 超过 {timeout:g}s，打印线程堆栈以定位卡死点"
                )
                faulthandler.dump_traceback(all_threads=True)

        threading.Thread(target=watch, name="GDrive-stack-watch", daemon=True).start()
        return done

    def get_file_item(self, storage: str, path: Path) -> Optional[FileItem]:
        """按路径查询文件或目录。"""
        is_gdrive = self._is_gdrive(storage)
        diag = (
            f"get_file_item storage={storage} gdrive={is_gdrive} "
            f"thread={threading.current_thread().name} path={path}"
        )
        self._trace(diag)
        if is_gdrive:
            logger.warning(f"【GDrive】DIAG {diag}")
        else:
            logger.debug(f"【GDrive】DIAG {diag}")
        if not is_gdrive:
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        started = time.monotonic()
        logger.info(f"【GDrive】get_file_item 开始：{path}")
        done = self._stack_watchdog(f"get_file_item {path}")
        try:
            result = provider.get_item(path)
        finally:
            done.set()
        elapsed = time.monotonic() - started
        self._trace(
            f"get_file_item done {path} -> {result.path if result else 'None'} "
            f"elapsed={elapsed:.2f}s"
        )
        logger.info(
            f"【GDrive】get_file_item 完成：{path} -> "
            f"{result.path if result else 'None'}（{elapsed:.2f}s）"
        )
        return result

    def get_file_item_strict(self, storage: str, path: Path) -> Optional[FileItemQueryResult]:
        """严格查询文件或目录，返回宿主模块契约要求的查询回执。"""
        is_gdrive = self._is_gdrive(storage)
        diag = (
            f"get_file_item_strict storage={storage} gdrive={is_gdrive} "
            f"thread={threading.current_thread().name} path={path}"
        )
        self._trace(diag)
        if is_gdrive:
            logger.warning(f"【GDrive】DIAG {diag}")
        else:
            logger.debug(f"【GDrive】DIAG {diag}")
        if not is_gdrive:
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        started = time.monotonic()
        logger.info(f"【GDrive】get_file_item_strict 开始：{path}")
        done = self._stack_watchdog(f"get_file_item_strict {path}")
        try:
            item = provider.get_item_strict(path)
        finally:
            done.set()
        elapsed = time.monotonic() - started
        self._trace(
            f"get_file_item_strict done {path} -> {item.path if item else 'None'} "
            f"elapsed={elapsed:.2f}s"
        )
        logger.info(
            f"【GDrive】get_file_item_strict 完成：{path} -> "
            f"{item.path if item else 'None'}（{elapsed:.2f}s）"
        )
        return FileItemQueryResult(item=item)

    def get_parent_item(self, fileitem: FileItem) -> Optional[FileItem]:
        """获取父目录。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.get_parent(fileitem) if provider else None

    def get_item(self, fileitem: FileItem) -> Optional[FileItem]:
        """兼容旧模块契约：按文件项查询对应云端项。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        provider = self._provider_or_none()
        return provider.get_item(fileitem.path) if provider else None

    def exists(self, fileitem: FileItem) -> Optional[bool]:
        """兼容旧模块契约：判断文件项是否存在。"""
        if not self._is_gdrive(fileitem.storage):
            return None
        return self.get_item(fileitem) is not None

    def storage_usage(self, storage: str) -> Optional[StorageUsage]:
        """兼容旧模块契约：查询容量。"""
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        return provider.usage() if provider else None

    def support_transtype(self, storage: str) -> Optional[Dict[str, str]]:
        """兼容旧模块契约：声明支持的整理方式。"""
        if not self._is_gdrive(storage) or not self._provider:
            return None
        return dict(GDriveStorageProvider.transtype)

    def snapshot_storage(
            self,
            storage: str,
            path: Path,
            last_snapshot_time: Optional[float] = None,
            max_depth: int = 5,
            previous_snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """生成目录文件快照；查询失败返回 None 以保留旧基线。"""
        del last_snapshot_time, previous_snapshot
        if not self._is_gdrive(storage):
            return None
        provider = self._provider_or_none()
        if not provider:
            return None
        try:
            root = provider.get_item_strict(path)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】快照根目录查询失败：{path} - {err}")
            return None
        if not root:
            return {}
        result: Dict[str, Dict[str, Any]] = {}

        def walk(item: FileItem, depth: int) -> None:
            if item.type == "dir":
                if depth >= max_depth:
                    return
                for child in provider.list_strict(item):
                    walk(child, depth + 1)
            else:
                result[item.path or ""] = {
                    "size": item.size or 0,
                    "modify_time": item.modify_time or 0,
                    "fileid": item.fileid,
                    "type": item.type,
                }

        try:
            walk(root, 0)
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】生成快照失败：{path} - {err}")
            return None
        return result

    def storage_manage(self, storage: str, action: str, **params: Any) -> Optional[Dict[str, Any]]:
        """处理系统存储页的通用管理动作。"""
        if not self._is_gdrive(storage):
            return None
        action_value = getattr(action, "value", action)
        action_value = str(action_value or "")
        if action_value == "usage":
            provider = self._provider_or_none()
            usage = provider.usage() if provider else None
            if not usage:
                return {"success": False, "message": "容量查询失败", "data": None}
            return {
                "success": True,
                "message": "容量已刷新",
                "data": {
                    "total": usage.total,
                    "available": usage.available,
                    "total_text": format_size(int(usage.total)),
                    "available_text": format_size(int(usage.available)),
                },
            }
        if action_value == "support_transtype":
            return {
                "success": True,
                "message": "",
                "data": {"transtype": dict(GDriveStorageProvider.transtype)},
            }
        if action_value == "generate_auth_url":
            url = self._auth_url()
            if not url:
                return {"success": False, "message": "请先填写 client_id 和 redirect_uri", "data": None}
            return {
                "success": True,
                "message": "",
                "data": {"authUrl": url, "state": (self.get_data("oauth_state") or {}).get("value", "")},
            }
        if action_value == "check_login":
            provider = self._provider_or_none()
            ok = bool(provider and provider.check())
            return {
                "success": ok,
                "message": "连接正常" if ok else "连接失败",
                "data": {"status": 1 if ok else 0, "tip": "连接正常" if ok else "请检查 OAuth 凭据"},
            }
        if action_value == "save_config":
            conf = params.get("conf") or {}
            updates = {key: conf[key] for key in FORM_KEYS if key in conf}
            if updates:
                self._save_config(updates)
                self._rebuild_client()
                if self._enabled and self._register_storage:
                    self._ensure_storage_registered()
            return {"success": True, "message": "配置已保存", "data": None}
        return {"success": False, "message": "不支持的管理动作", "data": None}

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def _auth_url(self) -> Optional[str]:
        """生成并缓存一次性 state 的授权链接。"""
        if not self._client:
            return None
        state = self.get_data("oauth_state") or {}
        now = time.time()
        if (
                not isinstance(state, dict)
                or not state.get("value")
                or float(state.get("expires_at") or 0) < now
        ):
            state = {
                "value": secrets.token_urlsafe(24),
                "expires_at": now + OAUTH_STATE_TTL,
            }
            self.save_data("oauth_state", state)
        try:
            return self._client.build_auth_url(self._config["redirect_uri"], state["value"])
        except GoogleDriveAuthError as err:
            logger.warning(f"【GDrive】生成授权链接失败：{err}")
            return None

    def _exchange_code(self, code: str) -> Tuple[bool, str]:
        """用授权码换取 refresh_token 并持久化。"""
        if not self._client:
            return False, "尚未配置完整的 OAuth 凭据"
        if not code:
            return False, "授权码为空"
        try:
            token = self._client.exchange_code(code, self._config["redirect_uri"])
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】授权码换取令牌失败：{err}")
            return False, str(err)
        refresh_token = str(token.get("refresh_token") or "").strip()
        if not refresh_token:
            return False, "Google 未返回 refresh_token，请重新授权并确认 prompt=consent"
        self._save_config({"refresh_token": refresh_token, "oauth_code": ""})
        self.del_data("oauth_state")
        self._rebuild_client()
        if self._enabled and self._register_storage:
            self._ensure_storage_registered()
        self._record_log("oauth", "", True, "授权成功")
        return True, "授权成功，refresh_token 已保存"

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """注册详情页操作和 OAuth 回调 API。"""
        return [
            {
                "path": "/test",
                "endpoint": self.api_test,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试 Google Drive 连接",
            },
            {
                "path": "/test_upload",
                "endpoint": self.api_test_upload,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "向测试路径写入再删除一个小文件",
            },
            {
                "path": "/quota",
                "endpoint": self.api_quota,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新 Google Drive 容量",
            },
            {
                "path": "/auth/url",
                "endpoint": self.api_auth_url,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "生成 Google OAuth 授权链接",
            },
            {
                "path": "/auth/exchange",
                "endpoint": self.api_auth_exchange,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "用配置中的授权码换取 refresh_token",
            },
            {
                "path": "/oauth/callback",
                "endpoint": self.api_oauth_callback,
                "methods": ["GET"],
                "auth": "bear",
                "allow_anonymous": True,
                "summary": "Google OAuth 回调",
            },
            {
                "path": "/logs/page",
                "endpoint": self.api_logs_page,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "切换操作日志页码",
            },
            {
                "path": "/logs/clear",
                "endpoint": self.api_logs_clear,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空操作日志",
            },
        ]

    def api_test(self) -> Dict[str, Any]:
        """测试连接并刷新容量。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        if not self._client:
            return {"success": False, "message": "尚未配置完整的 OAuth 凭据", "data": None}
        try:
            info = self._client.check()
            quota = self._client.usage()
        except GoogleDriveError as err:
            logger.warning(f"【GDrive】连接测试失败：{err}")
            self._record_log("test", "", False, str(err))
            return {"success": False, "message": f"连接失败：{err}", "data": None}
        self._last_quota = {
            "total": quota.total,
            "available": quota.available,
            "usage": quota.usage,
            "checked_at": _now_text(),
        }
        self.save_data("quota", self._last_quota)
        self._record_log("test", "", True, "连接正常")
        user = info.get("user") or {}
        root = info.get("root") or {}
        return {
            "success": True,
            "message": "连接正常",
            "data": {
                "user": user.get("displayName") or user.get("emailAddress") or "",
                "root": root.get("path") or self._config.get("root_path"),
                "root_name": root.get("name") or "",
                "total": quota.total,
                "available": quota.available,
                "total_text": format_size(quota.total),
                "available_text": format_size(quota.available),
            },
        }

    def api_test_upload(self) -> Dict[str, Any]:
        """向测试路径写入一个临时文件并删除，用于验证写权限和上传链路。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        provider = self._provider_or_none()
        if not provider:
            return {"success": False, "message": "尚未完成授权", "data": None}
        target_path = str(self._config.get("test_path") or "/").strip() or "/"
        temp_file = self.get_data_path() / ".gdrive_write_test"
        done = self._stack_watchdog(f"api_test_upload {target_path}", 30.0)
        try:
            self._diag_step("start", target_path)
            temp_file.write_text("moviepilot gdrive write test\n", encoding="utf-8")
            self._diag_step("get_folder")
            folder = provider.get_folder(target_path, create=True)
            self._diag_step("get_folder_done", f"folder={folder.path if folder else None}")
            if not folder:
                extra = ""
                if self._client and self._client.is_mount_root(target_path):
                    access = self._client.describe_folder_access(self._client.root_id)
                    if access.get("is_shared_drive"):
                        extra = (
                            f"；根目录 ID={self._client.root_id} 是共享盘 ID，"
                            "不是共享盘内的文件夹 ID。请改用共享盘内实际文件夹 ID，"
                            "或保持 root_folder_id=root，把快捷方式放在“我的云端硬盘”，"
                            "并把测试上传路径写到 /快捷方式名/... 下。"
                        )
                    elif not access.get("exists"):
                        extra = (
                            f"；根目录 ID={self._client.root_id} 不存在或当前账号无权限。"
                            "如果使用团队盘快捷方式，请保持 root_folder_id=root，"
                            "并把测试上传路径写到 /快捷方式名/... 下；"
                            "不要把共享盘 ID 直接填到根目录 ID。"
                        )
                return {
                    "success": False,
                    "message": f"无法获取/创建测试目录：{target_path}{extra}",
                    "data": None,
                }
            access_info: Dict[str, Any] = {}
            if self._client and folder.fileid:
                self._diag_step("describe_folder_access", f"id={folder.fileid}")
                access = self._client.describe_folder_access(folder.fileid)
                self._diag_step(
                    "describe_folder_access_done",
                    f"exists={access.get('exists')} canAdd={access.get('can_add_children')}",
                )
                access_info = access
                if not access.get("exists"):
                    return {
                        "success": False,
                        "message": (
                            f"目标目录不存在或当前账号无权限：{target_path}"
                            f"（ID={folder.fileid}）。团队盘快捷方式请确认路径位于 "
                            "/快捷方式名/... 下，且账号有该目录访问权限。"
                        ),
                        "data": {"path": target_path, "fileid": folder.fileid},
                    }
                if access.get("can_add_children") is False:
                    return {
                        "success": False,
                        "message": (
                            f"目标目录存在但当前账号没有写入权限：{target_path}"
                            f"（ID={folder.fileid}）。请给账号该共享盘/目录的"
                            "“内容管理者”或等效写入权限。"
                        ),
                        "data": {"path": target_path, "fileid": folder.fileid},
                    }
                # 先走 files.create 元数据探针，区分“目录不可写”和“上传端点 404”
                try:
                    self._diag_step("create_probe", f"id={folder.fileid}")
                    probe = self._client.create_probe(folder.fileid)
                    self._diag_step("create_probe_done", f"ok={probe.get('ok')}")
                    access_info["create_probe"] = probe
                except GoogleDriveError as err:
                    access_summary = (
                        f"；目录能力：canAddChildren={access.get('can_add_children')}，"
                        f"canEdit={access.get('can_edit')}，"
                        f"isSharedDrive={access.get('is_shared_drive')}"
                    )
                    return {
                        "success": False,
                        "message": (
                            f"目录创建测试失败（files.create 返回错误）：{err}"
                            f"{access_summary}；目录 ID={folder.fileid}"
                        ),
                        "data": {"path": target_path, "access": access_info},
                    }
            self._diag_step("upload_start")
            item = provider.upload(folder, temp_file, ".gdrive_write_test")
            self._diag_step("upload_done", f"item={item.path if item else None}")
            if not item:
                failure = next(
                    (
                        entry for entry in reversed(self._logs)
                        if entry.get("action") == "upload" and not entry.get("ok")
                    ),
                    None,
                )
                detail = (failure or {}).get("detail") or "上传失败，请查看操作日志"
                access_summary = ""
                if access_info:
                    access_summary = (
                        f"；目录能力：canAddChildren={access_info.get('can_add_children')}，"
                        f"canEdit={access_info.get('can_edit')}，"
                        f"isSharedDrive={access_info.get('is_shared_drive')}"
                    )
                return {
                    "success": False,
                    "message": f"测试上传失败：{detail}{access_summary}",
                    "data": {"path": target_path, "access": access_info},
                }
            provider.delete(item)
            return {
                "success": True,
                "message": f"测试上传成功：{item.path}",
                "data": {"path": item.path, "fileid": item.fileid},
            }
        finally:
            done.set()
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass

    def api_quota(self) -> Dict[str, Any]:
        """刷新容量并缓存。"""
        if not self._enabled:
            return {"success": False, "message": "插件未启用", "data": None}
        provider = self._provider_or_none()
        if not provider:
            return {"success": False, "message": "尚未配置完整的 OAuth 凭据", "data": None}
        usage = provider.usage()
        if not usage:
            self._record_log("quota", "", False, "容量查询失败")
            return {"success": False, "message": "容量查询失败，请检查授权", "data": None}
        self._last_quota = {
            "total": usage.total,
            "available": usage.available,
            "usage": max(0.0, usage.total - usage.available) if usage.total else 0.0,
            "checked_at": _now_text(),
        }
        self.save_data("quota", self._last_quota)
        self._record_log("quota", "", True, "")
        return {
            "success": True,
            "message": "容量已刷新",
            "data": {
                "total": usage.total,
                "available": usage.available,
                "total_text": format_size(int(usage.total)),
                "available_text": format_size(int(usage.available)),
            },
        }

    def api_auth_url(self) -> Dict[str, Any]:
        """返回 OAuth 授权链接。"""
        url = self._auth_url()
        if not url:
            return {"success": False, "message": "请先填写 client_id 和 redirect_uri", "data": None}
        state = (self.get_data("oauth_state") or {}).get("value", "")
        return {"success": True, "message": "", "data": {"authUrl": url, "state": state}}

    def api_auth_exchange(self) -> Dict[str, Any]:
        """使用配置页保存的授权码换取 refresh_token。"""
        code = str(self._config.get("oauth_code") or "").strip()
        if not code:
            return {"success": False, "message": "请先在配置页填写授权码并保存", "data": None}
        ok, message = self._exchange_code(code)
        return {"success": ok, "message": message, "data": None}

    def api_oauth_callback(
            self,
            code: str = "",
            state: str = "",
            error: str = "",
    ) -> Dict[str, Any]:
        """处理 Google OAuth 回调；必须校验一次性 state。"""
        if error:
            self._record_log("oauth", "", False, f"Google 返回错误：{error}")
            return {"success": False, "message": f"Google 授权失败：{error}", "data": None}
        saved = self.get_data("oauth_state") or {}
        if (
                not isinstance(saved, dict)
                or not state
                or state != saved.get("value")
                or float(saved.get("expires_at") or 0) < time.time()
        ):
            return {"success": False, "message": "授权状态校验失败或已过期，请重新生成授权链接", "data": None}
        ok, message = self._exchange_code(code)
        return {"success": ok, "message": message, "data": None}

    def api_logs_page(self, payload: dict = Body(...)) -> Dict[str, Any]:
        """切换操作日志页码。"""
        try:
            page = int((payload or {}).get("page") or 1)
        except (TypeError, ValueError):
            page = 1
        pages = self._log_pages()
        self._page = min(max(1, page), pages)
        return {
            "success": True,
            "message": f"第 {self._page} 页",
            "data": {"page": self._page},
        }

    def api_logs_clear(self) -> Dict[str, Any]:
        """清空操作日志。"""
        self._logs = []
        self._page = 1
        self.save_data("logs", [])
        return {"success": True, "message": "日志已清空", "data": None}

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _trace(self, message: str) -> None:
        """把诊断信息追加到插件数据目录，绕开宿主日志级别过滤。"""
        try:
            trace_file = self.get_data_path() / "debug_calls.log"
            with self._trace_lock:
                with trace_file.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')} "
                        f"[{threading.current_thread().name}] {message}\n"
                    )
        except Exception:
            pass

    def _diag_step(self, step: str, detail: str = "") -> None:
        """记录测试上传/查询的阶段，确保 WARNING 级别可见。"""
        message = f"test_upload step={step} {detail}".strip()
        self._trace(message)
        logger.warning(f"【GDrive】DIAG {message}")

    def _record_log(self, action: str, path: str, ok: bool, detail: str = "") -> None:
        """追加一条操作日志并持久化，最多保留 MAX_LOGS 条。"""
        entry = {
            "time": _now_text(),
            "action": str(action)[:40],
            "path": str(path or "")[:300],
            "ok": bool(ok),
            "detail": str(detail or "")[:500],
        }
        self._logs.append(entry)
        if len(self._logs) > MAX_LOGS:
            self._logs = self._logs[-MAX_LOGS:]
        try:
            self.save_data("logs", self._logs)
        except Exception:
            logger.debug("【GDrive】保存操作日志失败", exc_info=True)

    def _log_pages(self) -> int:
        """返回日志总页数。"""
        return max(1, math.ceil(len(self._logs) / self._page_size))

    def _page_logs(self) -> List[Dict[str, Any]]:
        """返回当前页日志。"""
        pages = self._log_pages()
        self._page = min(max(1, self._page), pages)
        start = (self._page - 1) * self._page_size
        return list(reversed(self._logs))[start:start + self._page_size]

    # ------------------------------------------------------------------
    # 表单与页面
    # ------------------------------------------------------------------
    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """返回 Google Drive 配置表单和默认值。"""
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
                                "先在 Google Cloud Console 启用 Google Drive API、创建 OAuth Web 客户端，"
                                "并把下方回调地址登记为授权重定向 URI。插件使用完整 drive 权限。"
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
                                    "props": {"model": "register_storage", "label": "自动注册系统存储"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "display_name",
                                        "label": "存储显示名",
                                        "placeholder": "Google Drive",
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
                                        "model": "client_id",
                                        "label": "OAuth client_id",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "client_secret",
                                        "label": "OAuth client_secret",
                                        "type": "password",
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
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "refresh_token",
                                        "label": "refresh_token（可留空，通过授权链接获取）",
                                        "type": "password",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "oauth_code",
                                        "label": "临时授权码",
                                        "hint": "从回调页复制 code 后保存，再点详情页“用授权码换取令牌”",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "redirect_uri",
                            "label": "OAuth 回调地址",
                            "hint": "必须与 Google Cloud 控制台登记的重定向 URI 完全一致",
                            "persistent-hint": True,
                        },
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
                                        "model": "root_folder_id",
                                        "label": "根目录 ID 或分享链接",
                                        "placeholder": "root 或文件夹 ID",
                                        "hint": "留空/root 表示我的云端硬盘根目录；共享盘可填共享盘文件夹 ID",
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
                                        "model": "root_path",
                                        "label": "虚拟根路径",
                                        "placeholder": "/",
                                        "hint": "默认为 /；填写 /GDrive 时对外挂载到该路径",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "test_path",
                            "label": "测试上传路径",
                            "placeholder": "/",
                            "hint": "详情页“测试上传”写入的目录；留空使用根路径，可填团队盘快捷方式下的目录",
                            "persistent-hint": True,
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
                                    "props": {"model": "use_shared_drive", "label": "使用共享盘"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "shared_drive_id",
                                        "label": "共享盘 ID",
                                        "hint": "共享盘 ID 不是文件夹 ID；可在共享盘 URL 中获取",
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
                                    "component": "VSelect",
                                    "props": {
                                        "model": "delete_mode",
                                        "label": "删除方式",
                                        "items": [
                                            {"title": "移入回收站（推荐）", "value": "trash"},
                                            {"title": "永久删除", "value": "permanent"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "chunk_size_mb",
                                        "label": "上传分片大小（MB）",
                                        "type": "number",
                                        "min": 1,
                                        "max": 1024,
                                        "step": 1,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "timeout",
                                        "label": "HTTP 超时（秒）",
                                        "type": "number",
                                        "min": 5,
                                        "max": 600,
                                        "step": 5,
                                    },
                                }],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "display_name": DEFAULT_DISPLAY_NAME,
            "register_storage": True,
            "client_id": "",
            "client_secret": "",
            "refresh_token": "",
            "redirect_uri": self._default_redirect_uri(),
            "oauth_code": "",
            "root_folder_id": "root",
            "root_path": DEFAULT_ROOT_PATH,
            "test_path": "/",
            "use_shared_drive": False,
            "shared_drive_id": "",
            "delete_mode": DEFAULT_DELETE_MODE,
            "chunk_size_mb": DEFAULT_CHUNK_SIZE_MB,
            "timeout": DEFAULT_TIMEOUT,
        }

    def get_page(self) -> Optional[List[dict]]:
        """返回状态、容量、操作按钮和日志列表。"""
        contents: List[dict] = []
        if not self._enabled:
            contents.append(self._alert("warning", "插件未启用，以下为最近状态和日志。"))
        if not self._client:
            contents.append(self._alert(
                "warning",
                "尚未配置 OAuth 客户端：请填写 client_id、client_secret，"
                "并通过详情页授权链接或手动 refresh_token 完成授权。",
            ))
        elif not self._config.get("refresh_token"):
            contents.append(self._alert(
                "info",
                "OAuth 客户端已配置，但尚未授权：请打开下方授权链接，或填写 refresh_token/授权码。",
            ))
        else:
            contents.append(self._alert(
                "success",
                f"已配置 Google Drive 客户端；根路径 {self._config.get('root_path')}；"
                f"删除方式 {'永久删除' if self._config.get('delete_mode') == 'permanent' else '移入回收站'}。",
            ))
        registered = self._storage_registered()
        contents.append(self._alert(
            "info" if registered else "warning",
            f"系统存储：{'已注册 gdrive' if registered else '未注册 gdrive'}；"
            f"显示名：{self._display_name}；共享盘："
            f"{self._config.get('shared_drive_id') or '未设置'}。",
        ))
        quota_text = "容量尚未刷新，可点击“刷新容量”。"
        if self._last_quota:
            quota_text = (
                f"总容量 {format_size(int(self._last_quota.get('total') or 0))}，"
                f"剩余 {format_size(int(self._last_quota.get('available') or 0))}；"
                f"检查时间 {self._last_quota.get('checked_at') or '-'}。"
            )
        contents.append(self._alert("info", quota_text))
        latest_upload_failure = next(
            (
                entry for entry in reversed(self._logs)
                if entry.get("action") == "upload" and not entry.get("ok")
            ),
            None,
        )
        if latest_upload_failure:
            contents.append(self._alert(
                "error",
                f"最近上传失败：{latest_upload_failure.get('time') or '-'} "
                f"{latest_upload_failure.get('path') or '-'}；"
                f"原因：{latest_upload_failure.get('detail') or '未知'}",
            ))

        buttons: List[dict] = [
            self._page_button("测试连接", "mdi-connection", "/test"),
            self._page_button("测试上传", "mdi-cloud-upload", "/test_upload"),
            self._page_button("刷新容量", "mdi-cloud-refresh", "/quota"),
        ]
        auth_url = self._auth_url()
        if auth_url:
            buttons.append({
                "component": "VBtn",
                "props": {
                    "href": auth_url,
                    "target": "_blank",
                    "color": "primary",
                    "variant": "tonal",
                    "prepend-icon": "mdi-google",
                },
                "text": "打开 Google 授权页",
            })
        if self._config.get("oauth_code"):
            buttons.append(self._page_button("用授权码换取令牌", "mdi-key", "/auth/exchange"))
        buttons.append(self._page_button("清空日志", "mdi-broom", "/logs/clear"))
        contents.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-3 flex-shrink-0"},
            "content": buttons,
        })

        logs = self._page_logs()
        if logs:
            headers = ["时间", "操作", "路径", "结果", "详情"]
            ths = [{"component": "th", "text": header} for header in headers]
            rows = []
            for entry in logs:
                rows.append({
                    "component": "tr",
                    "content": [
                        self._text_cell(entry.get("time") or "-"),
                        self._text_cell(entry.get("action") or "-"),
                        self._text_cell(entry.get("path") or "-", title=entry.get("path")),
                        {
                            "component": "td",
                            "content": [{
                                "component": "VChip",
                                "props": {
                                    "color": "success" if entry.get("ok") else "error",
                                    "size": "small",
                                    "variant": "tonal",
                                },
                                "text": "成功" if entry.get("ok") else "失败",
                            }],
                        },
                        self._text_cell(entry.get("detail") or "-", title=entry.get("detail")),
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
                "props": {"class": "flex-grow-1 d-flex align-center justify-center", "style": "min-height: 0;"},
                "content": [self._alert("info", "暂无操作日志。")],
            })

        pages = self._log_pages()
        contents.append({
            "component": "div",
            "props": {"class": "d-flex align-center justify-center ga-2 mt-2 flex-shrink-0"},
            "content": [
                self._page_button("上一页", "mdi-chevron-left", "/logs/page", {"page": self._page - 1}, disabled=self._page <= 1),
                {"component": "span", "props": {"class": "text-body-2"}, "text": f"第 {self._page} / {pages} 页"},
                self._page_button("下一页", "mdi-chevron-right", "/logs/page", {"page": self._page + 1}, disabled=self._page >= pages),
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
        title_value = str(title or value)
        return {
            "component": "td",
            "props": {
                "style": "max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;",
                "title": title_value,
            },
            "text": value,
        }
