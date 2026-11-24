# Scenario: 插件主动推送敏感配置到外部接口

> 来源：`GDriveStorage` v1.1.4 配置同步（2026-11-24）。
> 适用于任何需要把插件内凭据/配置主动 `POST` 给用户指定服务的功能。

## 1. Scope / Trigger

触发条件：

- 插件新增「向外部 URL 推送凭据/配置」的能力；
- 需要定时 + 手动两种触发方式；
- 请求体包含 `client_secret`、`refresh_token`、token 等敏感值；
- 页面按钮调用插件自有 API。

不适用：普通只读第三方 API 调用、无敏感数据的 webhook 通知。

## 2. Signatures

### 2.1 配置键

| 键 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `sync_enabled` | bool | `false` | 定时开关 |
| `sync_url` | str | `""` | `http(s)://host/path` |
| `sync_token` | str | `""` | 非空才允许推送；表单 `type: password` |
| `sync_interval_hours` | int | `6` | 1–168 |
| `sync_timeout` | int | `30` | 5–300 秒 |

五个键必须同时加入：

1. `FORM_KEYS`（系统存储保存白名单）；
2. `_normalize_config()`（clamp + strip）；
3. `get_form()` 默认值与 UI；
4. 契约测试的 `REQUIRED_FORM_KEYS`。

漏掉 `FORM_KEYS` 会导致配置保存后被静默丢弃。

### 2.2 调度与 API

```python
def get_service(self):  # 条件不满足返回 []
    return [{
        "id": "<Plugin>Sync",
        "name": "<可读名称>",
        "trigger": "interval",
        "func": self.sync_from_scheduler,
        "kwargs": {"seconds": self._sync_interval_hours * 3600},
    }]

def sync_from_scheduler(self) -> None:
    """非阻塞拿锁 → daemon 线程 → 立即返回。"""

def api_sync_push(self) -> Dict[str, Any]:
    """手动同步执行；返回恰好三键 envelope。"""
```

`get_api()` 条目：

```python
{"path": "/sync/push", "endpoint": self.api_sync_push,
 "methods": ["POST"], "auth": "bear", "summary": "..."}
```

### 2.3 推送客户端

```python
class GDriveConfigSyncClient:
    def __init__(self, version: str, timeout: float = 30,
                 transport: Optional[SyncTransport] = None): ...
    def push(self, url: str, payload: Dict[str, str],
             trigger: str = "manual") -> SyncResult: ...
```

`SyncResult`：`ok`、`status_code`、`message`、`elapsed_ms`、`trigger`、`at`，提供 `to_dict()`。

## 3. Contracts

### 3.1 请求

```http
POST {sync_url}
Content-Type: application/json
User-Agent: <Plugin>/<plugin_version>
```

- **不跟随重定向**（`follow_redirects=False`）；`3xx` 视为失败，防止凭据被转发。
- 无查询参数、无自定义鉴权 Header。
- 请求体恰好四字段，无时间戳/版本/请求 ID：

```json
{"client_id": "...", "client_secret": "...", "refresh_token": "...", "token": "..."}
```

### 3.2 响应判定

| 条件 | 结果 |
| --- | --- |
| `200 <= status < 300` | 成功；不解析响应体 |
| `3xx` | 失败（不跟随） |
| `4xx` / `5xx` | 失败 |
| 超时 / 连接错误 | 失败，`status_code=None` |
| 响应片段 | 最多 200 字符，写入状态用于排查 |

不自动重试；重复请求由接收端按 `client_id` upsert 保证幂等。

### 3.3 并发与生命周期

- 一个 `threading.Lock` 同时保护定时与手动路径；`acquire(blocking=False)`。
- 定时路径：拿锁 → `Thread(daemon=True).start()` → 立即返回；**`start()` 失败必须释放锁**。
- 手动路径：同步执行，`finally` 释放锁；拿不到锁返回「同步进行中」。
- `stop_service()` 关闭客户端但**不 join** 网络线程；在途请求记为失败，不阻塞宿主重载。
- 锁在 `__init__` 创建，重载不替换，避免锁状态丢失。

