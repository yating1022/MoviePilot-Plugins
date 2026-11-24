# Google Drive 存储配置同步 — 执行计划（父任务）

本任务自身不直接写业务代码，负责契约、拆分、跨子任务验收与最终集成检查。

## 任务地图

| 顺序 | 任务 | 产物 | 前置 |
| --- | --- | --- | --- |
| 1 | `11-24-gdrive-config-sync-impl` | `sync_client.py`、插件集成、测试、版本、README | 父 `design.md` 契约 |
| 1（可并行） | `11-24-gdrive-config-sync-api-doc` | `docs/gdrive-config-sync-api.md` | 父 `design.md` 契约 |
| 2 | 父任务最终检查 | 字段一致性回查、全量测试、发布信息 | 两个子任务完成 |

## 执行清单

- [ ] 审查父子任务 PRD、`design.md` 是否固化以下决策：四字段 body、snake_case、2xx 成功、小时/默认 6、仅定时+手动、token 在 body。
- [ ] 启动子任务 A：`python ./.trellis/scripts/task.py start 11-24-gdrive-config-sync-impl`。
- [ ] 子任务 A 完成后启动子任务 B（或提前起草、实现完成后回查）：`task.py start 11-24-gdrive-config-sync-api-doc`。
- [ ] 对照 `design.md` §2 检查文档字段、Header、成功判定、超时、重定向语义。
- [ ] 对照实现检查文档：请求体键名、成功判定、`User-Agent`、是否附加额外字段。
- [ ] 运行验证命令：

```bash
python -m py_compile plugins.v3/gdrivestorage/__init__.py plugins.v3/gdrivestorage/sync_client.py
pytest tests/v3/gdrivestorage -q
python -c "import json;json.load(open('package.v3.json',encoding='utf-8'))"
```

- [ ] 版本一致性：`plugin_version`、`package.v3.json.version`、`history` 顶部条目均为 `1.1.4`。
- [ ] 安全检查：`grep -n "client_secret\|refresh_token\|sync_token" plugins.v3/gdrivestorage/__init__.py`，确认无日志/页面明文输出。
- [ ] 回滚点：上述新增均为可选配置；若需回滚，移除新增配置键与 `get_service` 分支即可恢复原行为。
- [ ] 归档两个子任务，再归档父任务；提交信息遵循仓库约定。

## 评审门

1. 子任务 A 完成后：父任务检查实现是否偏离契约，偏离则退回 A 修改。
2. 子任务 B 完成后：字段与实现逐项对照。
3. 提交前：`pytest` 全绿、版本一致、无凭据泄漏。

## 验证命令说明

- 本仓库不是完整 MoviePilot 宿主，插件测试通过 `tests/v3/gdrivestorage/` 内的 `app.*` 桩运行。
- 真实推送需宿主环境；本任务最低要求为单测 + 编译 + 静态契约测试通过。
