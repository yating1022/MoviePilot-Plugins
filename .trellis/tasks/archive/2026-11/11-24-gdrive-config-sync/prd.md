# Google Drive 存储配置同步 — 需求文档

## Goal

在现有 `GDriveStorage` 插件中新增「配置同步」能力：按用户配置的间隔（或手动触发）向一个可配置的 HTTP 接口发送 `POST` 请求，把 Google OAuth 凭据（`client_id`、`client_secret`、`refresh_token`）与自定义授权 `token` 推送给接收端；同时输出一份接收端接口文档，供另一个项目实现该接口。

## 背景与已确认决策

| 主题 | 决策 |
| --- | --- |
| 推送目标 | 单个可配置 URL |
| 请求方式 | `POST` + `application/json` |
| 字段命名 | `snake_case` |
| 请求体字段 | `client_id`、`client_secret`、`refresh_token`、`token`（四个字段，无额外包装） |
| 自定义 token | 用户在插件配置中填写，放在请求体 `token` 字段，用于接收端鉴权 |
| 触发时机 | 仅「定时」+「手动」，不在保存配置或 OAuth 换令牌成功时自动推送 |
| 定时间隔 | 以「小时」为单位，默认 6，范围 1–168 |
| 成功判定 | HTTP `2xx` |
| 失败重试 | 第一版不自动重试；下一次定时或手动触发时再试 |
| 文档交付 | 接口文档写入仓库，供另一个项目照此实现接收端 |

## 任务拆分

| 任务 | 目录 | 职责 |
| --- | --- | --- |
| 父任务 | `11-24-gdrive-config-sync` | 需求源、请求契约、跨子任务验收、最终集成检查 |
| 子任务 A | `11-24-gdrive-config-sync-impl` | 插件端实现：配置表单、定时服务、手动 API、页面状态、日志、版本与测试 |
| 子任务 B | `11-24-gdrive-config-sync-api-doc` | 接收端接口文档：鉴权、请求/响应、状态码、幂等与示例 |

两个子任务都以父任务 `design.md` 中的请求契约为唯一事实源；文档子任务不依赖实现子任务的代码完成，但最终验收要在实现完成后回查字段一致性。

## Functional Requirements

### FR1 配置模型

- FR1.1 新增 `sync_enabled`（bool，默认 `false`），控制定时同步开关。
- FR1.2 新增 `sync_url`（str，默认空），接收端完整 URL。
- FR1.3 新增 `sync_token`（str，默认空），插件推送给接收端的自定义授权 token。
- FR1.4 新增 `sync_interval_hours`（int，默认 `6`，范围 1–168）。
- FR1.5 新增 `sync_timeout`（int，秒，默认 `30`，范围 5–300），单次推送超时。
- FR1.6 新配置键必须加入 `FORM_KEYS` 白名单，并在 `_normalize_config()` 中归一化。
- FR1.7 表单新增独立分组，说明「定时同步会把 OAuth 凭据发送到该接口」；`client_secret`、`refresh_token`、`sync_token` 使用密码型输入。
- FR1.8 配置保存后由宿主重载插件，新的定时服务按新配置生效。

### FR2 定时同步

- FR2.1 `get_service()` 仅在以下条件全部满足时注册 `interval` 服务：插件已启用、`sync_enabled=true`、`sync_url` 合法、`sync_token` 非空、`client_id`/`client_secret`/`refresh_token` 非空。
- FR2.2 服务 ID 固定为 `GDriveStorageSync`，名称可读，间隔为 `sync_interval_hours * 3600` 秒。
- FR2.3 调度入口必须立即返回，网络请求在后台线程执行，不阻塞宿主调度线程。
- FR2.4 同一时刻只允许一个同步任务；重复触发时跳过并记录原因，不排队堆积。
- FR2.5 插件停用/重载后不再启动新任务；进行中的线程允许在超时内自然结束，`stop_service()` 不等待网络请求。

### FR3 手动同步

- FR3.1 详情页新增「立即同步配置」按钮，调用 `plugin/GDriveStorage/sync/push`（`POST`）。
- FR3.2 手动触发不受 `sync_enabled` 限制，但要求 `sync_url`、`sync_token` 与三项 Google 凭据齐全。
- FR3.3 手动触发同步执行：等待 HTTP 结果（受 `sync_timeout` 限制）后返回结果，便于页面立即反馈。
- FR3.4 返回三键 envelope `{success, message, data}`；成功时宿主自动刷新页面，失败时 toast 显示可读原因。
- FR3.5 已有同步任务进行中时，手动触发返回失败「同步进行中，请稍后再试」。
- FR3.6 新增「查看同步状态」不单独建接口：状态随详情页渲染。

