"""Google Drive 目录树抓取客户端（只读）。

只负责 OAuth 刷新与 ``files.list`` 全量分页，不依赖 MoviePilot 宿主。
HTTP 传输层可在测试中注入，默认延迟导入 ``httpx2``，回退 ``httpx``。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .errors import GDriveTreeAuthError, GDriveTreeCancelled, GDriveTreeError
from .tree_builder import FOLDER_MIME, SHORTCUT_MIME

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"

ROOT_FIELDS = "id,name,mimeType,driveId,parents,shortcutDetails"
FILE_FIELDS = (
    "id,name,mimeType,parents,size,modifiedTime,createdTime,"
    "md5Checksum,trashed,driveId,shortcutDetails"
)
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


@dataclass(frozen=True, slots=True)
class RootInfo:
    """解析后的树根信息。"""

    file_id: str
    name: str
    drive_id: str
    corpora: str


class _HttpxTransport:
    """基于 httpx2（缺失时回退 httpx）的同步传输层。"""

    def __init__(self, timeout: float) -> None:
        try:
            import httpx2 as httpx  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 宿主通常提供 httpx2
            import httpx  # type: ignore[no-redef]
        self._client = httpx.Client(timeout=timeout)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """发送一次同步 HTTP 请求。"""
        return self._client.request(
            method=method,
            url=url,
            params=params,
            data=data,
            headers=headers,
        )

    def close(self) -> None:
        """关闭底层连接池。"""
        self._client.close()


def _to_int(value: Any, default: int) -> int:
    """把配置值转成整数并做下限保护。"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class GDriveTreeClient:
    """Google Drive 只读客户端：OAuth 刷新、根解析、全量分页。"""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout: float = 60,
        transport: Optional[Any] = None,
    ) -> None:
        """
        保存凭据并准备传输层；构造期不发网络请求。

        :param client_id: Google OAuth client_id
        :param client_secret: Google OAuth client_secret
        :param refresh_token: 已授权的 refresh_token
        :param timeout: HTTP 超时秒数
        :param transport: 可注入的传输层，需提供 request/close
        """
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.timeout = max(5.0, float(timeout or 60))
        self._transport = transport or _HttpxTransport(self.timeout)
        self._access_token = ""
        self._expires_at = 0.0
        self._token_lock = threading.RLock()

    # ------------------------------------------------------------------
    # OAuth2
    # ------------------------------------------------------------------
    def refresh_access_token(
        self,
        force: bool = False,
        stop_event: Optional[Any] = None,
    ) -> str:
        """
        刷新并缓存 access_token。

        :param force: 忽略缓存强制刷新
        :param stop_event: 可选中止事件
        :return: 可用 access_token
        :raises GDriveTreeAuthError: 凭据缺失或 Google 拒绝刷新
        """
        with self._token_lock:
            now = time.monotonic()
            if not force and self._access_token and now < self._expires_at:
                return self._access_token
            if not self.refresh_token:
                raise GDriveTreeAuthError(
                    "尚未配置 refresh_token，请先在 GDriveStorage 完成 Google 授权"
                )
            if not self.client_id or not self.client_secret:
                raise GDriveTreeAuthError(
                    "GDriveStorage 缺少 client_id/client_secret，请先完成配置"
                )
            payload = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
            try:
                response = self._request(
                    "POST",
                    TOKEN_URL,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth=False,
                    retry=True,
                    stop_event=stop_event,
                )
            except GDriveTreeError as err:
                raise GDriveTreeAuthError(
                    f"刷新 access_token 失败，refresh_token 可能已失效：{err}"
                ) from err
            data = self._json(response)
            access_token = str(data.get("access_token") or "")
            if not access_token:
                raise GDriveTreeAuthError(
                    "Google 未返回 access_token，refresh_token 可能已失效"
                )
            expires_in = _to_int(data.get("expires_in"), 3600)
            self._access_token = access_token
            self._expires_at = time.monotonic() + max(60, expires_in - 60)
            return access_token

    # ------------------------------------------------------------------
    # Drive 读取
    # ------------------------------------------------------------------
    def get_file(self, file_id: str, fields: str = ROOT_FIELDS) -> Dict[str, Any]:
        """
        读取单个文件/文件夹元数据。

        :param file_id: 文件 ID；``root`` 表示账号根
        :param fields: 需要的字段
        :return: 元数据字典
        """
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{quote(str(file_id), safe='')}",
            params={"fields": fields, "supportsAllDrives": "true"},
        )
        return self._json(response)

    def resolve_root(
        self,
        root_folder_id: str,
        fallback_name: str = "",
    ) -> RootInfo:
        """
        解析配置的根目录，处理 ``root`` 别名、快捷方式和共享盘。

        :param root_folder_id: GDriveStorage 的 root_folder_id
        :param fallback_name: 元数据缺少名称时使用的显示名
        :return: 真实根 ID、名称与 corpora
        """
        current = str(root_folder_id or "root").strip() or "root"
        seen: set[str] = set()
        meta: Dict[str, Any] = {}
        for _ in range(4):
            if current in seen:
                raise GDriveTreeError("根目录快捷方式形成循环，无法解析")
            seen.add(current)
            try:
                meta = self.get_file(current)
            except GDriveTreeError as err:
                if err.status_code in (403, 404):
                    raise GDriveTreeError(
                        f"根目录无法访问（ID={current}）：{err}；"
                        "请检查 GDriveStorage 的 root_folder_id 与共享盘权限",
                        status_code=err.status_code,
                    ) from err
                raise
            if str(meta.get("mimeType") or "") != SHORTCUT_MIME:
                break
            target = str((meta.get("shortcutDetails") or {}).get("targetId") or "")
            if not target:
                raise GDriveTreeError("根目录是快捷方式，但缺少 targetId")
            current = target
        else:
            raise GDriveTreeError("根目录快捷方式层级过深，无法解析")

        file_id = str(meta.get("id") or current)
        mime_type = str(meta.get("mimeType") or "")
        if mime_type and mime_type != FOLDER_MIME:
            raise GDriveTreeError(
                f"根目录不是文件夹（ID={file_id}，mimeType={mime_type}）；"
                "请把 GDriveStorage 的 root_folder_id 指向文件夹"
            )
        drive_id = str(meta.get("driveId") or "")
        return RootInfo(
            file_id=file_id,
            name=str(meta.get("name") or fallback_name or "/"),
            drive_id=drive_id,
            # 没有 driveId 时用 allDrives：既覆盖「我的云端硬盘」，也覆盖
            # 「共享给我」的文件夹和用户可访问的共享盘，避免只抓到根节点。
            corpora="drive" if drive_id else "allDrives",
        )

    def list_all(
        self,
        *,
        corpora: str = "user",
        drive_id: str = "",
        include_trashed: bool = False,
        max_pages: int = 2000,
        max_files: Optional[int] = None,
        stop_event: Optional[Any] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        分页拉取可枚举范围内的全部文件元数据。

        :param corpora: ``user`` 或 ``drive``
        :param drive_id: 共享盘 ID（corpora=drive 时必填）
        :param include_trashed: 是否包含回收站文件
        :param max_pages: 最大页数保护
        :param max_files: 最大文件数保护（在分页过程中提前中止）
        :param stop_event: 可选 ``threading.Event``，置位后中止
        :return: (文件元数据列表, 请求页数)
        """
        if corpora == "drive" and not drive_id:
            raise GDriveTreeError("共享盘模式缺少 driveId，无法列举文件")
        files: List[Dict[str, Any]] = []
        pages = 0
        page_token: Optional[str] = None
        seen_tokens: set[str] = set()
        max_pages = max(1, int(max_pages or 1))

        while True:
            if stop_event is not None and stop_event.is_set():
                raise GDriveTreeCancelled("刷新已被主动中止")
            if pages >= max_pages:
                raise GDriveTreeError(
                    f"分页超过上限 {max_pages} 页，已中止；请缩小根目录范围或调大 max_pages"
                )
            params: Dict[str, Any] = {
                "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if corpora == "drive":
                params["corpora"] = "drive"
                params["driveId"] = drive_id
            elif corpora == "allDrives":
                params["corpora"] = "allDrives"
            else:
                params["corpora"] = "user"
            if not include_trashed:
                params["q"] = "trashed = false"
            if page_token:
                params["pageToken"] = page_token
            response = self._request(
                "GET", f"{DRIVE_API}/files", params=params, stop_event=stop_event
            )
            data = self._json(response)
            if data.get("incompleteSearch"):
                raise GDriveTreeError(
                    "Google Drive 返回不完整的搜索结果（incompleteSearch=true），"
                    "本次快照可能缺失节点，已中止"
                )
            pages += 1
            for raw in data.get("files") or []:
                if not isinstance(raw, dict):
                    continue
                if not include_trashed and raw.get("trashed"):
                    continue
                files.append(raw)
            if max_files is not None and len(files) > max_files:
                raise GDriveTreeError(
                    f"文件数超过上限 {max_files}，已中止；请缩小根目录范围或调大 max_nodes"
                )
            next_token = data.get("nextPageToken")
            if not next_token:
                return files, pages
            token_text = str(next_token)
            if token_text in seen_tokens:
                raise GDriveTreeError("Google Drive 分页令牌重复，已中止以避免死循环")
            seen_tokens.add(token_text)
            page_token = token_text

    def find_child(self, parent_id: str, name: str) -> Optional[Dict[str, Any]]:
        """
        在指定目录下按名称查找子项（用于归档记录只提供路径时补元数据）。

        :param parent_id: 父目录 ID
        :param name: 文件/目录名
        :return: 元数据；不存在返回 None
        """
        query = (
            f"'{self._escape_query(parent_id)}' in parents "
            f"and name = '{self._escape_query(name)}' and trashed = false"
        )
        params: Dict[str, Any] = {
            "q": query,
            "fields": f"files({FILE_FIELDS})",
            "pageSize": 100,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        }
        data = self._json(
            self._request("GET", f"{DRIVE_API}/files", params=params)
        )
        files = data.get("files") or []
        for item in files:
            if isinstance(item, dict):
                return item
        return None

    @staticmethod
    def _escape_query(value: Any) -> str:
        """转义 Drive 查询字符串中的反斜杠和单引号。"""
        return str(value).replace("\\", "\\\\").replace("'", "\\'")

    def close(self) -> None:
        """关闭传输层。"""
        try:
            self._transport.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = True,
        retry: bool = True,
        stop_event: Optional[Any] = None,
    ) -> Any:
        """发送请求；网络错误/401/429/5xx/403 限流按策略重试。"""
        refreshed = False
        attempt = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise GDriveTreeCancelled("刷新已被主动中止")
            request_headers = dict(headers or {})
            if auth:
                request_headers["Authorization"] = (
                    f"Bearer {self.refresh_access_token(stop_event=stop_event)}"
                )
            try:
                response = self._transport.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                )
            except Exception as err:
                if retry and attempt < MAX_RETRIES:
                    attempt += 1
                    self._sleep(attempt, stop_event)
                    continue
                raise GDriveTreeError(f"请求 Google Drive 失败：{err}") from err
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 401 and auth and not refreshed:
                refreshed = True
                self.refresh_access_token(force=True, stop_event=stop_event)
                continue
            retryable = status in RETRY_STATUS or self._is_rate_limited(response)
            if retryable and retry and attempt < MAX_RETRIES:
                attempt += 1
                self._sleep(attempt, stop_event)
                continue
            if status >= 400:
                self._raise_for_response(response)
            return response

    @staticmethod
    def _sleep(attempt: int, stop_event: Optional[Any]) -> None:
        """指数退避；可中止时用 Event.wait。"""
        delay = min(2 ** attempt, 8)
        if stop_event is not None:
            if stop_event.wait(delay):
                raise GDriveTreeCancelled("刷新已被主动中止")
        else:
            time.sleep(delay)

    @staticmethod
    def _is_rate_limited(response: Any) -> bool:
        """判断 403 是否为限流/后端瞬时错误。"""
        if int(getattr(response, "status_code", 0) or 0) != 403:
            return False
        try:
            payload = response.json()
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        if isinstance(error, dict):
            reasons = {
                str(item.get("reason"))
                for item in (error.get("errors") or [])
                if isinstance(item, dict) and item.get("reason")
            }
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}:
                return True
            message = str(error.get("message") or "").lower()
        else:
            message = str(error or payload.get("error_description") or "").lower()
        return "rate limit" in message or "quota" in message

    def _raise_for_response(self, response: Any) -> None:
        """把 Google 错误响应转换成可读异常。"""
        status = int(getattr(response, "status_code", 0) or 0)
        payload: Dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
        error = payload.get("error")
        reasons: List[str] = []
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            reasons = [
                str(item.get("reason"))
                for item in (error.get("errors") or [])
                if isinstance(item, dict) and item.get("reason")
            ]
        else:
            # OAuth 令牌端点使用 {"error": "invalid_grant", ...}
            message = str(
                error
                or payload.get("error_description")
                or getattr(response, "text", "")
                or ""
            ).strip()
        reason_text = f"（{reasons[0]}）" if reasons else ""
        detail = (message or "Google 未返回错误详情")[:400]
        if status == 401:
            raise GDriveTreeAuthError(
                f"Google Drive 认证失败 HTTP 401{reason_text}：{detail}",
                status_code=status,
            )
        raise GDriveTreeError(
            f"Google Drive 请求失败 HTTP {status}{reason_text}：{detail}",
            status_code=status,
        )

    @staticmethod
    def _json(response: Any) -> Dict[str, Any]:
        """解析响应 JSON；解析失败抛统一错误。"""
        try:
            parsed = response.json()
        except Exception:
            try:
                parsed = json.loads(getattr(response, "text", "") or "{}")
            except Exception as err:
                raise GDriveTreeError("Google Drive 返回了无法解析的响应") from err
        if not isinstance(parsed, dict):
            raise GDriveTreeError("Google Drive 返回的响应不是 JSON 对象")
        return parsed
