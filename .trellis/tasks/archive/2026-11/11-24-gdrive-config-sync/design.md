# Google Drive 存储配置同步 — 技术设计

## 1. 目标与边界

在 `GDriveStorage` 插件内增加单向「配置推送」：把本插件已保存的 Google OAuth 凭据与自定义 token，按定时或手动方式 `POST` 到用户指定的接口。

- 只改 `plugins.v3/gdrivestorage/`、`tests/v3/gdrivestorage/`、`package.v3.json`、根 `docs/`。
- 不改动 Google Drive 存储、整理链、OAuth 回调的既有行为。
- 不实现接收端；只定义并输出契约。

## 2. 请求契约（唯一事实源）

### 2.1 请求

```http
POST {sync_url}
Content-Type: application/json
User-Agent: MoviePilot-GDriveStorage/<plugin_version>
```

- 不跟随重定向（`follow_redirects=False`）：`3xx` 视为失败，防止凭据被转发。
- 不带查询参数、不带自定义 Header 鉴权；鉴权信息只在 body。
- 超时由 `sync_timeout` 控制（连接/读取总超时）。

### 2.2 请求体

恰好四个字段，JSON 对象，无外层包装：

```json
{
  "client_id": "xxx.apps.googleusercontent.com",
  "client_secret": "GOCSPX-xxx",
  "refresh_token": "1//xxx",
  "token": "user-defined-shared-secret"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `client_id` | string | 是 | Google OAuth 客户端 ID |
| `client_secret` | string | 是 | Google OAuth 客户端密钥 |
| `refresh_token` | string | 是 | Google OAuth 刷新令牌 |
| `token` | string | 是 | 用户自定义授权 token，接收端据此校验来源 |

值来自插件配置的 `client_id`、`client_secret`、`refresh_token`、`sync_token`；发送前不做编码转换。字段为空时不允许发起推送（前置条件不满足）。

### 2.3 响应与成功判定

- 任意 `2xx`：成功。接收端无需返回特定 JSON。
- 建议（非强制）：`200 {"success": true, "message": "ok"}`。
- `3xx`/`4xx`/`5xx`、连接错误、超时、非法 URL：失败。
- 失败时插件保留：HTTP 状态码（网络错误为 `null`）、错误信息（响应片段最多 200 字符）、耗时。
- 不自动重试；等下一次定时或手动触发。

### 2.4 幂等与安全（写入接收端文档）

- 接收端应以 `client_id`（或 `token` 对应的租户）为键做 upsert；重复推送相同值必须无副作用。
- 必须使用 HTTPS；插件允许 `http://` 但在页面/日志警告明文传输。
- 接收端应使用常量时间比较校验 `token`，且不得在日志中记录 `client_secret`/`refresh_token`/`token`。
- 契约变更采用新增字段 + 版本说明；第一版不发送版本字段，接收端应忽略未知字段。

## 3. 配置模型

`get_form()` 默认值新增：

| 键 | 类型 | 默认 | 范围/约束 | 说明 |
| --- | --- | --- | --- | --- |
| `sync_enabled` | bool | `false` | - | 启用定时同步 |
| `sync_url` | str | `""` | `http(s)://host/...` | 接收端完整 URL |
| `sync_token` | str | `""` | 非空才允许推送 | 自定义授权 token |
| `sync_interval_hours` | int | `6` | 1–168 | 定时间隔 |
| `sync_timeout` | int | `30` | 5–300 | 单次推送超时（秒） |

`FORM_KEYS` 必须加入上述 5 个键，否则系统存储保存时白名单会丢弃配置。

配置归一化：

```python
"sync_enabled": bool(cfg.get("sync_enabled")),
"sync_url": str(cfg.get("sync_url") or "").strip(),
"sync_token": str(cfg.get("sync_token") or "").strip(),
"sync_interval_hours": _clamp_int(cfg.get("sync_interval_hours"), 1, 168, 6),
"sync_timeout": _clamp_int(cfg.get("sync_timeout"), 5, 300, 30),
```

