"""GDriveTreeClient 的无网络单元测试。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from _plugin_loader import load_module

client_module = load_module("gdrive_tree_client")
errors = load_module("errors")

GDriveTreeClient = client_module.GDriveTreeClient
GDriveTreeError = errors.GDriveTreeError
GDriveTreeAuthError = errors.GDriveTreeAuthError
GDriveTreeCancelled = errors.GDriveTreeCancelled

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"


class FakeResponse:
    """客户端实际读取的最小响应对象。"""

    def __init__(
        self,
        status_code: int = 200,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        """返回预设 JSON。"""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    @property
    def text(self) -> str:
        """返回 JSON 文本。"""
        return json.dumps(self._payload or {})


class FakeTransport:
    """按顺序返回预设响应，并记录调用参数。"""

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> FakeResponse:
        """返回下一个预设响应。"""
        self.calls.append({
            "method": method,
            "url": url,
            "params": params,
            "data": data,
            "headers": headers,
        })
        if not self.responses:
            raise AssertionError("unexpected request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        """记录关闭。"""
        self.closed = True


def _token(token: str = "access-1") -> FakeResponse:
    """构造令牌响应。"""
    return FakeResponse(payload={"access_token": token, "expires_in": 3600})


def _make_client(transport: FakeTransport) -> Any:
    """构造注入了 FakeTransport 的客户端。"""
    return GDriveTreeClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        transport=transport,
    )


def test_resolve_root_alias() -> None:
    """root 别名解析为真实 ID，使用 user corpora。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"id": "REALROOT", "name": "My Drive", "mimeType": FOLDER}),
    ])
    root = _make_client(transport).resolve_root("root")
    assert root.file_id == "REALROOT"
    assert root.corpora == "allDrives"
    assert root.drive_id == ""
    assert transport.calls[1]["url"].endswith("/files/root")


def test_resolve_root_shared_drive_folder() -> None:
    """根带 driveId 时选择 drive corpora。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={
            "id": "F1",
            "name": "Shared",
            "mimeType": FOLDER,
            "driveId": "D1",
        }),
    ])
    root = _make_client(transport).resolve_root("F1")
    assert root.corpora == "drive"
    assert root.drive_id == "D1"


def test_resolve_root_shortcut_follows_target() -> None:
    """根是快捷方式时跟随 targetId。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={
            "id": "SC",
            "name": "Shortcut",
            "mimeType": SHORTCUT,
            "shortcutDetails": {"targetId": "T1"},
        }),
        FakeResponse(payload={
            "id": "T1",
            "name": "Target",
            "mimeType": FOLDER,
            "driveId": "D1",
        }),
    ])
    root = _make_client(transport).resolve_root("SC")
    assert root.file_id == "T1"
    assert root.corpora == "drive"
    assert transport.calls[2]["url"].endswith("/files/T1")


def test_list_all_paginates_and_filters_trashed() -> None:
    """分页拼接并过滤回收站文件。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={
            "files": [{"id": "1", "name": "a"}, {"id": "2", "trashed": True}],
            "nextPageToken": "next",
        }),
        FakeResponse(payload={"files": [{"id": "3", "name": "c"}]}),
    ])
    files, pages = _make_client(transport).list_all(max_pages=10)
    assert [item["id"] for item in files] == ["1", "3"]
    assert pages == 2
    first_query = transport.calls[1]["params"]
    assert first_query["q"] == "trashed = false"
    assert first_query["corpora"] == "user"
    assert first_query["pageSize"] == 1000
    assert transport.calls[2]["params"]["pageToken"] == "next"


def test_list_all_drive_corpora() -> None:
    """共享盘模式带 driveId。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"files": []}),
    ])
    _make_client(transport).list_all(corpora="drive", drive_id="D1")
    params = transport.calls[1]["params"]
    assert params["corpora"] == "drive"
    assert params["driveId"] == "D1"


def test_list_all_all_drives_corpora() -> None:
    """allDrives 模式同时覆盖我的云端硬盘与共享盘。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"files": []}),
    ])
    _make_client(transport).list_all(corpora="allDrives")
    params = transport.calls[1]["params"]
    assert params["corpora"] == "allDrives"
    assert "driveId" not in params


def test_list_all_rejects_duplicate_token() -> None:
    """分页令牌重复时中止，避免死循环。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"files": [], "nextPageToken": "same"}),
        FakeResponse(payload={"files": [], "nextPageToken": "same"}),
    ])
    with pytest.raises(GDriveTreeError):
        _make_client(transport).list_all(max_pages=10)


