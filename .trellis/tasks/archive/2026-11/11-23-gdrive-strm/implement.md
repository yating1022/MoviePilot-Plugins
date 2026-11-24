# Google STRM 管理 — 实施计划

## 前置

- 任务状态需先 `task.py start` 后进入实现。
- 先读 `.trellis/spec/plugin-v3/` 三份规范（页面 envelope、插件数据库、存储扩展）。
- 参考实现：`plugins.v3/gdrivetree/`（线程、快照、事件）、
  `plugins.v3/u115autoupload/`（页面/API 刷新）。

## 实施步骤

### 1. GDriveTree 变更通知（先做，事件契约先行）

- [ ] 新增 `plugins.v3/gdrivetree/tree_events.py`：
  - `TreeChangeSet`、`node_payload`、`removed_payload`、`build_payload`；
  - `EVENT_NAME = "tree.changed"`、`MAX_EVENT_NODES = 2000`；
  - 超限/`full` 时清空列表并置 `truncated`。
- [ ] `plugins.v3/gdrivetree/__init__.py`：
  - 配置归一化、类字段、`init_plugin`、`get_form`、默认值新增
    `strm_notify` / `strm_plugin_id`；
  - `_instance_id()` 与 `_publish_tree_change(change)`；
  - `_save_snapshot_incremental` 收集 added/updated/removed 并返回 `TreeChangeSet`；
  - `_save_snapshot_replace` 返回 full `TreeChangeSet`；
  - `_save_snapshot` 透传返回值，提交成功后发布；
  - `_upsert_nodes` 返回新增/更新并发布 `kind="archive"`；
  - `api_clear` 发布 `kind="clear", full=True`；
  - 所有发布 try/except 隔离。
- [ ] `plugins.v3/gdrivetree/plugin_version = "1.2.0"`，README 配置表与 API 说明更新。
- [ ] `package.v3.json`：GDriveTree 版本 1.2.0 + history 条目。

验证：

```bash
python -m pytest tests/v3/gdrivetree -q
python -m py_compile plugins.v3/gdrivetree/__init__.py plugins.v3/gdrivetree/tree_events.py
```

### 2. 新插件骨架

- [ ] 新建 `plugins.v3/gdrivestrm/__init__.py`，类 `GDriveStrm`：
  - `plugin_name = "谷歌 STRM 管理"`、`plugin_version = "1.0.0"`、
    `plugin_config_prefix = "gdrivestrm_"`、`plugin_order = 102`、`auth_level = 1`；
  - `get_database_models()` 返回 `StrmMeta`、`StrmRecord`；
  - 空实现的生命周期方法（后续步骤填充）。
- [ ] `package.v3.json` 新增条目：name/description/labels/version/icon(`cloudstrm.png`)/
      author/level/system_version/history。
- [ ] 新增 `plugins.v3/gdrivestrm/README.md`（功能、前置、配置、API、FAQ）。

### 3. 纯逻辑模块

- [ ] `strm_mapper.py`：
  - 默认后缀、`parse_extensions`、`is_video_name`、`render_url`；
  - `strip_root_path`、`build_rel_target`、`build_targets`；
  - 路径安全校验（绝对、`..`、NUL）。
- [ ] `tests/v3/gdrivestrm/_plugin_loader.py` + `test_strm_mapper.py`：
  - 根路径带前缀、目录节点、大小写后缀、快捷方式目标 ID、非法路径。

验证：

```bash
python -m pytest tests/v3/gdrivestrm/test_strm_mapper.py -q
```

### 4. 跨插件读取

- [ ] `tree_source.py`：`TreeSourceError`、`TreeFile`、`read_root_path`、
      `iter_tree_files`；使用原生 SQL 与 `get_database(plugin_id)`。
- [ ] 单测：SQLite 内存库建 `tree_meta`/`tree_node`，覆盖有数据、未建树、缺表。

### 5. 同步引擎

- [ ] `strm_syncer.py`：`StrmRecordData`、`SyncOutcome`、`StrmSyncer`；
  - `full_sync`：内容校验、跳过一致、清理失效、空目录回收；
  - `apply_changes`：新增/更新/改名迁移/删除/转非视频；
  - 原子写、根内校验、`.strm` 删除限制、`stop_event` 检查。
