# Scenario: 归档入树的名称匹配

### 1. Scope / Trigger

适用于把整理历史（`TransferComplete` / `TransferHistory`）中的路径映射回 Google Drive
目录树的代码：

- 新增/修改 `_ensure_nodes_for_path`、`_nodes_for_target`、`find_child`、`list_children`；
- 出现「云端未找到路径段」「云端未找到目标文件」；
- 归档路径来自本地命名模板，可能被规范化过。

### 2. Signatures

```python
# plugins.v3/gdrivetree/gdrive_tree_client.py
find_child(parent_id: str, name: str) -> Optional[Dict[str, Any]]
list_children(parent_id: str, *, max_pages: int = 5,
              stop_event: Optional[Any] = None) -> List[Dict[str, Any]]

# plugins.v3/gdrivetree/name_match.py
MATCH_EXACT = 0
MATCH_NORMALIZED = 1
MATCH_NO_SPACE = 2
normalize_name(name: Any, level: int = MATCH_NORMALIZED) -> str
match_child(candidates: Sequence[Any], wanted: Any) -> NameMatch
candidate_names(candidates: Sequence[Any], limit: int = 5) -> Tuple[str, ...]

# plugins.v3/gdrivetree/__init__.py
_find_child_meta(client, anchor, name) -> Tuple[Optional[Dict[str, Any]], str]
```

### 3. Contracts

- `find_child` 使用 Drive `name = '<name>'`，是**字符级精确匹配**；只转义 `\` 和 `'`。
- 精确失败时 `_find_child_meta` 调用 `list_children`（只按父目录过滤，不按名称），
  再按以下层级做等价匹配，任一层**唯一命中**即采用：
  1. `MATCH_EXACT`：原样字符串相等；
  2. `MATCH_NORMALIZED`：NFKC + CJK 标点映射（`。`→`.`、`、`→`,`）
     + `casefold` + 去零宽字符 + 首尾裁剪 + 内部连续空白折叠为单空格；
  3. `MATCH_NO_SPACE`：第 2 层再删除全部空白。
- 同一层出现多个命中 → 歧义，**不选择**，抛错并附候选名（最多 5 个）。
- 写入 `tree_node.name` 的值始终来自云端元数据 `meta["name"]`，不是归档路径。
- 精确命中时不调用 `list_children`（零额外请求）。
- `GDriveTreeCancelled` 必须直接上抛，不能被降级为「未找到」。

### 4. Validation & Error Matrix

| 条件 | 行为 |
| --- | --- |
| 精确命中 | 直接返回，不列举子项 |
| 容错唯一命中 | INFO 日志「历史名 → 云端名」，按云端名入树 |
| 同层多候选 | 抛错 `云端未找到路径段…；名称匹配存在歧义，候选：a、b` |
| 未命中但有其它子项 | 抛错并附最多 5 个候选名 |
| 未命中且目录为空 | 抛错，不附候选 |
| `list_children` 失败 | 抛错并附 `（列举子项失败：...）` |
| 列举超过 `max_pages` 页 | 抛错 `子项超过 N 页，已中止列举` |
| 取消信号 | `GDriveTreeCancelled` 上抛，归档记录保持原状态 |

### 5. Good / Base / Bad Cases

- Good：云端 `明天，美食广场见。.2025.S01E02.第2集.mkv`，归档路径
  `明天,美食广场见..2025.S01E02.第2集.mkv` → 层级 1 命中，写入云端名。
- Base：两边完全一致 → 层级 0，不触发列举。
- Bad：把 `第1集` 与 `第2集` 用编辑距离模糊匹配 → 可能把错文件写进树，
  STRM 会跟着生成错误目标。必须不做相似度。

### 6. Tests Required

- `tests/v3/gdrivetree/test_name_match.py`
  - 全角标点、大小写、空白数量、零宽字符；
  - `第1集` ≠ `第2集`；全角数字可折叠；
  - 同层歧义返回候选；完全同名重复也视为歧义。
- `tests/v3/gdrivetree/test_gdrive_tree_client.py`
  - `list_children` 的 `q` 不含 `name =`，`pageSize=1000`，分页令牌跟随；
  - 分页令牌重复时中止；超过页数上限报错。
- `tests/v3/gdrivetree/test_plugin_runtime.py`
  - 全角/空格差异能入树且名字为云端名；
  - 精确命中时 `list_children` 调用次数为 0；
  - 歧义与无候选时 `ArchiveLink.error` 含候选名。

### 7. Wrong vs Correct

#### Wrong

```python
# 精确查询失败就直接放弃；或者用相似度猜一个
child = client.find_child(parent_id, segment)
if child is None:
    raise GDriveTreeError("云端未找到路径段")
```

#### Correct

```python
child, detail = self._find_child_meta(client, anchor, segment)
if child is None:
    raise GDriveTreeError(f"云端未找到路径段：{anchor.path}{segment}{detail}")
```

```python
# 归一化只做等价类，不做相似度；多候选必须报错
match = match_child(candidates, name)
if match.item is None:
    return None, "；名称匹配存在歧义，候选：" + "、".join(match.ambiguous[:5])
```
