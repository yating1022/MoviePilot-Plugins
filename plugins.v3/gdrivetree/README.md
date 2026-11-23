# Google Drive 目录树（GDriveTree）

复用 [Google Drive 存储](../gdrivestorage/) 插件的 OAuth 配置，一次性拉取 Google Drive
文件元数据，在本地按根目录裁剪建树，并把目录树持久化到插件自有数据库。

## 功能

- **配置复用**：不重复授权。通过宿主公开方法 `get_config("GDriveStorage")` 读取
  `client_id`、`client_secret`、`refresh_token`、`root_folder_id`、`root_path` 等。
- **快速建树**：使用 Drive REST `files.list` 分页（每页最多 1000 条）全量拉取，
  再在本地按 `parents` 关系从根目录 BFS 裁剪，避免逐层递归造成的请求膨胀。
- **持久化**：节点存入插件自有表 `tree_node`，刷新状态与统计存入 `tree_meta`。
- **刷新方式**：详情页「立即刷新」+ 可配置间隔的定时刷新。
- **增量写库**：全量刷新后按 `file_id` 对比数据库，只删除缺失、插入新增、更新变化行。
- **归档增量**：监听整理完成事件，并定时对比宿主归档记录；通过 `archive_link` 表去重，
  把新归档到 Google Drive 的文件及其父目录增量加入目录树。
- **STRM 联动**：可选中开启「通知 STRM 插件」，刷新/归档/清空后向指定插件发送
  `tree.changed` 事件，供 Google STRM 管理插件秒级增量生成 STRM。
- **浏览与搜索**：详情页分页表格；`/search` API 支持按名称/路径关键字搜索。

## 前置条件

1. MoviePilot V3（`>= 3.0.0`）。
2. 已安装、启用并完成 **GDriveStorage** 的 Google OAuth 授权。
3. GDriveStorage 的根目录配置（`root_folder_id` / `root_path`）就是本插件要抓取的树根。
   如果 GDriveStorage 使用共享盘或文件夹快捷方式，本插件会自动解析。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 插件总开关 |
| 定时刷新 | 关 | 注册 interval 定时任务 |
| 刷新间隔（小时） | 24 | 1–168 |
| 源插件 ID | `GDriveStorage` | 使用插件分身时填分身 ID |
| 包含回收站 | 关 | 开启后 `trashed` 文件也入库 |
| 每页节点数 | 20 | 详情页分页大小 |
| 最大拉取页数 | 2000 | 每页最多 1000 条；超限中止 |
| 最大入库节点数 | 200000 | 超限中止并保留旧数据 |
| 全量刷新时增量写库 | 开 | 只删除缺失、插入新增、更新变化节点 |
| 监控归档记录增量入树 | 开 | 监听整理完成事件 + 定时对比归档记录 |
| 归档轮询间隔（分钟） | 5 | 1–1440 |
| 通知 STRM 插件 | 关 | 目录树变化后发送 `tree.changed` 事件 |
| STRM 插件 ID | `GDriveStrm` | 目标插件实例 ID；分身填分身 ID |

## 抓取原理与限制

参考文章「方法三」的本质是：**一次性全量拉取 + 本地建树**。

- Drive 查询语言只有 `'<id>' in parents`（匹配直接子项），没有递归子树条件。
- 因此插件不能只请求某个子树；它先按根节点确定可枚举范围：
  - 根在共享盘内 → `corpora=drive&driveId=...`；
  - 其它情况 → `corpora=allDrives`，覆盖「我的云端硬盘」「共享给我」的文件夹和可访问的共享盘。
- 根目录下如果还有团队盘/共享文件夹快捷方式，插件会展开快捷方式指向的目录。
- 拉取完成后，只保留从根目录可达的节点入库，根子树之外的元数据会被丢弃。
- 如果账号里无关文件很多，全量拉取会变慢。可参考原文建议：单独开一个 Google
  账号，只把目标文件夹共享给它，再让 GDriveStorage 使用该账号授权。

## 数据表

- `tree_meta`：单行（`id=1`）保存刷新状态、根信息、统计、错误。
- `tree_node`：目录树节点快照。刷新成功时在**单事务**内按配置整体替换或增量更新；
  刷新失败/中止只更新 `tree_meta`，旧节点保持不变。
- `archive_link`：归档记录处理状态。`history_id` 去重，成功记 `done`，失败记 `failed`
  并累计重试次数（最多 3 次）。

## API

| 路径 | 参数 | 说明 |
| --- | --- | --- |
| `/refresh` | 无 | 启动后台刷新 |
| `/status` | 无 | 刷新状态与源配置状态 |
| `/nodes/page` | `{"page": 1}` | 节点分页 |
| `/search` | `{"keyword": "x", "page": 1}` | 按名称/路径搜索 |
| `/clear` | 无 | 清空节点并重置统计；刷新进行中会被拒绝 |
| `/archive/scan` | 无 | 立即扫描归档记录 |
| `/archive/status` | 无 | 归档增量处理状态 |

所有接口返回恰好三键 `{success, message, data}`。

## STRM 事件通知

开启「通知 STRM 插件」后，插件在以下时点通过
`EventType.PluginTriggered` 定向发送事件（`event_name = "tree.changed"`）：

- 全量/增量刷新成功写库后（`kind = "refresh"`）；
- 整理完成或归档轮询增量入树后（`kind = "archive"`）；
- 清空目录树后（`kind = "clear"`，`full = true`）。

事件 `data` 携带 `added` / `updated` / `removed` 节点明细；变更节点超过
2000 个或整体替换时只发送 `full = true` 的全量信号。目标是事件数据中的
`__mp_target_plugin_id`，只有该插件实例会收到。通知失败只记日志，不影响刷新。

## 常见错误

| 提示 | 处理 |
| --- | --- |
| 未找到插件 GDriveStorage 的配置 | 先启用并保存 GDriveStorage 配置 |
| 缺少 client_id/client_secret/refresh_token | 在 GDriveStorage 完成 OAuth 授权 |
| refresh_token 可能已失效 | 重新授权 GDriveStorage |
| 分页超过上限 / 节点数超过上限 | 缩小根目录范围，或调大 `max_pages` / `max_nodes` |
| 根目录不存在或无权限 | 检查 GDriveStorage 的根目录配置与共享盘权限 |
| 根目录不是文件夹 | 把 GDriveStorage 的 `root_folder_id` 指向文件夹，而不是文件 |
| 共享盘根解析失败 | 共享盘 ID 无效时不会再回退账号根；修正 `shared_drive_id` |
| 搜索结果不完整 | Google 返回 `incompleteSearch=true` 时本次刷新中止；缩小范围或重试 |

> `root_folder_id` 与 `shared_drive_id` 支持直接填文件夹分享链接或共享盘 URL，插件会自动提取纯 ID。

## 说明

- 本插件只读 Google Drive，不修改任何文件。
- 第一版为全量重建，不做增量 diff。
- 不下载文件内容；仅保存元数据。