- [ ] `tests/v3/gdrivestrm/test_strm_syncer.py`（tmp_path）：
  - 创建层级、幂等 mtime、内容变化重写、清理开关、改名迁移、
    不停根目录、去掉空目录、单文件错误继续。

验证：

```bash
python -m pytest tests/v3/gdrivestrm/test_strm_syncer.py -q
```

### 6. 插件编排

- [ ] `__init__.py` 配置归一化/表单/默认值（字段见 design 8.1）。
- [ ] 工作线程：入队合并（full > diff > event）、5000 节点升级、异常隔离。
- [ ] `get_service()`：`auto_sync` 时注册 interval，入队 diff。
- [ ] 事件处理器 `@eventmanager.register(EventType.PluginTriggered)`：
  - 兼容 dict 与 Pydantic 对象读取；
  - 校验 `event_watch`、`plugin_id == tree_plugin_id`、`event_name == "tree.changed"`。
- [ ] 执行器：
  - 解析输出根（相对路径按 `/config`）；
  - 读取树 → 目标集合 → 调 syncer；
  - 单事务写 record、更新 meta；
  - 失败仅更新 meta，不清空记录。
- [ ] API：`/sync`、`/sync/full`、`/status`、`/records/page`、`/clear`。
- [ ] `get_page()`：状态、按钮、分页表（遵守 envelope 与 VAlert 样式规范）。

### 7. 运行时与契约测试

- [ ] `tests/v3/gdrivestrm/test_plugin_runtime.py`：
  - 宿主桩含 `EventType.PluginTriggered`、`eventmanager`（记录注册与发送）；
  - 建表、写入一棵树、全量同步生成正确路径与内容；
  - 事件新增 → 秒级（直接调用处理器）生成；事件删除 → 清理开关两种行为；
  - diff 补齐丢失文件；重复全量 mtime 不变；
  - 源树缺失时 meta failed 且文件保留。
- [ ] `tests/v3/gdrivestrm/test_plugin_contract.py`：
  - 目录名 = 类名小写、版本与 `package.v3.json` 一致；
  - `get_api` 全部端点三键 envelope；
  - `get_database_models` 表名与基类；
  - 允许的 `app.*` 导入前缀。
- [ ] `tests/v3/gdrivetree/test_tree_events.py`：
  - 载荷字段、超限退化、full、removed 结构；
- [ ] `tests/v3/gdrivetree/test_plugin_runtime.py` 增补：
  - 开启通知后刷新/归档发布 `tree.changed`；
  - 关闭通知或目标为空时不发布；
  - 发布异常不影响刷新。

### 8. 全量验证

```bash
python -m pytest tests/v3/gdrivestrm tests/v3/gdrivetree -q
python -m py_compile plugins.v3/gdrivestrm/__init__.py \
  plugins.v3/gdrivestrm/strm_mapper.py \
  plugins.v3/gdrivestrm/tree_source.py \
  plugins.v3/gdrivestrm/strm_syncer.py \
  plugins.v3/gdrivetree/__init__.py \
  plugins.v3/gdrivetree/tree_events.py
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
```

## 风险文件与回滚点

| 文件 | 风险 | 控制 |
| --- | --- | --- |
| `plugins.v3/gdrivetree/__init__.py` | 改动成熟插件，可能影响刷新 | 通知逻辑全部 try/except；默认关闭；先补测试 |
| `plugins.v3/gdrivestrm/__init__.py` | 线程生命周期 / 页面契约 | 严格按 gdrivetree 线程模式；三键 envelope 静态测试 |
| `package.v3.json` | 版本/JSON 格式错误 | `json.load` + 版本一致性测试 |
| 输出目录删除逻辑 | 误删用户文件 | 只删 `strm_record` 记录且 `.strm` 后缀、根内校验 |

回滚点：

1. GDriveTree 改动完成并通过测试后可单独提交；关闭通知即恢复原行为。
2. 新插件先以「禁用」状态交付；启用后才写文件。
3. 同步出错只写 meta，不删数据；必要时清空 `strm_record` 后重新全量。
