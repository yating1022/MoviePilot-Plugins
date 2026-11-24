# Scenario: STRM 内容模式与本地路径前缀

### 1. Scope / Trigger

适用于 `GDriveStrm` 以及任何根据「目录树 + 输出根」生成 `.strm` 的插件：

- 新增/修改 `.strm` 内容格式（直链、本地路径或未来协议）；
- 新增影响内容字符串的配置项；
- 排查「切换配置后旧 STRM 不更新」类问题。

### 2. Signatures

```python
# plugins.v3/gdrivestrm/strm_mapper.py
CONTENT_MODE_URL = "url"
CONTENT_MODE_PATH = "path"
DEFAULT_CONTENT_MODE = CONTENT_MODE_URL
DEFAULT_PATH_PREFIX = ""

join_path_prefix(prefix: Any, relative: Any) -> str

build_target(
    tree_file: TreeFile,
    root_path: Any,
    extensions: FrozenSet[str],
    url_template: str,
    *,
    content_mode: str = DEFAULT_CONTENT_MODE,
    path_prefix: str = DEFAULT_PATH_PREFIX,
) -> Optional[StrmTarget]

# plugins.v3/gdrivestrm/strm_syncer.py
StrmSyncer.apply_changes(
    added, updated, removed, *,
    root_path, extensions, url_template, records,
    content_mode=DEFAULT_CONTENT_MODE,
    path_prefix=DEFAULT_PATH_PREFIX,
) -> SyncOutcome
```

配置键：

| 键 | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `content_mode` | str | `url` | 仅 `path` 被识别为路径模式，其他值回退 `url` |
| `path_prefix` | str | 空 | 路径模式要求非空；原样保存，拼接时归一化 |

### 3. Contracts

- 内容统一存放在 `StrmTarget.url` / `StrmRecord.url`（沿用 `strm_record.url` 列），
  URL 与本地路径共用，不做数据库迁移。
- `.strm` 文件落盘路径始终是
  `strip_root_path(node.path, tree_meta.root_path)` 去掉扩展名后的相对路径，
  与内容模式无关。
- 路径模式内容 = `join_path_prefix(path_prefix, relative)`：
  - `\` → `/`，合并重复 `/`，去掉前缀末尾 `/`；
  - 空前缀或 `/` 得到 `/` + relative；
  - 非空前缀原样拼接（允许 `D:/Media`），不强制补前导 `/`；
  - 不包含 `root_path` 这一虚拟根层。
- 路径模式忽略 `url_template` 与 `shortcut_target_id`；
  快捷方式使用节点自身树路径。
- 切换模式或前缀后不需要清记录：同步器按内容字符串比较，
  不一致即重写旧 `.strm`。

### 4. Validation & Error Matrix

| 条件 | 行为 |
| --- | --- |
| `content_mode=path` 且 `path_prefix` 为空 | 同步中止，meta `failed`，错误含「路径前缀」，不写不删 |
| `content_mode=url` 且模板缺 `{fileId}` | 同步中止，meta `failed`，不写不删 |
| 相对路径含 `..` / 绝对路径 / NUL | 该文件跳过并记 error |
| 前缀含反斜杠或重复斜杠 | 归一化后写入内容 |
| 模式/前缀在同步中途变化 | 以作业开始时的实例配置为准，下次同步校正 |

### 5. Good / Base / Bad Cases

- Good：树 `/A/B/C/a.mkv`，前缀 `/home/googleDevice`，
  内容 `/home/googleDevice/A/B/C/a.mkv`，文件落盘 `A/B/C/a.strm`。
- Base：`content_mode=url`（默认），内容为模板渲染后的直链，行为与旧版一致。
- Bad：路径模式填写空前缀仍继续同步，写出 `/A/B/C/a.mkv` 这类看似有效、
  实际与挂载点无关的内容。

### 6. Tests Required

- `tests/v3/gdrivestrm/test_gdrivestrm_mapper.py`
  - 前缀归一化表（尾斜杠、`/`、空、反斜杠、`D:/Media`）；
  - 路径模式忽略模板与快捷方式目标 ID；
  - `root_path` 裁剪后内容不含虚拟根。
- `tests/v3/gdrivestrm/test_gdrivestrm_syncer.py`
  - `apply_changes` 路径模式写入内容；
  - 旧直链内容被重写为路径。
- `tests/v3/gdrivestrm/test_gdrivestrm_runtime.py`
  - 全量同步写出预期路径内容；
  - 空前缀导致 meta `failed` 且无文件；
  - URL → 路径切换后内容重写。

### 7. Wrong vs Correct

#### Wrong

```python
# 直接把树完整路径拼前缀，会把虚拟根也带进内容
content = prefix + tree_file.path          # /home/gd/GDrive/A/B/C/a.mkv
```

```python
# 路径模式继续校验 {fileId}，导致合法配置无法同步
if "{fileId}" not in self._url_template:
    abort()
```

#### Correct

```python
relative = strip_root_path(tree_file.path, root_path)
content = join_path_prefix(path_prefix, relative)
```

```python
if self._content_mode == CONTENT_MODE_PATH:
    if not self._path_prefix:
        abort("本地路径模式需要配置路径前缀")
elif "{fileId}" not in self._url_template:
    abort("模板缺少 {fileId}")
```
