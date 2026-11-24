# Google Drive 配置同步 — 接收端接口文档

> 提供方：MoviePilot `GDriveStorage` 插件（v1.1.4+）
> 方向：插件 → 你的服务，单向推送
> 文档状态：与插件 `design.md` 契约一致，最终实现完成后会回查字段

## 1. 这个接口做什么

`GDriveStorage` 插件会把用户配置好的 Google OAuth 凭据，通过一次 HTTP `POST` 推送到你指定的接口。你的服务收到后保存（或更新）这些凭据，即可用它们在别处访问同一个 Google Drive。

触发方式只有两种：

1. **定时**：插件按用户配置的间隔（小时）自动推送，默认 6 小时。
2. **手动**：用户在插件详情页点「立即同步配置」。

保存插件配置、完成 Google 授权时**不会**自动推送。因此你的接口必须能安全地处理重复请求。

```text
MoviePilot GDriveStorage
        │  POST application/json
        │  {client_id, client_secret, refresh_token, token}
        ▼
你的接收端  ──► 校验 token ──► 按 client_id upsert ──► 返回 2xx
```

## 2. 请求

```http
POST {你在插件里配置的同步 URL}
Content-Type: application/json
User-Agent: MoviePilot-GDriveStorage/<插件版本>
```

示例：

```http
POST https://your-service.example.com/api/gdrive/config HTTP/1.1
Content-Type: application/json
User-Agent: MoviePilot-GDriveStorage/1.1.4

{"client_id":"...","client_secret":"...","refresh_token":"...","token":"..."}
```

### 2.1 关键行为

| 项目 | 约定 |
| --- | --- |
| 方法 | 仅 `POST` |
| 查询参数 | 无 |
| 自定义 Header | 无；鉴权 token 在 body 中 |
| 是否跟随重定向 | **否**。接收端返回 `3xx` 会被插件判定为失败 |
| 超时 | 默认 30 秒，插件侧可配置 5–300 秒 |
| 重试 | 不自动重试；失败后等下一次定时或用户手动触发 |
| 请求体编码 | UTF-8 JSON |

> 建议你的接口在 1 秒内返回；不要做耗时同步任务。确需异步处理时，先落库再立即返回 `200`。

## 3. 鉴权：body 中的 `token`

插件配置里有一个「同步 token」字段，用户自定义，插件会把它原样放进请求体 `token` 字段。

接收端必须：

1. 校验 `token` 与服务端保存的期望值一致；
2. 使用**常量时间比较**（如 Python `hmac.compare_digest`、Java `MessageDigest.isEqual`），避免计时攻击；
3. 校验失败返回 `401` 或 `403`，且不要在响应中回显收到的 token。

`token` 是共享密钥，不是 OAuth 令牌；它只用于证明「这个请求来自被授权的 MoviePilot 实例」。

## 4. 请求体

请求体是一个 JSON 对象，**恰好四个字段**，没有外层包装、没有额外元数据：

| 字段 | 类型 | 必填 | 含义 |
| --- | --- | --- | --- |
| `client_id` | string | 是 | Google OAuth 2.0 客户端 ID |
| `client_secret` | string | 是 | Google OAuth 2.0 客户端密钥 |
| `refresh_token` | string | 是 | Google OAuth 刷新令牌，用于换取 access token |
| `token` | string | 是 | 用户自定义授权 token，接收端据此校验来源 |

示例（值均为占位符）：

```json
{
  "client_id": "1234567890-abcdef.apps.googleusercontent.com",
  "client_secret": "GOCSPX-xxxxxxxxxxxxxxxx",
  "refresh_token": "1//0gxxxxxxxxxxxxxxxxxxxxxxxx",
  "token": "my-shared-secret-2026"
}
```

字段语义补充：

- `client_id` / `client_secret` 来自 Google Cloud Console 的 OAuth Web 客户端。
- `refresh_token` 由用户完成 Google 授权后获得，长期有效（除非被撤销）。
- 四个值均按插件配置原样发送，接收端不需要也不应该做格式改写。
- 插件在任一字段为空时不会发起请求；因此你不需要处理空字符串的「合法」请求，可以按 `400` 处理。

