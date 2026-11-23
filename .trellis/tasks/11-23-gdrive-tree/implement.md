# Google Drive 目录树插件 — 执行计划

## 0. 前置（规划阶段已完成）

- [x] 确认抓取策略：方法三 = `files.list` 全量拉取 + 本地按根裁剪（`research/google-drive-tree-strategy.md`）。
- [x] 确认跨插件配置读取：宿主公开方法 `get_config(plugin_id)`。
- [x] 确认插件自有数据库：`plugin_declarative_base()` + `get_database_models()`。
- [x] 确认命名与范围：`GDriveTree`，默认根目录子树。
- [ ] 评审 `prd.md` / `design.md` 后执行 `task.py start`。

## 1. 纯逻辑层

- [ ] 创建 `plugins.v3/gdrivetree/` 目录。
- [ ] 实现 `tree_builder.py`：
  - [ ] `TreeNodeData`、`TreeStats` dataclass。
  - [ ] `build_tree(files, root_id, root_name, root_path, max_nodes)`。
  - [ ] 多 parents 取第一个；根不在 files 中时合成根。
  - [ ] BFS 裁剪、路径拼接、深度、统计、排序。
  - [ ] 时间解析容错、size 容错、快捷方式字段。
  - [ ] `max_nodes` 超限抛 `GDriveTreeError`。
- [ ] 实现 `gdrive_tree_client.py`：
  - [ ] `_HttpxTransport` 延迟导入 `httpx2`，回退 `httpx`；支持注入 transport。
  - [ ] `refresh_access_token` 缓存 + 强制刷新。
  - [ ] `get_file`、`resolve_root`（root 别名 / 快捷方式 / driveId）。
  - [ ] `iter_files` 分页、`max_pages`、令牌重复保护、`stop_event`、回收站过滤。
  - [ ] 401 刷新重试、429/5xx 退避、错误类型层次。
- [ ] 创建 `tests/v3/gdrivetree/`：
  - [ ] `test_tree_builder.py` 覆盖建树/裁剪/统计/多父/快捷方式/超限。
  - [ ] `test_gdrive_tree_client.py` 使用 FakeTransport 覆盖分页/根解析/认证/取消。

## 2. 插件主体 `__init__.py`

- [ ] 定义 `TreeMeta`、`TreeNode`（继承 `plugin_declarative_base()`）。
- [ ] `plugin_name`、`plugin_version="1.0.0"`、`plugin_config_prefix="gdrivetree_"`、`auth_level=1`。
- [ ] `get_database_models()` 返回两张表。
- [ ] 配置：`_normalize_config`、`get_form` 默认值、`_clamp_int`。
- [ ] 生命周期：
  - [ ] `init_plugin`：`stop_service()` → 归一化配置 → 重建 stop_event/lock → 是否自动刷新。
  - [ ] `stop_service`：置位 stop_event、关闭客户端、等待线程最多 5 秒。
  - [ ] `get_state`。
- [ ] 刷新编排：
  - [ ] `_read_source_config()` 校验 `GDriveStorage` 配置。
  - [ ] `start_refresh()` 非阻塞锁 + 后台线程。
  - [ ] `_run_refresh()`：写 running → resolve_root → iter_files → build_tree → 单事务 delete/insert/update meta。
  - [ ] 失败/取消写 `failed`/`cancelled`，保留旧节点。
  - [ ] `refresh_from_scheduler()` 只启动后台线程。
- [ ] `get_service()`：启用且 `auto_refresh` 时注册 interval（`seconds=hours*3600`）。
- [ ] 页面：
  - [ ] `get_page()` 状态/统计/错误/按钮/分页表格；VAlert 加 `flex: 0 0 auto;`；根容器固定高度。
  - [ ] `get_api()`：`/refresh`、`/status`、`/nodes/page`、`/search`、`/clear`。
  - [ ] 所有 API 恰好三键 envelope；异常内部捕获。
- [ ] 数据库访问统一使用 `self.get_database().session()`；分页查询按 `path`、`id` 排序。

## 3. 契约与发布

- [ ] `plugins.v3/gdrivetree/README.md`：配置复用、方法限制、刷新、常见错误。
- [ ] `package.v3.json` 新增 `GDriveTree`：name/description/labels/version/icon(`gdrive.png`)/author/level/system_version/history(`v1.0.0`)。
- [ ] `tests/v3/gdrivetree/test_plugin_contract.py`：
  - [ ] 版本三处一致。
  - [ ] `get_api` 三键 envelope 静态检查。
  - [ ] `get_database_models` 模型继承与表名。
  - [ ] 图标文件存在。
  - [ ] 插件导入的 `app.*` 模块限于公开 SDK。

## 4. 验证命令

```bash
python -m py_compile plugins.v3/gdrivetree/__init__.py plugins.v3/gdrivetree/gdrive_tree_client.py plugins.v3/gdrivetree/tree_builder.py
python -m pytest tests/v3/gdrivetree -q
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
git diff --check
```

## 5. v1.1.0 增量能力

- [x] 配置 `incremental_write` / `archive_watch` / `archive_poll_interval_minutes`。
- [x] 新增 `ArchiveLink` 表并交给宿主建表。
- [x] `_save_snapshot` 分派整体替换 / 增量写库（删除缺失、插入新增、更新变化）。
- [x] 新增 `archive_source.py`，只读宿主整理历史并过滤 gdrive 目标。
- [x] 监听 `EventType.TransferComplete`，后台线程增量入树。
- [x] 注册归档轮询服务，对比 `archive_link` 去重并限制重试。
- [x] 按 fileid 拉父链，或按路径定位父目录 + `find_child` 查询。
- [x] 页面/API 增加归档统计、扫描按钮、`/archive/scan`、`/archive/status`。
- [x] 测试：增量写库、归档事件、轮询去重、失败重试、服务开关、`find_child`。
- [x] 版本升至 1.1.0 并更新 `package.v3.json` history。

## 6. 完成标准

- [ ] 全部验收标准（`prd.md`）可核对。
- [ ] 本地测试全绿。
- [ ] 无敏感信息写入日志/页面/仓库。
- [ ] 更新 `.trellis/spec/plugin-v3/`（如产生新的可复用契约：跨插件配置读取、插件自有表刷新原子性）。
