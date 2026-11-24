"""Google Drive 配置同步的纯推送逻辑。

本模块只依赖标准库，不导入 ``app.*``，可在无 MoviePilot 宿主的环境直接测试：

- :func:`build_payload` 构造四字段请求体；
- :func:`validate_sync_url` 校验接收端 URL；
- :class:`HttpxSyncTransport` 延迟导入 ``httpx2``（回退 ``httpx``）发送请求；
- :class:`GDriveConfigSyncClient` 把一次推送的所有异常收敛为 :class:`SyncResult`。
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Protocol, Tuple
from urllib.parse import urlparse

DEFAULT_SYNC_TIMEOUT = 30
MAX_RESPONSE_SNIPPET = 200
USER_AGENT_PREFIX = "MoviePilot-GDriveStorage"


def _now_text() -> str:
    """返回本地时间文本。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_ms(started: float) -> int:
    """返回从 ``time.monotonic()`` 起算的毫秒数。"""
    return max(0, int((time.monotonic() - started) * 1000))


def _snippet(text: Any) -> str:
    """把响应文本截断为最多 200 字符，且去掉首尾空白。"""
    value = str(text or "").strip()
    return value[:MAX_RESPONSE_SNIPPET]


def _is_timeout_error(err: BaseException) -> bool:
    """判断异常是否属于超时；不导入传输层依赖。"""
    if isinstance(err, TimeoutError):
        return True
    return "timeout" in type(err).__name__.lower()


@dataclass(frozen=True)
class SyncResult:
    """一次配置推送的结果。"""

    ok: bool
    status_code: Optional[int]
    message: str
    elapsed_ms: int
    trigger: str
    at: str

    def to_dict(self) -> Dict[str, Any]:
        """返回可持久化/可展示的六字段字典。"""
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
            "trigger": self.trigger,
            "at": self.at,
        }


def build_payload(
        client_id: Any,
        client_secret: Any,
        refresh_token: Any,
        token: Any,
) -> Dict[str, str]:
    """构造接收端请求体：恰好四个字段，值为字符串。"""
    return {
        "client_id": str(client_id or ""),
        "client_secret": str(client_secret or ""),
        "refresh_token": str(refresh_token or ""),
        "token": str(token or ""),
    }