URL 合法性单独校验（`urllib.parse.urlparse`）：scheme 为 `http`/`https` 且 host 非空。非法 URL 不注册定时服务，手动触发返回失败。

## 4. 模块设计

```text
plugins.v3/gdrivestorage/
├── __init__.py        # 配置、调度、手动 API、状态展示（集成点）
└── sync_client.py     # 纯推送逻辑：payload、URL 校验、HTTP、结果对象

docs/
└── gdrive-config-sync-api.md   # 接收端接口文档

tests/v3/gdrivestorage/
├── test_sync_client.py         # 纯逻辑 + FakeTransport
└── test_plugin_contract.py     # 扩展表单键、调度与 envelope 静态检查
```

`sync_client.py` 不导入 `app.*`，可在无宿主环境直接测试。

### 4.1 `sync_client.py`

```python
@dataclass(frozen=True)
class SyncResult:
    ok: bool
    status_code: Optional[int]
    message: str
    elapsed_ms: int
    trigger: str            # "scheduler" | "manual"
    at: str                 # "%Y-%m-%d %H:%M:%S"

def build_payload(client_id, client_secret, refresh_token, token) -> Dict[str, str]

def validate_sync_url(url: str) -> Tuple[bool, str]

class SyncTransport(Protocol):
    def post(self, url: str, headers: Dict[str, str], content: bytes, timeout: float) -> Tuple[int, str]: ...

class HttpxSyncTransport:      # 延迟导入 httpx2，回退 httpx
    def __init__(self)
    def post(...)
    def close(self)

class GDriveConfigSyncClient:
    def __init__(self, token: str, timeout: float, transport: Optional[SyncTransport] = None)
    def push(self, url: str, payload: Dict[str, str]) -> SyncResult
    def close(self)
```

- `push()` 只负责一次 HTTP 调用并把所有异常收敛为 `SyncResult`，不抛出。
- 传输层返回 `(status_code, response_text)`；统一截断响应片段到 200 字符。
- 不记录请求体；`message` 中不得包含凭据。
- `httpx2.Client(timeout=timeout, follow_redirects=False)`；每次 `push` 用同一 client，插件关闭时 `close()`。

### 4.2 插件集成点（`__init__.py`）

运行时字段：

```python
_sync_enabled: bool
_sync_url: str
_sync_token: str
_sync_interval_hours: int
_sync_timeout: int
_sync_lock: threading.Lock          # 同一时刻只允许一个推送
_sync_status: Dict[str, Any]        # 持久化展示状态
_sync_client: Optional[GDriveConfigSyncClient]
```

生命周期：

- `init_plugin()`：归一化配置、读取 `get_data("sync_status")`、构造同步客户端；URL 为 `http://` 时 `logger.warning`。
- `stop_service()`：关闭并置空 `_sync_client`；不 join 网络线程，避免卡住宿主重载。
- `__init__()`：初始化锁和状态字典。

就绪判定 `_sync_ready(require_enabled: bool)`：

```text
client_id/client_secret/refresh_token 非空
且 sync_url 合法
且 sync_token 非空
且 (require_enabled 为 False 或 sync_enabled 为 True)
```

定时：

```python
def get_service(self):
    if not self._enabled or not self._sync_ready(require_enabled=True):
        return []
    return [{
        "id": "GDriveStorageSync",
        "name": "Google Drive 存储（配置同步）",
        "trigger": "interval",
        "func": self.sync_from_scheduler,
        "kwargs": {"seconds": self._sync_interval_hours * 3600},
    }]

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
```

手动 API：

```python
def api_sync_push(self) -> Dict[str, Any]:
    # 插件未启用 / 配置不完整 / 已有任务 → 失败 envelope
    # 否则同步执行 _perform_sync("manual")，返回三键 envelope
```

`_perform_sync(trigger)`：

1. 构造 payload；
2. 调用 `GDriveConfigSyncClient.push()`；
3. 更新 `_sync_status`（`at/trigger/ok/status_code/message/elapsed_ms`）；
4. `save_data("sync_status", self._sync_status)`；
5. `logger.info` 成功 / `logger.warning` 失败（不含凭据）；
6. 返回 `SyncResult`。

