"""GoogleDrive 配置同步推送模块的无网络单元测试。"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SYNC_PATH = REPO_ROOT / "plugins.v3" / "gdrivestorage" / "sync_client.py"


def _load_sync_module():
    """按文件路径加载推送模块，避免依赖 MoviePilot 宿主包。"""
    spec = importlib.util.spec_from_file_location(
        "gdrive_sync_client_under_test", SYNC_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_module = _load_sync_module()
GDriveConfigSyncClient = sync_module.GDriveConfigSyncClient
SyncResult = sync_module.SyncResult
build_payload = sync_module.build_payload
validate_sync_url = sync_module.validate_sync_url


class FakeTransport:
    """记录调用并返回预设响应的传输层替身。"""

    def __init__(
            self,
            status: int = 200,
            text: str = "",
            error: Optional[BaseException] = None,
    ) -> None:
        self.status = status
        self.text = text
        self.error = error
        self.calls: List[Dict[str, Any]] = []
        self.close_calls = 0

    def post(
            self,
            url: str,
            headers: Dict[str, str],
            content: bytes,
            timeout: float,
    ) -> Tuple[int, str]:
        """记录参数并返回或抛出预设结果。"""
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "content": content,
            "timeout": timeout,
        })
        if self.error is not None:
            raise self.error
        return self.status, self.text

    def close(self) -> None:
        """记录关闭次数。"""
        self.close_calls += 1


def _payload() -> Dict[str, str]:
    """返回测试用四字段请求体。"""
    return build_payload("cid", "csecret", "refresh", "shared-token")


def test_build_payload_has_exactly_four_fields() -> None:
    """请求体恰好四个字段且值原样保留。"""
    payload = build_payload(" id ", "secret", "refresh", "token")
    assert list(payload) == ["client_id", "client_secret", "refresh_token", "token"]
    assert payload == {
        "client_id": " id ",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "token": "token",
    }


def test_build_payload_coerces_empty_values_to_empty_strings() -> None:
    """空值和 None 都归一化为空字符串。"""
    payload = build_payload(None, None, None, None)
    assert payload == {
        "client_id": "",
        "client_secret": "",
        "refresh_token": "",
        "token": "",
    }


def test_validate_sync_url_accepts_http_and_https() -> None:
    """http/https 且带主机名的 URL 合法。"""
    assert validate_sync_url("https://example.com/api/gdrive/config") == (True, "")
    assert validate_sync_url("http://192.168.1.2:8080/hook") == (True, "")


def test_validate_sync_url_rejects_empty_and_invalid_values() -> None:
    """空 URL、非 http(s)、缺少主机和相对路径都必须失败。"""
    ok, reason = validate_sync_url("")
    assert ok is False
    assert reason == "未配置同步接口 URL"

    for value in ("ftp://example.com/hook", "example.com/hook", "/relative", "https://"):
        ok, reason = validate_sync_url(value)
        assert ok is False, value
        assert reason


def test_push_treats_2xx_as_success() -> None:
    """200 和 204 都视为成功。"""
    for status in (200, 204):
        transport = FakeTransport(status=status, text="")
        client = GDriveConfigSyncClient("1.1.4", timeout=10, transport=transport)
        result = client.push("https://example.com/hook", _payload())
        assert result.ok is True
        assert result.status_code == status
        assert result.trigger == "manual"
        assert result.at


def test_push_marks_redirect_and_error_status_as_failure() -> None:
    """3xx/4xx/5xx 都是失败，并保留状态码。"""
    for status in (301, 401, 500):
        transport = FakeTransport(status=status, text="bad request")
        client = GDriveConfigSyncClient("1.1.4", transport=transport)
        result = client.push("https://example.com/hook", _payload())
        assert result.ok is False
        assert result.status_code == status
        assert f"HTTP {status}" in result.message


def test_push_maps_timeout_to_readable_failure() -> None:
    """超时异常返回 ok=False、无状态码和可读原因。"""
    transport = FakeTransport(error=TimeoutError("timed out"))
    client = GDriveConfigSyncClient("1.1.4", transport=transport)
    result = client.push("https://example.com/hook", _payload())
    assert result.ok is False
    assert result.status_code is None
    assert "请求超时" in result.message


def test_push_maps_connection_error_to_readable_failure() -> None:
    """连接错误不会抛出，转换为失败结果。"""
    transport = FakeTransport(error=ConnectionError("connection refused"))
    client = GDriveConfigSyncClient("1.1.4", transport=transport)
    result = client.push("https://example.com/hook", _payload())
    assert result.ok is False
    assert result.status_code is None
    assert "请求失败" in result.message


def test_push_truncates_long_response_snippet_to_200_chars() -> None:
    """失败响应片段最多保留 200 字符。"""
    body = "x" * 500
    transport = FakeTransport(status=500, text=body)
    client = GDriveConfigSyncClient("1.1.4", transport=transport)
    result = client.push("https://example.com/hook", _payload())
    snippet = result.message.split("：", 1)[1]
    assert result.ok is False
    assert len(snippet) == sync_module.MAX_RESPONSE_SNIPPET == 200
    assert snippet == "x" * 200


def test_push_sends_expected_headers_timeout_and_json_body() -> None:
    """请求头、超时和 JSON 请求体必须符合契约。"""
    transport = FakeTransport(status=200)
    client = GDriveConfigSyncClient("1.1.4", timeout=12.5, transport=transport)
    payload = _payload()
    client.push("https://example.com/hook", payload, trigger="scheduler")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://example.com/hook"
    assert call["timeout"] == 12.5
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["User-Agent"] == "MoviePilot-GDriveStorage/1.1.4"
    assert json.loads(call["content"].decode("utf-8")) == payload


def test_push_does_not_forward_when_url_is_invalid() -> None:
    """URL 非法时不发起请求，直接返回失败。"""
    transport = FakeTransport(status=200)
    client = GDriveConfigSyncClient("1.1.4", transport=transport)
    result = client.push("ftp://example.com/hook", _payload())
    assert result.ok is False
    assert result.status_code is None
    assert "同步接口 URL 无效" in result.message
    assert transport.calls == []


def test_sync_result_to_dict_has_six_fields() -> None:
    """结果对象序列化为六字段字典。"""
    result = SyncResult(
        ok=True,
        status_code=200,
        message="HTTP 200 成功",
        elapsed_ms=7,
        trigger="manual",
        at="2026-11-24 12:00:00",
    )
    assert result.to_dict() == {
        "ok": True,
        "status_code": 200,
        "message": "HTTP 200 成功",
        "elapsed_ms": 7,
        "trigger": "manual",
        "at": "2026-11-24 12:00:00",
    }


def test_close_is_idempotent_and_forwards_to_transport() -> None:
    """close 可重复调用。"""
    transport = FakeTransport()
    client = GDriveConfigSyncClient("1.1.4", transport=transport)
    client.close()
    assert transport.close_calls == 1


def test_httpx_transport_disables_redirects_and_closes_idempotently() -> None:
    """真实传输层必须构造不跟随重定向的客户端，且可重复关闭。"""
    try:
        import httpx2  # noqa: F401
    except ImportError:
        pytest.importorskip("httpx")
    transport = sync_module.HttpxSyncTransport(timeout=5)
    try:
        assert transport._client is not None
        assert transport._client.follow_redirects is False
    finally:
        transport.close()
        transport.close()
        assert transport._client is None


def test_module_does_not_import_host_app_packages() -> None:
    """推送模块不得依赖 ``app.*``。"""
    tree = ast.parse(SYNC_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app."), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app."), alias.name
