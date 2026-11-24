# Google STRM 管理

## Goal

新增 MoviePilot V3 插件「谷歌 STRM 管理」（`GDriveStrm`），把 GDriveTree 插件已构建的
Google Drive 目录树映射成本地 STRM 媒体库：

- 输出目录中的文件夹层级与目录树父子结构一致；
- 配置后缀（默认全部常见视频后缀）的文件生成 `.strm`，内容为对应 Drive 文件的直链；
- 目录树发生变化时，GDriveTree 通过插件事件通知本插件，本插件秒级增量落盘；
- 支持定时同步与手动全量同步，弥补事件丢失或插件重启后的漏同步。

## Background / Confirmed Facts

- 目录树插件 `GDriveTree` 将树持久化在自有数据库表 `tree_node` / `tree_meta`
  （`plugins.v3/gdrivetree/__init__.py:52`），节点含 `file_id`、`parent_file_id`、
  `name`、`mime_type`、`is_dir`、`size`、`modified_time`、`path`、`shortcut_target_id`。
- 树路径带 `root_path` 前缀，目录以 `/` 结尾；根节点路径等于 `root_path`
  （`plugins.v3/gdrivetree/tree_builder.py:50`、`:100`）。
- 宿主 `_PluginBase.get_database(plugin_id)` 可取得其他插件自有数据库句柄
  （MoviePilot v3 `app/sdk/plugin/base.py`），SQLite 与 PostgreSQL 均按插件隔离解析。
- 宿主支持定向插件事件：`EventType.PluginTriggered` 配合事件数据中的
  `__mp_target_plugin_id` 只投递给指定插件实例
  （MoviePilot v3 `app/runtime/event/dispatch.py:141`、`:414`）。
- 插件清单为仓库根 `package.v3.json`，插件目录位于 `plugins.v3/<plugin>/`，
  详情页 API 必须返回三键 `{success, message, data}`（见 `.trellis/spec/plugin-v3/`）。
- GDriveTree 现有增量写库逻辑在 `_save_snapshot_incremental`
  （`plugins.v3/gdrivetree/__init__.py:1331`），归档增量在 `_upsert_nodes`。

## Requirements

### R1 新插件与清单

- 新增 `plugins.v3/gdrivestrm/`，插件类名 `GDriveStrm`，显示名「谷歌 STRM 管理」。
- 在 `package.v3.json` 注册条目（版本、历史、标签、图标），并新增 README。
- 插件可独立启用/停用；停用后不注册定时服务、不响应事件、不写文件。

### R2 配置项

- 目录树插件 ID（默认 `GDriveTree`，支持插件分身 ID）。
- STRM 输出根目录（默认 `/config/strm`；相对路径按宿主配置目录解析）。
- 视频后缀白名单（默认覆盖常见视频格式）。
- STRM 内容模板（默认 `https://www.googleapis.com/drive/v3/files/{fileId}?alt=media&supportsAllDrives=true`）。
- 定时同步开关与间隔。
- 事件增量开关（本插件侧是否处理 GDriveTree 事件）。
- 失效 STRM 清理开关，默认开启（见 R7）。
- 详情页记录分页大小。

### R3 目录与文件映射

- 对树中每个命中后缀的视频文件，输出路径为
  `<输出根目录>/<相对树路径去扩展名>.strm`。
- 相对树路径 = 节点 `path` 去掉 `tree_meta.root_path` 前缀。
- 只创建命中视频文件的目录链，不镜像没有任何视频的空目录。
- 父目录不存在时递归创建，保证与树父子结构一致。
- `.strm` 内容为模板渲染后的 URL，`{fileId}` 取节点 `file_id`；
  若节点是文件快捷方式且记录了 `shortcut_target_id`，URL 使用目标文件 ID。
- 默认后缀集合（可配置）：`mkv, mp4, avi, ts, m2ts, mts, iso, rmvb, wmv, mov, flv,
  webm, mpg, mpeg, m4v, vob, 3gp, f4v, asf, divx, ogm, ogv, tp, trp, m2v, mxf`。
