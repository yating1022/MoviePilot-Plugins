# GDriveStrm 支持本地路径内容与前缀

## Goal

让「谷歌 STRM 管理」（GDriveStrm）在生成 `.strm` 时可以选择写入**本地挂载路径**
而不是 Google Drive 直链，并支持在配置页配置一个路径前缀。

示例：目录树中文件路径为 `/A/B/C/a.mkv`，前缀配置为 `/home/googleDevice`，
则生成的 `.strm` 内容为：

```
/home/googleDevice/A/B/C/a.mkv
```

默认仍使用现有 Google Drive 直链，升级后不改变已有用户行为。

## Background / Confirmed Facts

- `.strm` 内容当前由 `url_template` 渲染：`strm_mapper.render_url()`，
  模板必须包含 `{fileId}`（`plugins.v3/gdrivestrm/strm_mapper.py:165`）。
- 每个目标的内容保存在 `StrmTarget.url`，落库到 `strm_record.url`
  （`plugins.v3/gdrivestrm/__init__.py:65`）。
- 同步器只比较内容字符串，内容不同即重写，因此切换内容模式后旧文件会被自动校正
  （`plugins.v3/gdrivestrm/strm_syncer.py:240`）。
- 目录树节点路径带 `tree_meta.root_path` 前缀；相对路径由
  `strip_root_path()` 得到，`.strm` 文件本身也按该相对路径落盘
  （`plugins.v3/gdrivestrm/strm_mapper.py:136`）。
- 配置页由 `get_form()` 返回 Vuetify 组件树，`VSelect` 已有使用先例
  （`plugins.v3/gdrivestorage/__init__.py:1940`）。

## Requirements

### R1 新增配置项

- 新增「STRM 内容模式」`content_mode`，可选：
  - `url`：Google Drive 直链（默认，保持现状）；
  - `path`：本地文件路径。
- 新增「本地路径前缀」`path_prefix`，文本，默认空。
- 两个字段都必须进入 `_normalize_config()`、`init_plugin()`、`get_form()` 默认值。
- 非 `path` 的取值一律按 `url` 处理，避免脏配置导致同步异常。

### R2 路径模式的内容生成

- 路径模式内容 = `path_prefix` 与「相对目录树根路径」拼接，保留源文件名和扩展名：
  - 相对路径取 `strip_root_path(node.path, tree_meta.root_path)`；
  - 内容示例：`/home/googleDevice` + `A/B/C/a.mkv` → `/home/googleDevice/A/B/C/a.mkv`。
- 前缀归一化：
  - 去掉首尾空白，`\` 转 `/`，合并重复 `/`，去掉末尾 `/`；
  - 前缀 `/` 等价于空前缀，内容为 `/A/B/C/a.mkv`；
  - 非空前缀原样拼接，不强制补前导 `/`（允许 `D:/Media` 这类挂载点）。
- 路径模式不要求 `url_template` 包含 `{fileId}`。
- 文件快捷方式在路径模式下使用快捷方式节点自身的树路径（即挂载后可见的路径），
  不再使用 `shortcut_target_id`。

### R3 兼容与同步行为

- 默认 `content_mode=url` 时行为与现在完全一致，包括模板校验。
- 路径模式且 `path_prefix` 为空时，同步应中止并写入可读错误，
  不写文件、不清理旧文件。
- 切换内容模式或前缀后，已有 `.strm` 内容与目标不一致，下次同步自动重写。
- 不新增数据库列、不做数据迁移；沿用 `StrmRecord.url` 与 `StrmTarget.url`
  字段保存内容。
- 同步失败、清理、幂等、事件增量等既有语义不变。

### R4 配置页与文档

- 配置页新增内容模式选择与前缀输入框，说明前缀只在路径模式生效，
  并给出示例。
- README 更新功能说明、配置表与常见问题。
- 插件版本升级并同步 `package.v3.json` 的 `version` 与 `history`。

### R5 测试

- 纯逻辑测试覆盖：路径拼接、前缀归一化、root_path 裁剪、快捷方式、
  非法路径、URL 模式回归。
- 同步器测试覆盖：路径模式新增/更新内容。
- 实例测试覆盖：路径模式完整同步写出预期内容；空前缀中止；
  URL 模式与现有测试保持通过。
- 契约测试通过（版本一致、API envelope 等）。

## Acceptance Criteria

- [ ] `content_mode=path`、`path_prefix=/home/googleDevice`，树文件
      `/A/B/C/a.mkv`，全量同步后 `.strm` 内容为
      `/home/googleDevice/A/B/C/a.mkv`。
- [ ] 树 `root_path=/GDrive/`、节点 `/GDrive/Movies/a.mkv`，输出仍为
      `Movies/a.strm`，内容为 `<prefix>/Movies/a.mkv`（不包含 `GDrive` 层）。
- [ ] 前缀 `/home/googleDevice/` 与 `/home/googleDevice` 结果一致；
      前缀 `/` 得到 `/A/B/C/a.mkv`。
- [ ] `content_mode=path` 且前缀为空时同步状态为失败，错误信息提示配置前缀，
      且不新增或删除任何 `.strm`。
- [ ] `content_mode=url` 时所有既有测试通过，缺少 `{fileId}` 仍中止同步。
- [ ] 从 URL 模式切换到路径模式后再次同步，旧直链内容被重写为路径内容。
- [ ] `python -m pytest tests/v3/gdrivestrm -q` 通过。
- [ ] `python -m py_compile plugins.v3/gdrivestrm/*.py` 通过，
      `package.v3.json` 可解析且插件版本与 history 一致。

## Out of Scope

- 不自动探测宿主机挂载点，前缀完全由用户配置。
- 不支持按目录配置不同前缀（单插件单前缀）。
- 不校验目标路径在媒体服务器上是否真实存在。
- 不管理 rclone/Alist 等挂载工具，也不生成 `.nfo`、封面、字幕。
- 不改动 GDriveTree 插件与目录树数据结构。
