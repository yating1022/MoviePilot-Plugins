# GDriveStorage 配置同步：插件端实现 — 需求文档

父任务：`11-24-gdrive-config-sync`（契约与最终验收）
本文只描述实现工作；请求契约以父任务 `design.md` §2 为准。

## Goal

在 `plugins.v3/gdrivestorage/` 内实现定时与手动配置推送：新增配置项、独立推送模块、调度服务、手动 API、详情页状态与按钮、持久化结果，并补齐测试与发布信息。

## Functional Requirements

### FR1 推送模块 `sync_client.py`

- FR1.1 新增 `plugins.v3/gdrivestorage/sync_client.py`，不导入 `app.*`。
- FR1.2 `build_payload(client_id, client_secret, refresh_token, token) -> Dict[str, str]` 返回恰好四个键，顺序固定，值为字符串。
- FR1.3 `validate_sync_url(url) -> (bool, str)`：仅接受 `http`/`https` 且 host 非空；空 URL 返回 `(False, "未配置同步接口 URL")`。
- FR1.4 `SyncResult` 数据类包含 `ok`、`status_code`、`message`、`elapsed_ms`、`trigger`、`at`，并提供 `to_dict()`。
- FR1.5 `GDriveConfigSyncClient.push(url, payload)`：`POST` JSON，Header 含 `Content-Type` 与 `User-Agent: MoviePilot-GDriveStorage/<version>`，`follow_redirects=False`，超时由构造参数控制；任何异常/非 2xx 都返回 `ok=False` 的 `SyncResult`，不抛出。
- FR1.6 响应片段截断至 200 字符；`message` 不包含请求体内容。
- FR1.7 传输层可注入（`transport` 参数），默认延迟导入 `httpx2`，缺失回退 `httpx`。

### FR2 配置集成

- FR2.1 `FORM_KEYS` 追加 `sync_enabled`、`sync_url`、`sync_token`、`sync_interval_hours`、`sync_timeout`。
- FR2.2 `_normalize_config()` 按父设计 §3 归一化并 clamp。
- FR2.3 `get_form()` 默认值增加 5 个键；新增同步分组与安全说明。
- FR2.4 `sync_token` 输入为密码型；`sync_interval_hours`、`sync_timeout` 为数字输入并带范围。
- FR2.5 `init_plugin()` 读取同步配置到运行时字段，加载 `get_data("sync_status")`，构造同步客户端。
- FR2.6 `stop_service()` 关闭同步客户端；可重复调用。

### FR3 定时同步

- FR3.1 `get_service()` 在插件启用且 `_sync_ready(require_enabled=True)` 时返回单个 interval 服务，`id="GDriveStorageSync"`，`seconds=sync_interval_hours*3600`。
- FR3.2 条件不满足返回 `[]`，不抛异常。
- FR3.3 `sync_from_scheduler()` 立即返回；非阻塞拿锁成功后启动 daemon 线程，失败则记录跳过日志。
- FR3.4 `_sync_worker()` 在 `finally` 中释放锁。
- FR3.5 定时触发不等待 HTTP 完成，不阻塞宿主调度线程。

### FR4 手动触发

- FR4.1 `get_api()` 新增 `POST /sync/push` → `api_sync_push`，`auth: "bear"`。
- FR4.2 `api_sync_push()` 校验：插件启用、`_sync_ready(require_enabled=False)`、锁可用。
- FR4.3 校验失败返回 `{success: false, message: <可读原因>, data: None}`。
- FR4.4 成功/失败均返回恰好三键 envelope；`data` 含 `status_code`、`elapsed_ms`、`at`、`trigger`。
- FR4.5 同步执行，等待结果后返回；超时受 `sync_timeout` 限制。
- FR4.6 失败原因可读（缺哪个配置、HTTP 状态码、超时/连接错误）。

### FR5 详情页

- FR5.1 新增同步状态 `VAlert`：开关、URL 主机、间隔、上次时间/结果/耗时/错误；`http://` 追加明文警告；`style: "flex: 0 0 auto;"`。
- FR5.2 新增「立即同步配置」按钮，指向 `/sync/push`。
- FR5.3 页面只读缓存状态，不发网络请求。
- FR5.4 页面与日志不输出 `client_secret`、`refresh_token`、`sync_token` 明文。

### FR6 状态持久化与日志

- FR6.1 每次实际推送后 `save_data("sync_status", {...})`，包含 `at`、`trigger`、`ok`、`status_code`、`message`、`elapsed_ms`。
- FR6.2 成功 `logger.info`、失败 `logger.warning`，消息含 trigger、状态码、耗时，不含凭据。
- FR6.3 插件重载后状态仍可从 `get_data("sync_status")` 读回。
- FR6.4 跳过（并发/未就绪）不覆盖上次成功/失败状态。

### FR7 测试与发布

- FR7.1 新增 `tests/v3/gdrivestorage/test_sync_client.py`：payload、URL 校验、2xx、非 2xx、超时/连接错误、响应截断、User-Agent、不跟随重定向参数。
- FR7.2 更新 `tests/v3/gdrivestorage/test_plugin_contract.py`：`REQUIRED_FORM_KEYS` 加 5 键；新增 `get_service()` 条件与 `sync/push` 端点存在性检查。
- FR7.3 插件版本 → `1.1.4`；`package.v3.json` 的 `version`/`history` 同步。
- FR7.4 `README.md` 增加「配置同步」章节。

## Constraints

- 不新增第三方依赖。
- 不改动 Google Drive 存储/整理/OAuth 既有逻辑。
- 不把同步失败暴露为未捕获异常。
- 不通过网络请求初始化；`init_plugin` 只做本地准备。

## Acceptance Criteria

- [ ] 5 个新配置键出现在表单默认值与 `FORM_KEYS`，保存后可回读。
- [ ] `get_service()` 在配置完整且开关开启时返回 `GDriveStorageSync`；任一条件缺失返回 `[]`。
- [ ] 定时触发线程化，立即返回；同一时刻只发一个请求。
- [ ] 手动按钮成功时页面刷新，失败时 toast 可读原因。
- [ ] 接收端收到的 body 恰好四字段且值正确。
- [ ] 2xx 记为成功；3xx/4xx/5xx/超时记为失败并记录状态码/原因/耗时。
- [ ] 上次状态在重载后仍显示。
- [ ] 日志/页面无凭据明文。
- [ ] `pytest tests/v3/gdrivestorage -q` 全绿；`py_compile` 通过。
- [ ] 版本三处一致（类、package、history），README 已更新。

## Out of Scope

- 接收端实现与部署。
- 自动重试/告警/多接收端。
- 凭据加密存储、签名。
