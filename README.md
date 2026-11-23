# MoviePilot-Plugin-Market

MoviePilot 三方插件仓库（仅支持 V3）：https://github.com/yating1022/MoviePilot-Plugins

作者：yating1022

## 插件列表

### [Google Drive 存储（GDriveStorage）](plugins.v3/gdrivestorage/README.md)

把 Google Drive 接入 MoviePilot V3 存储体系的自定义存储插件，基于 Google Drive API v3 与 OAuth 2.0，不依赖额外的 Google SDK。

- 在系统存储中注册 `gdrive` 类型，可在目录配置中选择 “Google Drive”。
- 作为媒体库/目标存储参与整理：支持服务端移动（move）与复制（copy）。
- 支持浏览、创建目录、上传、下载、重命名、删除和容量查询。
- 支持 OAuth2 授权码流程、refresh_token 自动刷新、团队盘快捷方式和共享盘列举。
- 详情页显示授权、注册、容量状态和最近操作日志。

| 项目 | 值 |
| --- | --- |
| 插件 ID | `GDriveStorage` |
| 版本 | 1.1.3 |
| 标签 | 网盘 |
| 系统要求 | MoviePilot >= 3.0.0 |

### [115 目录秒传上传（U115AutoUpload）](plugins.v3/u115autoupload/README.md)

实时监控本地目录，发现新文件后先多次探测 115 秒传，全部未命中后再走普通上传。

- 复用主程序“设置 → 存储”里已登录的 115 授权，插件内无需再填账号。
- `watchdog` 实时监控 + 定时扫描双保险，网络盘收不到事件时也能兜底。
- 串行上传队列，定时扫描只负责入队，不会打断正在进行的上传。
- 上传过程输出进度、平均速度和耗时心跳，详情页可随时刷新。
- 插件自有数据库记录已处理文件，支持列表、重试、删除记录和清理成功记录。
- 上传结果通过主程序通知渠道发送。

| 项目 | 值 |
| --- | --- |
| 插件 ID | `U115AutoUpload` |
| 版本 | 1.3.1 |
| 标签 | 网盘 |
| 系统要求 | MoviePilot >= 3.0.0 |

### [Google Drive 目录树（GDriveTree）](plugins.v3/gdrivetree/README.md)

复用 Google Drive 存储插件的授权配置，一次性分页拉取文件元数据并在本地按根目录裁剪建树。

- 目录树持久化到插件自有数据库，支持手动与定时刷新、增量写库、分页浏览和搜索。
- 监听整理完成事件并定时对比归档记录，把新归档到 Google Drive 的文件增量加入目录树。
- 可把刷新、归档和清空产生的变化以 `tree.changed` 定向事件通知 STRM 插件。

| 项目 | 值 |
| --- | --- |
| 插件 ID | `GDriveTree` |
| 版本 | 1.2.4 |
| 标签 | 网盘 |
| 系统要求 | MoviePilot >= 3.0.0 |

### [谷歌 STRM 管理（GDriveStrm）](plugins.v3/gdrivestrm/README.md)

读取 Google Drive 目录树，把视频文件按原父子结构映射为本地 `.strm`。

- `.strm` 内容为 Google Drive 文件直链，文件快捷方式使用目标文件 ID。
- 支持目录树事件秒级增量、定时对账、手动全量同步。
- 默认只创建视频所在目录链；失效 STRM 只清理本插件记录过的文件。
- 幂等写入，内容一致不改变文件修改时间。

| 项目 | 值 |
| --- | --- |
| 插件 ID | `GDriveStrm` |
| 版本 | 1.0.1 |
| 标签 | 网盘 |
| 系统要求 | MoviePilot >= 3.0.0 |

## 安装
1. 打开 MoviePilot → 设置 → 插件 → 插件市场 → 添加仓库，填入：

   ```text
   https://github.com/yating1022/MoviePilot-Plugins
   ```

2. 刷新插件列表，安装需要的插件并启用。

3. 以上插件均只支持 MoviePilot V3（`system_version >= 3.0.0`），详细配置请查看各插件目录下的 `README.md`。

## 反馈

问题与建议请提交到：https://github.com/yating1022/MoviePilot-Plugins/issues

## 许可

本项目基于 [GPL-3.0](LICENSE) 许可发布。
