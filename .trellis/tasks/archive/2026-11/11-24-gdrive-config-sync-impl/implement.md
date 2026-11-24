# GDriveStorage 配置同步：插件端实现 — 执行计划

父任务：`11-24-gdrive-config-sync`。开始前先读父 `design.md` §2–§5 与本文 `design.md`。

## 前置

- [ ] `task.py start 11-24-gdrive-config-sync-impl`（状态变 `in_progress` 后才改代码）。
- [ ] 阅读 `research/gdrive-storage-sync-code-map.md` 确认集成点。

## 实施顺序

### 1. 纯推送模块

- [ ] 新建 `plugins.v3/gdrivestorage/sync_client.py`：
  - `SyncResult`、`build_payload`、`validate_sync_url`；
  - `SyncTransport` 协议、`HttpxSyncTransport`（httpx2 → httpx）；
  - `GDriveConfigSyncClient.push()`，`follow_redirects=False`，异常收敛，200 字符截断。
- [ ] 新建 `tests/v3/gdrivestorage/test_sync_client.py`，先跑通纯逻辑。

验证：

```bash
python -m py_compile plugins.v3/gdrivestorage/sync_client.py
pytest tests/v3/gdrivestorage/test_sync_client.py -q
```

**回滚点 1**：此步骤独立，若需要可删除两个文件，不影响插件。

### 2. 配置与生命周期

- [ ] `FORM_KEYS` 追加 5 键。
- [ ] `_normalize_config()` 追加 5 项（clamp）。
- [ ] `get_form()` 默认值 + 同步分组 + 安全提示。
- [ ] `__init__()` 初始化 `_sync_lock`、`_sync_status`、`_sync_client`。
- [ ] `init_plugin()` 读取同步配置/状态、构造客户端、http 警告。
- [ ] `stop_service()` 关闭同步客户端。

验证：`pytest tests/v3/gdrivestorage/test_plugin_contract.py -q`（此时应因 `REQUIRED_FORM_KEYS` 未更新而失败，下一步修复）。

### 3. 调度与手动 API

- [ ] 新增常量 `DEFAULT_SYNC_INTERVAL_HOURS=6`、`DEFAULT_SYNC_TIMEOUT=30`。
- [ ] 实现 `_sync_missing()`、`_sync_ready()`、`get_service()`、`sync_from_scheduler()`、`_sync_worker()`、`_perform_sync()`、`api_sync_push()`。
- [ ] `get_api()` 追加 `/sync/push`。
- [ ] 确认 `api_sync_push` 每个 `return` 字面量恰好三键。

**回滚点 2**：`get_service()` 返回 `[]` 即可临时关闭定时能力。

### 4. 详情页

- [ ] `get_page()` 增加同步状态告警与「立即同步配置」按钮；告警带 `style: "flex: 0 0 auto;"`。
- [ ] 状态文本只显示 URL 的 `netloc + path`，不显示凭据。

验证：`pytest tests/v3/gdrivestorage/test_plugin_contract.py -q::test_get_page_does_not_raise_in_all_states`（或等价用例）。

### 5. 测试与发布

- [ ] 更新 `REQUIRED_FORM_KEYS` 加 5 键。
- [ ] 新增契约用例：`get_service()` 开关/完整性；`get_api` 含 `/sync/push`。
- [ ] `plugin_version = "1.1.4"`。
- [ ] `package.v3.json`：`GDriveStorage.version = "1.1.4"`，`history` 顶部加 `v1.1.4` 描述（定时/手动推送、字段、2xx 判定、安全提示）。
- [ ] `plugins.v3/gdrivestorage/README.md` 增加「配置同步」章节（配置项表、触发方式、请求体、安全提示、接收端文档链接）。

## 最终验证

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/sync_client.py
pytest tests/v3/gdrivestorage -q
python -c "import json;d=json.load(open('package.v3.json',encoding='utf-8'))['GDriveStorage'];print(d['version'],list(d['history'])[0])"
```

- [ ] 全绿；版本三处一致。
- [ ] `grep -n "logger.*client_secret\|logger.*refresh_token\|logger.*sync_token" plugins.v3/gdrivestorage/__init__.py` 无命中。
- [ ] 检查 `get_api()` 所有端点返回字典仍为三键。

## 质量门

- 遵循 `.trellis/spec/plugin-v3/page-api-envelope.md`。
- 不新增依赖，不导入 `app.core`/`app.helper`/`app.utils`/`app.db.models`。
- 失败路径均为可读中文提示，不抛出未捕获异常。
- 完成后交给 `trellis-check` 或父任务做契约回查。

## 回滚点总结

1. 删除 `sync_client.py` + 其测试 → 回到无同步模块状态。
2. `get_service()` 条件化返回 → 关闭定时。
3. 全部还原：移除新增配置键、API 条目、页面块，恢复版本号；无数据库迁移。