def test_list_all_respects_max_pages() -> None:
    """超过页数上限时抛错。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"files": [], "nextPageToken": "next"}),
    ])
    with pytest.raises(GDriveTreeError):
        _make_client(transport).list_all(max_pages=1)


def test_list_all_respects_stop_event() -> None:
    """stop_event 置位时立即中止，不发请求。"""
    import threading

    event = threading.Event()
    event.set()
    transport = FakeTransport([])
    with pytest.raises(GDriveTreeCancelled):
        _make_client(transport).list_all(stop_event=event)
    assert transport.calls == []


def test_401_refreshes_token_and_retries() -> None:
    """401 时强制刷新令牌并重试一次。"""
    transport = FakeTransport([
        _token("access-1"),
        FakeResponse(status_code=401, payload={"error": {"message": "expired"}}),
        _token("access-2"),
        FakeResponse(payload={"files": []}),
    ])
    files, pages = _make_client(transport).list_all()
    assert files == []
    assert pages == 1
    assert len(transport.calls) == 4
    assert transport.calls[3]["headers"]["Authorization"] == "Bearer access-2"


def test_invalid_grant_raises_auth_error() -> None:
    """refresh_token 失效（OAuth 错误体）时抛授权错误。"""
    transport = FakeTransport([
        FakeResponse(
            status_code=400,
            payload={
                "error": "invalid_grant",
                "error_description": "Token has been expired or revoked.",
            },
        ),
    ])
    with pytest.raises(GDriveTreeAuthError):
        _make_client(transport).list_all()


def test_root_must_be_folder() -> None:
    """根指向文件时明确报错。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={
            "id": "F1",
            "name": "a.mkv",
            "mimeType": "video/x-matroska",
        }),
    ])
    with pytest.raises(GDriveTreeError) as excinfo:
        _make_client(transport).resolve_root("F1")
    assert "不是文件夹" in str(excinfo.value)


def test_resolve_root_404_includes_root_id() -> None:
    """根目录 404 时错误信息包含根 ID。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(
            status_code=404,
            payload={"error": {"message": "File not found", "errors": [{"reason": "notFound"}]}},
        ),
    ])
    with pytest.raises(GDriveTreeError) as excinfo:
        _make_client(transport).resolve_root("BADID")
    assert "BADID" in str(excinfo.value)
    assert excinfo.value.status_code == 404


def test_incomplete_search_aborts() -> None:
    """incompleteSearch=true 时中止，避免假完整快照。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={"files": [], "incompleteSearch": True}),
    ])
    with pytest.raises(GDriveTreeError):
        _make_client(transport).list_all()


def test_max_files_aborts() -> None:
    """分页过程中文件数超限时提前中止。"""
    transport = FakeTransport([
        _token(),
        FakeResponse(payload={
            "files": [{"id": "1"}, {"id": "2"}],
        }),
    ])
    with pytest.raises(GDriveTreeError):
        _make_client(transport).list_all(max_files=1)


def test_network_error_is_retried(monkeypatch: Any) -> None:
    """传输层异常按指数退避重试。"""
    monkeypatch.setattr(
        client_module.GDriveTreeClient, "_sleep", staticmethod(lambda attempt, event: None)
    )
    transport = FakeTransport([
        _token(),
        ConnectionError("connection reset"),
        FakeResponse(payload={"files": []}),
    ])
    files, pages = _make_client(transport).list_all()
    assert files == []
    assert pages == 1
    assert len(transport.calls) == 3


def test_403_rate_limit_is_retried(monkeypatch: Any) -> None:
    """403 限流按指数退避重试。"""
    monkeypatch.setattr(
        client_module.GDriveTreeClient, "_sleep", staticmethod(lambda attempt, event: None)
    )
    transport = FakeTransport([
        _token(),
        FakeResponse(
            status_code=403,
            payload={"error": {
                "message": "Rate Limit Exceeded",
                "errors": [{"reason": "rateLimitExceeded"}],
            }},
        ),
        FakeResponse(payload={"files": []}),
    ])
    files, pages = _make_client(transport).list_all()
    assert files == []
    assert pages == 1
    assert len(transport.calls) == 3


def test_missing_refresh_token_rejected() -> None:
    """没有 refresh_token 时直接报授权错误。"""
    client = GDriveTreeClient(
        client_id="cid",
        client_secret="secret",
        refresh_token="",
        transport=FakeTransport([]),
    )
    with pytest.raises(GDriveTreeAuthError):
        client.refresh_access_token()


def test_close_closes_transport() -> None:
    """close 传递到传输层。"""
    transport = FakeTransport([])
    _make_client(transport).close()
    assert transport.closed is True
