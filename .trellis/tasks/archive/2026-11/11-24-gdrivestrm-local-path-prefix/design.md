# 技术设计：STRM 本地路径内容与前缀

## 1. 边界与不变量

- 改动范围：`plugins.v3/gdrivestrm/`（`strm_mapper.py`、`strm_syncer.py`、
  `__init__.py`）、`tests/v3/gdrivestrm/`、插件 README、`package.v3.json`。
- 不变量：
  - `.strm` 文件的落盘相对路径只由 `strip_root_path()` 决定，与内容模式无关；
  - `StrmTarget.url` / `StrmRecord.url` 继续作为「内容」字段，URL 和本地路径共用，
    避免数据库迁移；
  - 既有 URL 模式行为与配置默认值不变。

## 2. 内容生成契约

### 2.1 模式常量

```python
CONTENT_MODE_URL = "url"
CONTENT_MODE_PATH = "path"
DEFAULT_CONTENT_MODE = CONTENT_MODE_URL
DEFAULT_PATH_PREFIX = ""
```

放在 `strm_mapper.py`，供插件与测试共享。

### 2.2 前缀拼接

```python
def join_path_prefix(prefix: Any, relative: Any) -> str:
    text = str(prefix or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.rstrip("/")
    rel = str(relative or "").strip().lstrip("/")
    if not text:
        return "/" + rel
    return f"{text}/{rel}"
```

| 前缀输入 | 相对路径 | 输出 |
| --- | --- | --- |
| `/home/googleDevice` | `A/B/C/a.mkv` | `/home/googleDevice/A/B/C/a.mkv` |
| `/home/googleDevice/` | `A/B/C/a.mkv` | `/home/googleDevice/A/B/C/a.mkv` |
| `/` | `A/B/C/a.mkv` | `/A/B/C/a.mkv` |
| `D:/Media` | `A/B/C/a.mkv` | `D:/Media/A/B/C/a.mkv` |
| 空 | `A/B/C/a.mkv` | `/A/B/C/a.mkv` |

> 空前缀在 mapper 层按 `/` 处理；插件层在路径模式下会提前拒绝空前缀，
> 避免用户误以为配置生效。

### 2.3 build_target 签名

保持位置参数兼容，新增关键字参数：

```python
def build_target(
    tree_file: TreeFile,
    root_path: Any,
    extensions: FrozenSet[str],
    url_template: str,
    *,
    content_mode: str = CONTENT_MODE_URL,
    path_prefix: str = "",
) -> Optional[StrmTarget]:
```

流程：

1. 校验视频后缀、相对路径与安全检查（不变）。
2. `content_mode == "path"`：
   - `content = join_path_prefix(path_prefix, relative)`；
3. 否则：
   - `identifier = shortcut_target_id or file_id`；
   - `content = render_url(url_template, identifier)`；为空则返回 `None`。
4. `StrmTarget.url` 保存 `content`。

`build_targets()` 透传两个新关键字参数。

## 3. 同步器改动

`StrmSyncer.apply_changes()` 增加 `content_mode` 与 `path_prefix` 关键字参数，
并透传给 `build_target()`。`full_sync()` 不直接构建目标，无需改动。

内容比较逻辑不变：`record.url != target.url` 即重写。
因此切换 URL/路径模式或修改前缀后，下一次全量/增量同步会自动校正内容。

## 4. 插件编排改动

### 4.1 配置归一化

```python
"content_mode": (
    CONTENT_MODE_PATH
    if str(cfg.get("content_mode") or "").strip() == CONTENT_MODE_PATH
    else CONTENT_MODE_URL
),
"path_prefix": str(cfg.get("path_prefix") or "").strip(),
```

实例字段：`self._content_mode`、`self._path_prefix`。

### 4.2 同步前校验

`_execute_locked()` 中按模式校验：

```python
if self._content_mode == CONTENT_MODE_PATH:
    if not self._path_prefix:
        # 写 meta failed：本地路径模式需要配置路径前缀
        return
elif "{fileId}" not in self._url_template:
    # 保持现有失败逻辑
    return
```

### 4.3 表单

在「STRM 内容模板」上方新增一行：

- `VSelect model=content_mode`，items：
  `Google 直链 / url`、`本地文件路径 / path`；
- `VTextField model=path_prefix`，placeholder `/home/googleDevice`，
  hint 说明「仅本地路径模式生效；与目录树根下的相对路径拼接」；
- 顶部 VAlert 文案补充两种内容模式说明。

### 4.4 日志

插件启用与同步开始时输出内容模式；路径模式输出前缀，便于排查。
不要把完整路径内容逐条打印到日志以外的地方（沿用现有逐条新增日志即可）。

## 5. 失败与错误矩阵

| 条件 | 行为 |
| --- | --- |
| 路径模式 + 空前缀 | meta `failed`，错误「本地路径模式需要配置路径前缀」，不写不删 |
| URL 模式 + 模板缺 `{fileId}` | 保持现有：meta `failed`，不写不删 |
| 相对路径非法（`..`、绝对、NUL） | 该文件跳过并记 error，与现状一致 |
| 前缀含反斜杠 | 归一化为 `/` |
| 前缀为 `/` | 内容为 `/` + 相对路径 |
| 同步中途内容模式变化 | 以后台作业开始时的配置为准；下次同步自然校正 |

## 6. 兼容性与迁移

- `content_mode` 缺省为 `url`，旧配置无感升级。
- 新增配置键只需 `_normalize_config()` 兜底，不需要宿主侧迁移。
- `strm_record` 表结构不变；`url` 列可存直链或本地路径。
- 回滚：把 `content_mode` 改回 `url` 并同步一次即可恢复直链内容。

## 7. 测试矩阵

| 层级 | 用例 |
| --- | --- |
| mapper | 路径拼接、前缀归一化、root_path 裁剪、路径模式忽略快捷方式目标 ID、非法路径、URL 模式回归 |
| syncer | `apply_changes` 路径模式新增与更新内容 |
| runtime | 路径模式完整同步内容正确；空前缀失败且不动文件；切换模式重写旧文件 |
| contract | 版本与 `package.v3.json` 一致、API envelope 三键 |

## 8. 风险

| 风险 | 控制 |
| --- | --- |
| 用户误配空前缀导致内容为 `/A/...` | 插件层校验空前缀并中止同步 |
| `url` 字段名与语义不符 | 在字段注释/文档中说明它是「内容」；不迁移表结构 |
| Windows 挂载点前缀被误改 | 前缀只做反斜杠转斜杠和去尾斜杠，不强制前导 `/` |
| 切换模式后大量重写 | 属预期；幂等逻辑保证内容一致时不重写 |