### 3.4 状态持久化

```python
self._sync_status = result.to_dict()
self.save_data("sync_status", self._sync_status)
```

只持久化结果元数据，不持久化请求体。

## 4. Validation & Error Matrix

| 条件 | 定时 | 手动 | 记录 |
| --- | --- | --- | --- |
| 插件未启用 | 不注册 | 失败 envelope | 不写状态 |
| 任一凭据/URL/token 缺失或 URL 非法 | 不注册 | 失败并列出缺失项 | 不写状态 |
| 已有任务运行 | 跳过 + info 日志 | 失败「同步进行中」 | 不写状态 |
| 2xx | 继续下次 | 成功 envelope | `ok=true` |
| 非 2xx | 继续下次 | 失败 envelope | 状态码 + 片段 |
| 超时/连接错误 | 继续下次 | 失败 envelope | `status_code=null` |
| 线程启动失败 | 释放锁 + warning | 不受影响 | 不写状态 |

## 5. Good / Base / Bad Cases

- **Good**：定时与手动共享非阻塞锁；手动等待结果；定时立即返回；状态持久化；页面只显示 URL 的 `netloc+path`。
- **Base**：配置未填全时 `get_service()` 返回 `[]`，手动 API 给出「同步配置不完整：缺少 …」。
- **Bad**：调度函数里直接 `client.push()`，阻塞宿主调度线程数十秒；`Thread.start()` 失败后不释放锁；日志打印 payload。

## 6. Tests Required

1. payload 恰好四键且顺序固定（assert `list(payload) == [...]`）。
2. URL 校验：https/http 通过；空、ftp、无 host、相对路径失败。
3. `2xx` 成功；`3xx/4xx/5xx` 失败且状态码正确。
4. 超时与连接异常 → `ok=False`、`status_code=None`。
5. 响应片段截断到 200 字符。
6. Header 含 `Content-Type`、`User-Agent`，`timeout` 透传。
7. 真实传输层 `follow_redirects is False`；`close()` 幂等。
8. 并发：手动忙锁返回失败；定时忙锁跳过。
9. worker 抛异常仍释放锁；线程启动失败仍释放锁。
10. `api_sync_push` 所有 return 字面量恰好 `{success, message, data}`。
11. 页面/日志无凭据明文（grep `logger.*client_secret` 等）。
12. 配置五键同时存在于 `FORM_KEYS`、归一化、表单默认值、契约测试。

## 7. Wrong vs Correct

### Wrong

```python
# 1) 跟随重定向：3xx 会把凭据转发到别的主机
client = httpx.Client(timeout=timeout, follow_redirects=True)

# 2) 调度线程里同步请求，阻塞宿主
def sync_from_scheduler(self):
    self._perform_sync("scheduler")

# 3) 打印请求体
logger.info(f"push payload={payload}")
```

### Correct

```python
client = httpx.Client(timeout=timeout, follow_redirects=False)

def sync_from_scheduler(self):
    if not self._lock.acquire(blocking=False):
        logger.info("...跳过：已有任务")
        return
    try:
        threading.Thread(target=self._sync_worker, args=("scheduler",),
                         daemon=True).start()
    except Exception:
        self._lock.release()
        logger.warning("...启动同步线程失败", exc_info=True)

logger.info(f"push ok={result.ok} status={result.status_code} {result.elapsed_ms}ms")
```

## 8. Design Decisions

1. **token 放 body 而非 Header**：接收端实现简单，且与配置值直接比对；代价是无法复用标准 `Authorization` 中间件。
2. **不做签名/HMAC（v1）**：用 HTTPS + 自定义 token 控制复杂度；需要更强鉴权时在网关叠加。
3. **不自动重试**：避免对用户服务造成重试风暴；下一次定时或手动自然补试。
4. **保存配置不自动推送**：行为可预测，避免调试配置时反复外发凭据。
5. **失败响应片段会进入页面**：便于排查，但要求接收端不回显请求体；这是文档中必须强调的接收端责任。