**不要依赖其他字段**：当前版本不发送时间戳、版本号、请求 ID、签名。未来的新增字段会以兼容方式加入，接收端应忽略未知字段；删除或改名属于破坏性变更。

## 5. 响应与成功判定

**判定规则：HTTP 状态码为任意 `2xx` 即成功。**

- 插件**不解析**响应体，不要求 `{"code":0}`、`{"success":true}` 之类的结构。
- 响应体可以为空（`204 No Content` 也完全合法）。
- 建议返回 `200` + JSON，便于人排查：

```json
{ "success": true, "message": "config updated" }
```

- 非 `2xx`、连接失败、超时，均判定为失败。
- 失败时插件会记录：HTTP 状态码、最多 200 字符的响应片段、耗时，并显示在插件详情页。

### 5.1 建议状态码

| 状态码 | 场景 | 插件表现 |
| --- | --- | --- |
| `200` / `204` | 保存/更新成功 | 成功 |
| `400` / `422` | 字段缺失或格式非法 | 失败，记录片段 |
| `401` / `403` | `token` 错误 | 失败，记录片段 |
| `409` | 可选：并发写入冲突 | 失败（插件不重试） |
| `429` | 可选：限流 | 失败（插件不重试） |
| `5xx` | 接收端故障 | 失败，记录片段 |
| `3xx` | 任何重定向 | 失败（插件不跟随） |

> 由于插件不自动重试，`429` 或 `5xx` 建议仅在确实无法处理时返回；否则下一次定时可能是数小时后。

## 6. 幂等与存储语义

- 一次请求代表**全量快照**：以本次收到的四个字段为准，覆盖该 `client_id` 对应的旧值。
- 建议以 `client_id` 作为业务主键做 upsert；如果同一 `client_id` 可能来自多个 MoviePilot 实例，可再结合 `token` 或来源标识区分租户。
- 相同内容重复推送必须无副作用（可以比较后跳过写入）。
- 建议记录 `updated_at`；来源 IP 可选记录，便于审计。
- 插件没有请求 ID，无法用它去重；请依赖幂等写入，而不是「只处理一次」。

## 7. 安全要求

1. **必须使用 HTTPS**。插件允许配置 `http://`（内网场景），但会在页面和日志中警告凭据明文传输；生产环境不要使用明文 HTTP。
2. **不要记录凭据明文**。`client_secret`、`refresh_token`、`token` 写入日志前必须掩码；建议只记录 `client_id` 尾号与时间。
3. **不要在错误响应里回显请求体**。插件会把非 2xx 响应的前 200 字符保存并显示在页面上，回显会导致密钥泄漏到插件界面。
4. `token` 使用常量时间比较；不要支持空 token。
5. 建议限制请求体大小（例如 8 KB）和只接受 `POST`。
6. 如需额外防护，可在网关层按来源 IP 白名单或限流，但不要改变本文的 body 契约。

## 8. 接收端最小实现清单

- [ ] 暴露一个 `POST` 端点，解析 `application/json`。
- [ ] 读取并校验 `token`（常量时间比较），失败返回 `401`/`403`。
- [ ] 校验四个字段存在且非空，缺失返回 `400`/`422`。
- [ ] 按 `client_id` upsert 三个 Google 凭据字段。
- [ ] 成功返回 `200`（可带 JSON）或 `204`。
- [ ] 日志中不出现 `client_secret` / `refresh_token` / `token` 明文。
- [ ] 错误响应不回显请求体。
- [ ] 处理重复请求无副作用。
- [ ] 响应时间远小于插件超时（默认 30 秒）。

## 9. 示例

### 9.1 curl

```bash
curl -X POST 'https://your-service.example.com/api/gdrive/config' \
  -H 'Content-Type: application/json' \
  -H 'User-Agent: MoviePilot-GDriveStorage/1.1.4' \
  --data-binary '{
    "client_id": "1234567890-abcdef.apps.googleusercontent.com",
    "client_secret": "GOCSPX-xxxxxxxxxxxxxxxx",
    "refresh_token": "1//0gxxxxxxxxxxxxxxxxxxxxxxxx",
    "token": "my-shared-secret-2026"
  }'
```

