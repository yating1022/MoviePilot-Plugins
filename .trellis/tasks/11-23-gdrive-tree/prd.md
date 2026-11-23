# Google Drive 目录树插件 — 需求文档

## Goal

在 MoviePilot V3 中新增 **GDriveTree** 插件：复用现有 `GDriveStorage` 插件的 Google OAuth 配置，以「一次性全量拉取 + 本地建树」的方式生成指定根目录的目录树，并持久化到插件自有数据库，供详情页浏览、搜索以及后续插件/接口查询。

## 背景与已确认决策

- 抓取方式采用参考文章的「方法三」：`files.list` 分页全量拉取，本地按 `parents` 建树。该接口无法递归查询子树，因此必须「全量拉取 + 按根裁剪」。
- 凭据自动复用 `GDriveStorage` 配置，新插件不重复 OAuth 授权。
- 数据库默认只存 `GDriveStorage` 根目录可达的子树。
- 刷新支持手动 + 定时。
- 插件目录 `plugins.v3/gdrivetree/`，类名 `GDriveTree`。

## Functional Requirements

### FR1 配置复用
- FR1.1 通过宿主公开方法 `get_config(source_plugin_id)` 读取 `client_id`、`client_secret`、`refresh_token`、`root_folder_id`、`root_path`、`use_shared_drive`、`shared_drive_id`。
- FR1.2 源插件 ID 可配置，默认 `GDriveStorage`，以支持插件分身。
- FR1.3 源配置缺失、未授权或 `refresh_token` 为空时，页面明确提示「先配置/授权 GDriveStorage」，不抛出未捕获异常。
- FR1.4 不在日志、页面或 API 中回显 `client_secret`、`refresh_token`、访问令牌。

### FR2 目录树抓取
- FR2.1 使用 Drive REST `files.list`，`pageSize=1000`，携带 `id,name,mimeType,parents,size,modifiedTime,createdTime,md5Checksum,driveId,shortcutDetails` 字段。
- FR2.2 解析根节点：支持 `root` 别名、普通文件夹 ID、共享盘文件夹、文件夹快捷方式；据此选择 `corpora=user` 或 `corpora=drive&driveId=...`。
- FR2.3 拉取后在本地以根节点为起点做 BFS，只保留可达节点；节点路径以 `GDriveStorage.root_path` 为前缀（默认 `/`）。
- FR2.4 默认排除回收站文件；提供「包含回收站」开关。
- FR2.5 提供最大页数、最大节点数保护；超限中止并保留旧数据。
- FR2.6 统计目录数、文件数、总大小、最大深度、请求页数与耗时。
- FR2.7 同一时刻只允许一个刷新任务；重复触发返回「正在刷新」。

### FR3 数据持久化
- FR3.1 新建插件自有表存储节点，至少包含：`file_id`、`parent_file_id`、`name`、`mime_type`、`is_dir`、`size`、`modified_time`、`path`、`depth`、`md5`、`trashed`、`created_at`。
- FR3.2 同一快照内 `file_id` 唯一；`file_id`、`parent_file_id`、`path`、`name` 建立索引。
- FR3.3 刷新采用「构建完成后原子替换」：失败时回滚，上一次成功数据保持不变。
- FR3.4 记录最后一次成功时间、状态、统计、错误信息与耗时。
- FR3.5 提供「清空目录树数据」操作。

### FR4 刷新触发
- FR4.1 详情页提供「立即刷新」按钮，刷新在后台线程执行，HTTP 请求立即返回。
- FR4.2 通过 `get_service()` 注册定时刷新（`interval`），间隔可配置，可单独关闭。
- FR4.3 插件停用或重载时安全停止后台任务，不残留线程。

### FR5 页面与 API
- FR5.1 详情页显示：源配置状态、根目录、抓取模式、最后刷新时间、刷新状态、统计、最近错误、节点分页列表。
- FR5.2 提供 API：立即刷新、刷新状态、节点分页、按名称/路径搜索、清空数据。
- FR5.3 所有页面 API 返回恰好三键 `{success, message, data}`；成功时宿主自动刷新页面。
- FR5.4 节点列表默认按路径排序，每页数量可配置（默认 20）。

### FR6 发布与文档
- FR6.1 在 `package.v3.json` 增加 `GDriveTree` 条目，版本、history、labels、icon、system_version 与插件类一致。
- FR6.2 提供 `plugins.v3/gdrivetree/README.md`，说明配置复用、方法限制（全量拉取）、刷新方式与常见错误。
- FR6.3 版本号三处一致：`plugin_version`、`package.v3.json.version`、`history` 顶部条目。

## Non-functional Requirements

- NFR1 只在 `init_plugin` 之后执行网络/数据库操作；不在模块导入期或类定义期产生副作用。
- NFR2 只使用 `app.sdk.*`、`app.schemas.*` 公开契约；不导入 `app.db.models.*` 等宿主内部模块。
- NFR3 不修改宿主封闭枚举或内部单例。
- NFR4 兼容 SQLite 与 PostgreSQL 插件库；表名不依赖数据库前缀。
- NFR5 无新增第三方依赖。
- NFR6 1 万文件量级的刷新目标在 1–2 分钟内完成；节点分页查询响应 < 200ms。

## Constraints

- 不通过 `import` 复用 `gdrivestorage` 的私有客户端代码；只通过 `get_config` 共享配置，避免插件加载顺序与包名耦合。
- 抓取范围默认是 `GDriveStorage` 根目录子树；「全账号入库」不在第一版范围。
- 第一版全量重建，不做增量 diff。

## Acceptance Criteria

- [ ] 启用插件且 `GDriveStorage` 已授权时，详情页显示「配置可用」、根目录与抓取模式。
- [ ] 点击「立即刷新」后状态变为「刷新中」，请求立即返回；完成后状态为「成功」并显示统计。
- [ ] 数据库中只存在根目录可达节点；从根节点按 `parent_file_id` 可遍历到所有入库节点。
- [ ] 同一快照内 `file_id` 无重复；`path` 以配置的 `root_path` 开头。
- [ ] 授权失效或网络错误时，刷新状态为「失败」，错误可见，旧数据未被删除。
- [ ] 定时刷新按配置间隔触发；关闭开关后不再触发。
- [ ] 搜索接口可按名称或路径关键字分页返回节点。
- [ ] 页面 API 均返回三键 envelope，页面按钮点击后自动刷新。
- [ ] `python -m py_compile` 通过；建树/裁剪/分页/错误处理有单元测试；`pytest` 通过。
- [ ] `package.v3.json` 与 `plugin_version` 一致，图标文件存在。

## Out of Scope

- 文件内容下载、上传、整理、删除。
- 多 Google 账号并行抓取。
- 增量同步/变更监听。
- 图形化树组件；第一版以统计卡片 + 分页表格 + 搜索呈现。
- 对非 `gdrive` 存储的目录树抓取。