### FR4 请求契约（唯一事实源见 `design.md`）

- FR4.1 请求方法 `POST`，`Content-Type: application/json`。
- FR4.2 请求体恰好四个字段：`client_id`、`client_secret`、`refresh_token`、`token`。
- FR4.3 请求头带 `User-Agent: MoviePilot-GDriveStorage/<plugin_version>`。
- FR4.4 不跟随重定向，避免凭据被转发到非预期主机。
- FR4.5 接收端返回任意 `2xx` 即成功；`3xx`、`4xx`、`5xx`、网络错误、超时均视为失败。
- FR4.6 `http://` 允许使用（内网场景），但页面与日志给出明文传输警告；`https://` 为推荐方式。
- FR4.7 请求体不附加时间戳、版本等额外字段；需要扩展时通过新版本显式变更。

### FR5 状态与日志

- FR5.1 每次同步结束后持久化：时间、结果（success/failed/skipped）、HTTP 状态码、错误信息、耗时。
- FR5.2 详情页展示：定时开关、接收端主机、间隔、上次同步时间/结果/耗时/错误。
- FR5.3 日志与页面不得输出 `client_secret`、`refresh_token`、`sync_token` 的明文；错误信息最多保留 200 字符响应片段。
- FR5.4 同步结果通过 `save_data("sync_status", {...})` 持久化，重载后仍可查看。
- FR5.5 操作日志列表可记录一条 `sync` 动作摘要（成功/失败），但不记录请求体。

### FR6 接口文档

- FR6.1 输出 `docs/gdrive-config-sync-api.md`，包含：用途、鉴权方式、请求方法/路径、Header、请求体字段表、响应约定、状态码建议、幂等与安全建议、`curl` 与 Python 示例、版本兼容策略。
- FR6.2 文档中的字段名、成功判定、内容类型必须与实现完全一致。
- FR6.3 文档明确接收端只需返回 2xx，无需特定 JSON 结构；建议返回 `{"success": true}` 但非强制。

### FR7 发布与文档

- FR7.1 插件版本从 `1.1.3` 升至 `1.1.4`，同步更新 `package.v3.json` 的 `version` 与 `history`。
- FR7.2 `plugins.v3/gdrivestorage/README.md` 增加「配置同步」章节，说明字段、触发方式、安全提示。
- FR7.3 不新增第三方依赖；HTTP 使用 `httpx2`，缺失时回退 `httpx`。

## Non-functional Requirements

- NFR1 遵守 `.trellis/spec/plugin-v3/` 的插件契约；页面按钮 API 返回恰好三键 envelope。
- NFR2 不导入宿主内部模块；只使用 `app.sdk.*`、`app.schemas.*` 公开契约。
- NFR3 网络调用可注入传输层，测试无需真实外网。
- NFR4 同步逻辑为独立小模块或插件内纯函数，能在无宿主环境单测。
- NFR5 单次推送不显著增加详情页渲染耗时（页面只读缓存状态）。
- NFR6 失败不影响 Google Drive 存储、整理链和既有 OAuth 流程。

## Acceptance Criteria

- [ ] 表单出现 5 个同步配置项，默认值与范围符合 FR1；保存后配置可回读。
- [ ] 满足条件时 `get_service()` 返回 `GDriveStorageSync` interval 服务；任一条件不满足则不注册。
- [ ] 定时触发后接收端收到 `POST`，body 恰为四个字段且值正确。
- [ ] 手动按钮触发后，2xx 返回成功并刷新页面；非 2xx/超时返回失败并展示原因。
- [ ] 并发触发时只有一个请求发出，其余返回「同步进行中」。
- [ ] 上次同步状态在插件重载后仍显示。
- [ ] 日志、页面、异常中不出现 `client_secret`、`refresh_token`、`sync_token` 明文。
- [ ] `docs/gdrive-config-sync-api.md` 可直接交付给另一项目实现接收端，字段与实现一致。
- [ ] `python -m py_compile plugins.v3/gdrivestorage/__init__.py` 通过。
- [ ] `pytest tests/v3/gdrivestorage` 通过；新增同步相关测试覆盖成功、非 2xx、超时、缺配置、并发。
- [ ] `package.v3.json` 版本、history、README 与插件版本一致。

## Out of Scope

- 接收端服务本身的实现。
- 多接收端、多 URL、按条件路由。
- 从接收端拉取/回写配置（本需求是单向推送）。
- HMAC/签名、mTLS、凭据加密存储（仅使用自定义 token + HTTPS 建议）。
- 推送失败后的自动重试、退避与告警渠道。
- 除 Google OAuth 三项凭据外的其他配置同步。