### 9.2 Python（FastAPI）骨架

```python
from hmac import compare_digest

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()
EXPECTED_TOKEN = "my-shared-secret-2026"  # 从环境变量/密钥管理读取，不要硬编码


class GDriveConfig(BaseModel):
    client_id: str
    client_secret: str
    refresh_token: str
    token: str


@app.post("/api/gdrive/config")
def receive_config(cfg: GDriveConfig) -> dict:
    # token 校验：常量时间比较
    if not compare_digest(cfg.token, EXPECTED_TOKEN):
        raise HTTPException(status_code=401, detail="invalid token")

    # 全量覆盖：以 client_id 为键 upsert
    # save_or_update(cfg.client_id, cfg.client_secret, cfg.refresh_token)
    # 日志只记录非敏感字段
    # logger.info("gdrive config updated: client_id=%s", mask(cfg.client_id))

    return {"success": True, "message": "config updated"}
```

Pydantic 会在字段缺失时自动返回 `422`，符合第 5 节的状态码建议。

### 9.3 建议的错误响应

```json
{ "success": false, "message": "invalid token" }
```

同样，**不要**把收到的 body 放进 `message`。

## 10. 联调步骤

1. 在接收端部署接口，准备一个足够长的随机 `token`。
2. 在 MoviePilot 的 `GDriveStorage` 配置页填写：
   - 同步接口 URL：你的 `POST` 地址
   - 同步 token：与接收端一致的随机值
   - 定时间隔（小时）
   - 超时（秒）
3. 保存配置后，打开插件详情页，点击「立即同步配置」。
4. 预期：接收端收到一次 `POST`，body 为四字段；返回 `2xx`；插件页面显示「同步成功」与状态码/耗时。
5. 把接收端的 token 改错，再次点击，预期插件显示失败并带 `401`/`403`。
6. 打开定时开关，等待一个周期，确认接收端收到下一次推送。
7. 重载插件，确认详情页仍显示上次同步结果。

## 11. 与插件配置的对应关系

| 插件配置项 | 作用 | 是否进入请求体 |
| --- | --- | --- |
| `client_id` | Google OAuth 客户端 ID | 是，字段 `client_id` |
| `client_secret` | Google OAuth 客户端密钥 | 是，字段 `client_secret` |
| `refresh_token` | Google OAuth 刷新令牌 | 是，字段 `refresh_token` |
| 同步接口 URL | 你的接收端地址 | 否（请求目标） |
| 同步 token | 自定义授权 token | 是，字段 `token` |
| 定时间隔（小时） | 默认 6，范围 1–168 | 否 |
| 超时（秒） | 默认 30，范围 5–300 | 否 |

## 12. 兼容与变更策略

- 当前契约版本：**v1**，请求体四字段，任意 `2xx` 成功。
- 新增字段：接收端必须忽略未知字段；插件会在变更说明中标注。
- 删除/改名/改变成功判定：属于破坏性变更，会提升接口契约版本并单独通知。
- 不保证请求顺序；并发请求以最后写入为准（建议接收端加行级锁或数据库 upsert）。
- 插件不发送 `If-Match`/`ETag`，接收端不要依赖条件请求。

## 13. 常见问题

**Q：为什么不是 `Authorization: Bearer`？**
A：当前契约把鉴权 token 放在 body 的 `token` 字段，便于接收端与配置值直接比对。请求头中没有额外鉴权信息。

**Q：返回 302 到登录页可以吗？**
A：不可以。插件不跟随重定向，`3xx` 一律失败。

**Q：必须返回 JSON 吗？**
A：不必。插件只看状态码。返回 JSON 只是为了便于人工排查。

**Q：可以要求插件带签名吗？**
A：当前版本不支持。若需要更强鉴权，请在网关层叠加 mTLS/IP 白名单，或提出契约变更。

**Q：插件多久推一次？**
A：默认 6 小时，可配置 1–168 小时；另外用户可随时手动触发。没有「配置变更即推送」。
