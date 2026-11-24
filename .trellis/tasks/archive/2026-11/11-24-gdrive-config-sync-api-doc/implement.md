# GDriveStorage 配置同步：接收端接口文档 — 执行计划

父任务：`11-24-gdrive-config-sync`。只有文档产出，不修改插件代码。

## 前置

- [ ] `task.py start 11-24-gdrive-config-sync-api-doc`。
- [ ] 读父 `design.md` §2（请求契约）与 §5（错误矩阵），以其中字段为准。

## 文档结构（写入 `docs/gdrive-config-sync-api.md`）

1. **标题与适用范围**
   - 说明由 MoviePilot `GDriveStorage` 插件主动推送，单向。
2. **请求概要**
   - `POST {接收端 URL}`，JSON，无查询参数。
   - Header 表：`Content-Type`、`User-Agent`。
   - 不跟随重定向的说明。
3. **鉴权**
   - `token` 在 body；与插件配置 `sync_token` 比对。
   - 常量时间比较建议；失败返回 401/403。
4. **请求体**
   - 四字段表：字段、类型、必填、含义、来源。
   - 完整 JSON 示例（占位符）。
   - 强调「恰好四个字段，无外层包装」。
5. **响应与成功判定**
   - 任意 2xx；建议 `200 {"success": true}`。
   - 插件不解析响应体。
6. **错误处理**
   - 建议状态码表；非 2xx 时插件记录状态码 + 最多 200 字符响应片段。
   - 超时默认 30s，接收端应快速返回。
7. **幂等与存储语义**
   - 按 `client_id` upsert；全量覆盖；重复推送安全。
8. **安全建议**
   - HTTPS、日志掩码、凭据最小化、不要在错误响应中回显请求体。
9. **示例**
   - `curl`。
   - Python FastAPI 骨架（读取 Pydantic 模型四字段、校验 token、upsert、返回 2xx）。
10. **联调 checklist**
    - 插件配置 URL/token → 保存 → 手动同步 → 接收端 2xx → 页面显示成功 → 重载后状态保留 → 定时开关验证。
11. **兼容策略**
    - 忽略未知字段；删除/改名属破坏性变更；当前无版本/时间戳字段。

## 质量检查

- [ ] 逐项对照父 `design.md` §2.2 字段表，确认拼写：`client_id`、`client_secret`、`refresh_token`、`token`。
- [ ] 确认没有写成 `clientId`、`clientSecret`、`refreshToken`。
- [ ] 确认成功判定写为「任意 2xx」，不是 `body.code == 0`。
- [ ] 确认说明 `3xx` 视为失败（插件不跟随重定向）。
- [ ] 确认示例无真实密钥。
- [ ] 与实现子任务完成后回查：Header、body、成功判定一致。

## 验证

```bash
# 文档存在且关键字段齐全
python - <<'PY'
from pathlib import Path
text = Path("docs/gdrive-config-sync-api.md").read_text(encoding="utf-8")
for key in ("client_id", "client_secret", "refresh_token", '"token"', "2xx", "POST"):
    assert key in text, key
print("ok")
PY
```

（Windows/PowerShell 环境可用等价的 `Select-String` 检查。）

## 回滚点

- 仅新增一个 Markdown 文件；回滚即删除该文件，不影响代码。
