# GDriveStorage 配置同步：接收端接口文档 — 需求文档

父任务：`11-24-gdrive-config-sync`。本任务只产出文档，不改插件代码。

## Goal

输出 `docs/gdrive-config-sync-api.md`，让另一个项目可以照文档实现接收端接口，正确接收 `MoviePilot GDriveStorage` 插件推送的 Google OAuth 凭据与自定义 token。

## Functional Requirements

### FR1 文档位置与受众

- FR1.1 文档路径固定为仓库根 `docs/gdrive-config-sync-api.md`。
- FR1.2 面向「接收端开发者」，不假设其了解 MoviePilot 插件内部；必要处可提插件行为，但不写实现细节。
- FR1.3 全中文，示例代码可英文。

### FR2 契约内容（必须与父 `design.md` §2 完全一致）

- FR2.1 方法与地址：`POST {sync_url}`，`Content-Type: application/json`。
- FR2.2 Header：`Content-Type`、`User-Agent: MoviePilot-GDriveStorage/<version>`；无自定义鉴权 Header。
- FR2.3 请求体恰好四字段：`client_id`、`client_secret`、`refresh_token`、`token`，均为 string；给出字段表与 JSON 示例。
- FR2.4 成功判定：任意 `2xx`；明确「不要求特定 JSON 结构」，并给出建议响应。
- FR2.5 失败判定：`3xx`/`4xx`/`5xx`、连接错误、超时；插件不跟随重定向，`3xx` 直接失败。
- FR2.6 超时：默认 30 秒，可配置 5–300 秒；接收端应尽快响应。
- FR2.7 不自动重试；重复请求是常态。

### FR3 鉴权与安全

- FR3.1 说明 `token` 是用户自定义共享密钥，接收端必须校验。
- FR3.2 建议常量时间比较；校验失败返回 `401`/`403`。
- FR3.3 推荐 HTTPS；说明插件允许 `http://` 但会警告明文传输。
- FR3.4 接收端不得在日志中记录 `client_secret`、`refresh_token`、`token`；如需日志用掩码。
- FR3.5 不建议把凭据放 URL 或额外 Header（当前契约不支持）。

### FR4 幂等与存储

- FR4.1 接收端应按 `client_id`（或 `token` 所属租户）upsert；重复推送相同值无副作用。
- FR4.2 说明一次请求即全量快照语义：收到后以本次四字段为准覆盖旧值。
- FR4.3 建议记录 `updated_at` 与来源 IP（可选）。
- FR4.4 无请求 ID、无签名；如需审计，用接收时间 + `client_id`。

### FR5 错误与状态码建议

- FR5.1 表格列出建议：`200/204` 成功；`400` 字段缺失或非法；`401/403` token 错误；`409` 可选（并发写入冲突）；`429` 可选（限流）；`5xx` 接收端故障。
- FR5.2 说明插件只区分 2xx / 非 2xx；非 2xx 时插件记录状态码与最多 200 字符响应片段。
- FR5.3 建议错误响应体 `{"success": false, "message": "..."}`，但非强制。

### FR6 示例

- FR6.1 `curl` 请求示例（含 Header 与四字段 body）。
- FR6.2 至少一个服务端示例骨架（Python FastAPI 或通用伪代码），展示读取 body、校验 token、upsert、返回 2xx。
- FR6.3 示例中的密钥用占位符，不能是真实凭据。

### FR7 联调与兼容

- FR7.1 提供联调 checklist：URL 可访问、token 一致、返回 2xx、插件页面显示成功、重载后状态保留。
- FR7.2 说明契约变更策略：新增字段时接收端应忽略未知字段；删除/改名视为破坏性变更。
- FR7.3 明确当前版本不含时间戳/版本字段；不要依赖额外字段。

## Acceptance Criteria

- [ ] `docs/gdrive-config-sync-api.md` 存在，结构完整（概述、请求、鉴权、响应、幂等、安全、错误、示例、联调）。
- [ ] 字段名、Header、成功判定、超时、重定向语义与父 `design.md` §2 逐字一致。
- [ ] 文档明确 body 恰好四字段，无额外包装。
- [ ] 文档明确「任意 2xx 即成功」，并解释插件不解析响应体。
- [ ] 示例使用占位符，不含真实凭据。
- [ ] 接收端开发者可只读该文档完成实现与联调。

## Out of Scope

- 接收端代码实现、部署与运维。
- 反向拉取/下发配置。
- 签名、mTLS、OAuth 客户端凭据以外的鉴权方案。
- 多租户路由设计。