def validate_sync_url(url: str) -> Tuple[bool, str]:
    """
    校验接收端 URL。

    :param url: 用户填写的完整接口地址
    :return: ``(是否合法, 可读原因)``；合法时原因为空字符串
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "未配置同步接口 URL"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False, "同步接口 URL 无法解析"
    if parsed.scheme not in ("http", "https"):
        return False, "同步接口 URL 必须以 http:// 或 https:// 开头"
    if not parsed.netloc:
        return False, "同步接口 URL 缺少主机名"
    return True, ""


class SyncTransport(Protocol):
    """推送客户端依赖的最小传输层协议。"""

    def post(
            self,
            url: str,
            headers: Dict[str, str],
            content: bytes,
            timeout: float,
    ) -> Tuple[int, str]:
        """发送一次 POST 请求并返回 ``(status_code, response_text)``。"""


class HttpxSyncTransport:
    """基于 httpx2（回退 httpx）的同步传输层。"""

    def __init__(self, timeout: float = DEFAULT_SYNC_TIMEOUT) -> None:
        """
        创建不跟随重定向的同步 HTTP 客户端。

        :param timeout: 单次请求总超时（秒）
        """
        try:
            import httpx2 as _httpx
        except ImportError:  # pragma: no cover - 宿主已提供 httpx2
            import httpx as _httpx  # type: ignore[no-redef]
        self._client: Optional[Any] = _httpx.Client(
            timeout=timeout, follow_redirects=False,
        )
        self._lock = threading.Lock()

    def post(
            self,
            url: str,
            headers: Dict[str, str],
            content: bytes,
            timeout: float,
    ) -> Tuple[int, str]:
        """发送 POST；不跟随重定向，3xx 交给调用方判定为失败。"""
        client = self._client
        if client is None:
            raise RuntimeError("HTTP 客户端已关闭")
        response = client.post(url, headers=headers, content=content, timeout=timeout)
        return int(response.status_code), str(response.text)

    def close(self) -> None:
        """关闭底层连接池，可重复调用。"""
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - 关闭失败不影响插件
                pass


class GDriveConfigSyncClient:
    """把插件配置推送到用户指定接口的客户端。"""

    def __init__(
            self,
            version: str,
            timeout: float = DEFAULT_SYNC_TIMEOUT,
            transport: Optional[SyncTransport] = None,
    ) -> None:
        """
        :param version: 插件版本，写入 ``User-Agent``
        :param timeout: 单次请求总超时（秒）
        :param transport: 可注入的传输层，便于无网络测试
        """
        self._version = str(version or "").strip() or "unknown"
        self._timeout = float(timeout or DEFAULT_SYNC_TIMEOUT)
        self._user_agent = f"{USER_AGENT_PREFIX}/{self._version}"
        self._transport = (
            transport if transport is not None
            else HttpxSyncTransport(timeout=self._timeout)
        )

    @property
    def user_agent(self) -> str:
        """返回本次推送使用的 User-Agent。"""
        return self._user_agent

    def _result(
            self,
            ok: bool,
            status_code: Optional[int],
            message: str,
            started: float,
            trigger: str,
    ) -> SyncResult:
        """构造结果对象。"""
        return SyncResult(
            ok=ok,
            status_code=status_code,
            message=message,
            elapsed_ms=_elapsed_ms(started),
            trigger=str(trigger or "manual"),
            at=_now_text(),
        )

    def push(
            self,
            url: str,
            payload: Dict[str, str],
            trigger: str = "manual",
    ) -> SyncResult:
        """
        发送一次配置推送；任何失败都返回 ``ok=False`` 的结果，不抛出异常。

        :param url: 接收端完整 URL
        :param payload: :func:`build_payload` 构造的四字段字典
        :param trigger: ``"scheduler"`` 或 ``"manual"``
        :return: 推送结果
        """
        started = time.monotonic()
        valid, reason = validate_sync_url(url)
        if not valid:
            return self._result(False, None, f"同步接口 URL 无效：{reason}", started, trigger)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        try:
            content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except Exception as err:
            return self._result(
                False, None, f"请求体序列化失败：{type(err).__name__}", started, trigger,
            )

        try:
            response = self._transport.post(url, headers, content, self._timeout)
        except Exception as err:
            if _is_timeout_error(err):
                message = f"请求超时：{type(err).__name__}"
            else:
                message = f"请求失败：{type(err).__name__}：{_snippet(str(err))}"
            return self._result(False, None, message, started, trigger)

        if inspect.isawaitable(response):
            # 同步路径无法等待协程；主动关闭，避免出现未等待的协程告警。
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            return self._result(
                False, None, "传输层返回了异步结果，无法同步处理", started, trigger,
            )

        try:
            status_code, response_text = response
            status = int(status_code)
        except Exception as err:
            return self._result(
                False,
                None,
                f"响应格式无效：{type(err).__name__}",
                started,
                trigger,
            )

        snippet = _snippet(response_text)
        if 200 <= status < 300:
            return self._result(True, status, f"HTTP {status} 成功", started, trigger)
        message = f"HTTP {status}：{snippet}" if snippet else f"HTTP {status}"
        return self._result(False, status, message, started, trigger)

    def close(self) -> None:
        """关闭传输层，可重复调用。"""
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()


__all__ = [
    "DEFAULT_SYNC_TIMEOUT",
    "MAX_RESPONSE_SNIPPET",
    "GDriveConfigSyncClient",
    "HttpxSyncTransport",
    "SyncResult",
    "SyncTransport",
    "build_payload",
    "validate_sync_url",
]