- 非视频文件不生成 `.strm`；目录节点不生成文件。
- 内容未变化的 `.strm` 不重复写入（幂等）。

### R4 全量同步

- 手动触发：读取当前完整目录树，重建/校正所有匹配文件的 STRM。
- 全量同步与增量同步互斥，同一时刻只运行一个同步任务。
- 全量同步完成后刷新状态与统计（文件数、目录数、耗时、错误）。

### R5 事件增量

- GDriveTree 配置中新增「通知 STRM 插件」开关与目标插件 ID（默认 `GDriveStrm`）。
- GDriveTree 在以下时点发送插件事件：全量刷新成功写库后、归档增量写库后、清空目录树后。
- 事件载荷包含变更类型与变更节点（新增/更新/删除），超过阈值时退化为「全量信号」。
- 目标插件未安装/未启用/事件被丢弃时，GDriveTree 刷新本身不受影响。
- 本插件收到事件后在后台线程合并短时间内的多次事件，尽快写入新增 STRM，
  避免阻塞事件分发线程。

### R6 定时同步

- 按配置间隔执行增量对账：对比目录树与本地记录，补齐缺失、更新变化、处理删除。
- 定时同步可发现事件丢失（插件重启、事件未送达、手动改动目录树）造成的差异。

### R7 失效清理

- 默认开启：当源文件从树中消失、改为非视频后缀或被移出根目录时，
  删除本插件记录过、且确认源节点已不存在的 `.strm`。
- 只删除本插件数据库记录过的文件；用户自行放入的其他文件绝不删除。
- 清理后删除本插件创建且已为空的父目录，不删除输出根目录。
- 清理开关关闭时只新增/更新，不删除。

### R8 状态与详情页

- 详情页展示：树源连接状态、最近同步时间/状态/错误、STRM 文件数、输出目录。
- 提供「立即增量同步」「全量同步」「清空记录」按钮与最近记录分页表。
- 所有页面 API 返回恰好 `{success, message, data}` 三键。

### R9 测试与文档

- 纯逻辑模块（路径映射、后缀过滤、URL 渲染、差异计算）不依赖宿主，可单测。
- 宿主桩测试覆盖：全量同步、增量事件、删除清理、幂等、源树缺失。
- 静态检查：`get_api` envelope、`package.v3.json` 版本一致、模块可编译。
- 更新两个插件 README 与 `package.v3.json` 版本历史。

## Acceptance Criteria

- [ ] 树中存在 `/A/B/C/D/a.mkv`、输出根 `/out` 时，全量同步后生成
      `/out/A/B/C/D/a.strm`，内容为 `https://www.googleapis.com/drive/v3/files/<a的fileId>?alt=media&supportsAllDrives=true`。
- [ ] 同一目录下 `a.srt`、`poster.jpg` 不生成 STRM。
- [ ] 树的 `root_path` 为 `/GDrive/` 时，输出不包含 `GDrive` 这一层。
- [ ] 全量同步重复执行不改变已一致文件的内容与修改时间。
- [ ] GDriveTree 刷新新增视频后，本插件在秒级内生成对应 STRM，无需手动操作。
- [ ] 事件中携带删除节点且清理开关开启时，对应 STRM 被删除；开关关闭时保留。
- [ ] 定时同步能补齐模拟丢失的 STRM，并更新变化的 URL。
- [ ] 插件停用后不再写文件；源目录树插件未启用/无数据时状态显示可读错误且不清空已有文件。
- [ ] `python -m pytest tests/v3/gdrivestrm tests/v3/gdrivetree` 通过。
- [ ] `python -m py_compile` 通过，`package.v3.json` 可解析且版本/history 一致。

## Out of Scope

- 不代理/转码 Google Drive 视频流，不生成 `.nfo`、封面、字幕文件。
- 不修改 Google Drive 内容，不调用 Drive 写接口。
- 不实现 WebDAV/挂载；STRM 内只写直链。
- 不做多树/多输出目录并行（首版单源单目标）。
- 不做 Windows 非法字符文件名转换（输出目录按 Linux 路径语义处理）。
