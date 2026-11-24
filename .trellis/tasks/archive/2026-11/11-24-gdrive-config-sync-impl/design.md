# GDriveStorage 配置同步：插件端实现 — 技术设计

父设计：`../11-24-gdrive-config-sync/design.md`（请求契约、配置表、错误矩阵以此为准）
本文件只补充代码级实现细节。

## 1. `sync_client.py`

### 1.1 常量与结果

```python
DEFAULT_SYNC_TIMEOUT = 30
MAX_RESPONSE_SNIPPET = 200

@dataclass(frozen=True)
class SyncResult:
    ok: bool
    status_code: Optional[int]
    message: str
    elapsed_ms: int
    trigger: str
    at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
            "trigger": self.trigger,
            "at": self.at,
        }
```

`at` 使用 `datetime.now().strftime("%Y-%m-%d %H:%M:%S")`。

### 1.2 payload 与 URL

```python
def build_payload(client_id, client_secret, refresh_token, token) -> Dict[str, str]:
    return {
        "client_id": str(client_id or ""),
        "client_secret": str(client_secret or ""),
        "refresh_token": str(refresh_token or ""),
        "token": str(token or ""),
    }
```

- 不做 strip，保留用户原值（配置归一化阶段已 strip）。
- `validate_sync_url` 使用 `urlparse`：scheme ∈ {http, https}，`netloc` 非空；返回可读中文原因。

### 1.3 传输层

```python
class SyncTransport(Protocol):
    def post(self, url: str, headers: Dict[str, str], content: bytes,
             timeout: float) -> Tuple[int, str]: ...
```

`HttpxSyncTransport`：

- 延迟导入：`import httpx2`，`ImportError` 时 `import httpx`。
- `httpx.Client(timeout=timeout, follow_redirects=False)`。
- `post()` 返回 `(response.status_code, response.text)`。
- `close()` 幂等。

### 1.4 `GDriveConfigSyncClient`

```python
GDriveConfigSyncClient(
    version: str,
    timeout: float = DEFAULT_SYNC_TIMEOUT,
    transport: Optional[SyncTransport] = None,
)
```

- `push(url, payload, trigger="manual") -> SyncResult`：
  1. `json.dumps(payload, ensure_ascii=False).encode("utf-8")`；
  2. Header：`Content-Type: application/json`、`User-Agent: MoviePilot-GDriveStorage/<version>`；
  3. 计时；调用 transport；
  4. `200 <= status < 300` → `ok=True`，message 可含状态码；
  5. 非 2xx → `ok=False`，message = `HTTP <status>：<截断响应>`；
  6. `Exception` → `ok=False`，`status_code=None`，message = `请求失败：<异常类型/文本>`；
  7. 超时（`TimeoutError` 或 httpx 超时）单独映射为可读「请求超时」。
- 不捕获 `KeyboardInterrupt`/`SystemExit`。
- `message` 只来自状态码与响应片段，不拼接 payload。

## 2. `__init__.py` 集成

### 2.1 常量与导入

- `from .sync_client import GDriveConfigSyncClient, SyncResult, build_payload, validate_sync_url`
- 新增 `DEFAULT_SYNC_INTERVAL_HOURS = 6`、`DEFAULT_SYNC_TIMEOUT = 30`。
- 无新增第三方导入。

### 2.2 类字段

```python
_sync_enabled = False
_sync_url = ""
_sync_token = ""
_sync_interval_hours = DEFAULT_SYNC_INTERVAL_HOURS
_sync_timeout = DEFAULT_SYNC_TIMEOUT
_sync_status: Dict[str, Any] = {}
_sync_client: Optional[GDriveConfigSyncClient] = None
_sync_lock: threading.Lock
```

`__init__()` 增加：`self._sync_lock = threading.Lock()`、`self._sync_status = {}`、`self._sync_client = None`。

### 2.3 生命周期

`init_plugin()` 在 `_config` 赋值后：

