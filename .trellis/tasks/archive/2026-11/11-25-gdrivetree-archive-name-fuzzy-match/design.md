# 技术设计：归档入树名称容错匹配

## 1. 边界

- 改动文件：
  - `plugins.v3/gdrivetree/gdrive_tree_client.py`（新增 `list_children`）
  - `plugins.v3/gdrivetree/name_match.py`（新模块，纯逻辑，无宿主依赖）
  - `plugins.v3/gdrivetree/__init__.py`（`_ensure_nodes_for_path` 回退逻辑）
  - `tests/v3/gdrivetree/`（新增测试）
- 不改：`tree_builder.py`、`tree_events.py`、`archive_source.py`、数据库表结构。

## 2. 归一化契约（新模块 `name_match.py`）

```python
MATCH_EXACT = 0          # 原样相等
MATCH_NORMALIZED = 1     # NFKC + casefold + 去零宽 + 去首尾空白
MATCH_NO_SPACE = 2       # 上者再删除所有空白

def normalize_name(name: Any, level: int) -> str: ...
def match_child(candidates: Sequence[Mapping], wanted: str) -> NameMatch: ...

@dataclass(frozen=True)
class NameMatch:
    level: int
    item: Optional[Dict[str, Any]]      # 唯一命中项
    ambiguous: Tuple[str, ...]          # 歧义候选名
```

归一化规则（第 1 层起）：

1. `unicodedata.normalize("NFKC", text)` —— 全角转半角，兼容字符折叠；
2. CJK 标点映射（NFKC 不处理）：`。` U+3002 → `.`，`、` U+3001 → `,`；
   半角 `｡`/`､` 已被 NFKC 折叠为上述 CJK 标点，无需单列；
3. `casefold()` —— 大小写无关（Drive 名称本身大小写敏感，但归档模板常改大小写）；
4. 删除零宽字符 `​ ‌ ‍ ‎ ‏ ⁠ ﻿ ­`；
5. `" ".join(text.split())` —— 首尾裁剪，并把内部连续空白折叠为单空格；
6. 第 2 层额外 `text.replace(" ", "")` 删除全部空白。

刻意不做的等价：`・`(U+30FB) 不映射为 `.`，避免把语义不同的日文分隔符
误当成路径分隔差异；`第1集` 与 `第2集` 永不相等（无相似度匹配）。

只有 `level` 最小且**唯一命中**的层级被采用；同层多命中 → `ambiguous`。

## 3. 客户端新增方法

```python
class GDriveTreeClient:
    def list_children(self, parent_id: str) -> List[Dict[str, Any]]:
        """列出目录直接子项（最多 1000 条，用于名称容错匹配）。"""
```

- 请求：`GET files`，`q="'<parent>' in parents and trashed = false"`，
  `fields=files(FILE_FIELDS)`，`pageSize=1000`，`corpora=allDrives`，
  `supportsAllDrives=true`，`includeItemsFromAllDrives=true`。
- 单页即可；若返回 `nextPageToken`，继续列举（上限 5 页，避免异常目录拖慢）。
- 失败抛 `GDriveTreeError`，由调用方降级为「无候选」。
- 该请求与 `find_child` 共用 `_escape_query` 与错误处理。

## 4. 解析流程改动

```python
meta, detail = self._find_child_meta(client, anchor, segment)
if meta is None:
    raise GDriveTreeError(f"云端未找到路径段：{anchor.path}{segment}{detail}")
```

`_find_child_meta()` 返回 `(云端元数据, 失败说明)`：

- 精确命中：`(meta, "")`，不调用 `list_children`；
- 容错唯一命中：`(meta, "")`，并输出 INFO 日志 `历史名 -> 云端名`；
- 歧义：`(None, "；名称匹配存在歧义，候选：a、b")`；
- 未命中但有子项：`(None, "；候选：a、b、c")`（最多 5 个）；
- 列举失败：`(None, "（列举子项失败：...）")`；
- 取消信号 `GDriveTreeCancelled` 直接上抛，不降级为「未找到」。

- 后续 `_node_from_child_meta(anchor, child, segment)` 保持不变；
  它内部 `name = str(meta.get("name") or fallback_name)`，
  因此写入树的名字天然是云端真实名称。
- 精确命中路径零额外请求。

## 5. 失败矩阵

| 条件 | 行为 |
| --- | --- |
| 精确查询命中 | 与现状一致，不调用 `list_children` |
| 精确失败 + 容错唯一命中 | INFO 日志，使用云端名称入树 |
| 精确失败 + 同层多候选 | 抛错，错误含候选名，记录 `failed` 并计入重试 |
| 精确失败 + 无候选 | 抛错，错误含最多 5 个候选名（若有其它子项） |
| `list_children` 抛错 | 抛错，错误追加列举失败原因 |
| 子项超过 5 页 | 中止列举，按无候选处理并提示 |

## 6. 为什么不用相似度

编辑距离会把 `第1集` 与 `第2集` 判为接近，归档场景下可能把 A 文件当 B 文件入树，
污染目录树并联动 STRM。只做等价类归一化，多候选直接报错交给人工。

## 7. 测试计划

| 层级 | 用例 |
| --- | --- |
| `test_name_match.py` | 四类差异各一例；同层歧义；零宽字符；层级优先级 |
| `test_gdrive_tree_client.py` | `list_children` 请求参数与分页 |
| `test_plugin_runtime.py` | 全角差异入树成功且名字为云端名；歧义报错含候选；精确命中不列举 |

## 8. 回滚

- 新逻辑集中在 `find_child` 返回 None 的分支；恢复为直接抛错即可回滚。
- `name_match.py` 为新增模块，删除不影响其它代码。