并发语义：调度器在启动线程前拿锁，手动路径同步拿锁；拿不到即返回「同步进行中」，不排队。

### 4.3 页面

`get_page()` 新增：

- 一个带 `style: "flex: 0 0 auto;"` 的 `VAlert`：同步开关、接收端主机、间隔、上次结果/耗时/错误；`http://` 时追加明文警告。
- 一个 `VBtn`「立即同步配置」，事件指向 `plugin/GDriveStorage/sync/push`，`method: post`。
- URL 只显示 `urlparse(url).netloc + path`，不显示凭据（URL 中不应含凭据）。

`get_api()` 新增：

```python
{
    "path": "/sync/push",
    "endpoint": self.api_sync_push,
    "methods": ["POST"],
    "auth": "bear",
    "summary": "立即向配置的接口推送 Google OAuth 凭据",
}
```

所有返回字典必须是恰好 `{success, message, data}` 三键，否则宿主不刷新页面。

### 4.4 配置表单

在超时/删除方式分组后新增同步分组：

- `VSwitch model=sync_enabled`
- `VTextField model=sync_url`（placeholder `https://example.com/api/gdrive/config`）
- `VTextField model=sync_token`，`type: password`
- `VTextField model=sync_interval_hours`，`type: number`，`min: 1`，`max: 168`
- `VTextField model=sync_timeout`，`type: number`，`min: 5`，`max: 300`
- `VAlert`（info）：说明会发送 `client_secret` 与 `refresh_token`，仅应填入可信接口。

## 5. 状态与错误矩阵

| 场景 | 定时行为 | 手动行为 | 记录 |
| --- | --- | --- | --- |
| 插件未启用 | 不注册 | 失败「插件未启用」 | 不写状态 |
| 凭据/URL/token 缺失或 URL 非法 | 不注册 | 失败并指出缺失项 | 不写状态 |
| 已有同步进行中 | 跳过并 info 日志 | 失败「同步进行中」 | 不写状态 |
| `2xx` | 继续下次 | 成功 envelope | `ok=true`，状态码、耗时 |
| 非 `2xx` | 继续下次 | 失败 envelope | `ok=false`，状态码、200 字符片段 |
| 超时/连接错误 | 继续下次 | 失败「请求失败：...」 | `ok=false`，`status_code=null` |

## 6. 安全

- 表单密码型输入；日志/页面永不输出 `client_secret`、`refresh_token`、`sync_token` 明文。
- 错误响应片段可能回显请求内容，截断后仅保存在插件数据与页面；若担心回显，接收端不应回显请求体。
- 不跟随重定向；不把凭据放 URL/Header。
- `http://` 允许但显式警告；文档推荐 HTTPS。
- 建议接收端按 `token` 校验并常量时间比较。

## 7. 兼容与回滚

- 新配置键全部有默认值；旧配置加载后同步功能默认关闭。
- `FORM_KEYS` 追加键不会改变既有键顺序语义。
- `get_service()` 返回空列表时与当前行为一致。
- 回滚：移除 `sync_client.py`、新增配置键、`get_service` 分支、`get_api` 条目、页面块，并还原版本号即可；不涉及数据迁移（仅 `sync_status` 数据键）。

## 8. 验证策略

- `python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/sync_client.py`
- `pytest tests/v3/gdrivestorage -q`
- 新增纯逻辑测试：payload 四字段、URL 校验、`2xx` 成功、`500` 失败、超时/连接异常、响应截断。
- 契约测试：`REQUIRED_FORM_KEYS` 更新；`get_service()` 条件分支；`api_sync_push` 三键 envelope；`sync/*` 端点声明。
- `json.load(package.v3.json)` 与版本/history 一致性。
- 有宿主环境时手工验证：配置 → 保存 → 详情页按钮 → 接收端日志；关闭定时开关后 `get_service()` 为空。