```python
self._sync_enabled = bool(cfg.get("sync_enabled"))
self._sync_url = cfg["sync_url"]
self._sync_token = cfg["sync_token"]
self._sync_interval_hours = cfg["sync_interval_hours"]
self._sync_timeout = cfg["sync_timeout"]
stored_sync = self.get_data("sync_status")
self._sync_status = stored_sync if isinstance(stored_sync, dict) else {}
self._sync_client = GDriveConfigSyncClient(
    version=self.plugin_version,
    timeout=float(self._sync_timeout),
)
if self._sync_url.startswith("http://"):
    logger.warning("【GDrive】配置同步使用 http://，OAuth 凭据将以明文传输")
```

`stop_service()` 在关闭 Google 客户端前后均可：关闭并置空 `_sync_client`，不 join 线程。

### 2.4 就绪判定

```python
def _sync_missing(self, require_enabled: bool) -> List[str]:
    """返回缺失项中文描述列表。"""
```

检查顺序：插件启用（仅 require_enabled）、client_id、client_secret、refresh_token、sync_url、sync_token、URL 合法。

```python
def _sync_ready(self, require_enabled: bool = False) -> bool:
    return not self._sync_missing(require_enabled)
```

手动触发时 `require_enabled=False`；但插件自身 `_enabled` 仍应校验（与其它 API 一致：未启用返回「插件未启用」）。

### 2.5 调度与执行

```python
def get_service(self):
    if not self._enabled or not self._sync_ready(require_enabled=True):
        return []
    return [{... "func": self.sync_from_scheduler, "kwargs": {"seconds": self._sync_interval_hours * 3600}}]

def sync_from_scheduler(self):
    if not self._enabled or not self._sync_ready(require_enabled=True):
        return
    if not self._sync_lock.acquire(blocking=False):
        logger.info("【GDrive】配置同步跳过：已有同步任务在运行")
        return
    threading.Thread(target=self._sync_worker, args=("scheduler",), daemon=True).start()

def _sync_worker(self, trigger: str):
    try:
        self._perform_sync(trigger)
    finally:
        self._sync_lock.release()

def _perform_sync(self, trigger: str) -> SyncResult:
    payload = build_payload(
        self._config.get("client_id"), ..., self._config.get("sync_token"),
    )
    client = self._sync_client
    if client is None:
        result = SyncResult(False, None, "同步客户端未初始化", 0, trigger, _now_text())
    else:
        result = client.push(self._sync_url, payload, trigger=trigger)
    self._sync_status = result.to_dict()
    try:
        self.save_data("sync_status", self._sync_status)
    except Exception:
        logger.debug("【GDrive】保存同步状态失败", exc_info=True)
    if result.ok:
        logger.info(f"【GDrive】配置同步成功：trigger={trigger} status={result.status_code} {result.elapsed_ms}ms")
    else:
        logger.warning(f"【GDrive】配置同步失败：trigger={trigger} status={result.status_code} {result.message}")
    return result
```

注意：日志不含 payload。

### 2.6 手动 API

```python
def api_sync_push(self) -> Dict[str, Any]:
    if not self._enabled:
        return {"success": False, "message": "插件未启用", "data": None}
    missing = self._sync_missing(require_enabled=False)
    if missing:
        return {"success": False, "message": "同步配置不完整：" + "、".join(missing), "data": None}
    if not self._sync_lock.acquire(blocking=False):
        return {"success": False, "message": "同步进行中，请稍后再试", "data": None}
    try:
        result = self._perform_sync("manual")
    finally:
        self._sync_lock.release()
    return {
        "success": result.ok,
        "message": ("同步成功" if result.ok else f"同步失败：{result.message}"),
        "data": result.to_dict(),
    }
```

所有 `return` 字面量必须恰好三键（契约测试会 AST 检查）。

### 2.7 `get_api()` 追加

```python
{
    "path": "/sync/push",
    "endpoint": self.api_sync_push,
    "methods": ["POST"],
    "auth": "bear",
    "summary": "立即向配置的接口推送 Google OAuth 凭据",
},
```

### 2.8 `get_page()` 追加

在状态告警后插入：

```python
contents.append(self._sync_alert())          # 构造同步状态文本，始终返回 VAlert
buttons.append(self._page_button("立即同步配置", "mdi-sync", "/sync/push"))
```

`_sync_alert()` 文案：

- 未配置：`未启用配置同步（在配置页填写接收端 URL 与 token）`
- 已配置：`配置同步：定时开/关；接收端 <netloc><path>；间隔 N 小时；上次 <at> <成功/失败> <状态码> <耗时>ms <错误>`
- `http://`：追加 `注意：当前使用 http://，凭据明文传输`
- 使用 `_alert("info"/"warning"/"error", text)`。

URL 展示用 `urlparse(url).netloc + path`，异常时显示 `(URL 无效)`。

### 2.9 表单

在现有「删除方式/分片/超时」`VRow` 后追加：

1. `VAlert` info：说明会发送 `client_secret` 与 `refresh_token`，只填可信接口。
2. `VRow`：
   - `VSwitch sync_enabled`（md=4）
   - `VTextField sync_url`（md=8，placeholder `https://example.com/api/gdrive/config`）
3. `VRow`：
   - `VTextField sync_token`（md=4，`type: password`）
   - `VTextField sync_interval_hours`（md=4，number，1–168）
   - `VTextField sync_timeout`（md=4，number，5–300）
4. 默认值字典追加 5 个键。

## 3. 测试设计

### 3.1 `tests/v3/gdrivestorage/test_sync_client.py`

纯模块测试，不加载插件：

- `FakeTransport` 记录 `(url, headers, content, timeout)`，返回预设状态/文本或抛异常。
- 用例：
  1. `build_payload` 恰好四键且值正确；
  2. `validate_sync_url`：https/http 通过；空、ftp、无 host、相对路径失败；
  3. 200/204 成功；
  4. 301/401/500 失败且状态码正确；
  5. 超时/连接异常 → `ok=False`、`status_code=None`；
  6. 长响应截断到 200 字符；
  7. Header 中 `Content-Type`、`User-Agent` 正确，`timeout` 透传；
  8. `SyncResult.to_dict()` 六键。

### 3.2 `test_plugin_contract.py` 扩展

- `REQUIRED_FORM_KEYS` 增加 5 键（否则 `test_get_form_defaults_are_complete` 失败）。
- 新增：
  - `get_api()` 含 `/sync/push` 且 `endpoint == api_sync_push`；
  - `get_service()`：构造最小配置实例（或 AST 检查源码包含 `GDriveStorageSync` 与 `_sync_ready`），断言开启且完整时返回 1 个服务、关闭时 `[]`；
  - `api_sync_push` 的三键 envelope 已被现有 AST 测试覆盖，无需新代码。

运行时实例测试可仿照 `test_get_page_does_not_raise_in_all_states`，设置 `_config` 后调用 `get_page()`/`get_service()`。

## 4. 验证

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/sync_client.py
pytest tests/v3/gdrivestorage -q
python -c "import json;print(json.load(open('package.v3.json',encoding='utf-8'))['GDriveStorage']['version'])"
```

## 5. 风险与对策

| 风险 | 对策 |
| --- | --- |
| 页面 API 非三键导致按钮无效 | 仅返回字面量三键；AST 测试兜底 |
| 定时线程阻塞宿主 | 线程化 + 超时 + 非阻塞锁 |
| 重载时旧线程仍写状态 | 结果写入幂等；`stop_service` 不等待，最多一次旧结果覆盖 |
| 凭据泄漏到日志 | 只记录状态码/耗时/截断响应；测试 grep 检查 |
| `http://` 明文 | 配置页与详情页警告 |
| 现有表单键集合测试失败 | 同步更新 `REQUIRED_FORM_KEYS` |
