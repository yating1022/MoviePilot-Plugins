# 实施计划：归档入树名称容错匹配

## 前置

- `python ./.trellis/scripts/task.py start` 后再改代码。
- 事实来源：`_ensure_nodes_for_path`（`__init__.py:1147`）、
  `find_child`（`gdrive_tree_client.py:326`）、`_node_from_child_meta`（`__init__.py:1225`）。

## 步骤

### 1. 归一化纯模块

- [ ] 新增 `plugins.v3/gdrivetree/name_match.py`：
  - `MATCH_EXACT` / `MATCH_NORMALIZED` / `MATCH_NO_SPACE`；
  - `normalize_name(name, level)`；
  - `NameMatch` dataclass（`level`、`item`、`ambiguous`）；
  - `match_child(candidates, wanted)`：按层级从小到大，取唯一命中。
- [ ] 只依赖标准库 `unicodedata`、`typing`。

### 2. 客户端列举子项

- [ ] `GDriveTreeClient.list_children(parent_id)`：
  - `q` 只用 `'<id>' in parents and trashed = false`（不按名称过滤）；
  - `pageSize=1000`、`fields=files(FILE_FIELDS)`、`corpora=allDrives`；
  - 最多 5 页，返回 `List[Dict]`。

### 3. 插件回退逻辑

- [ ] `_ensure_nodes_for_path` 中 `find_child` 返回 None 时：
  - 调 `list_children` → `match_child`；
  - 命中写 INFO 日志（层级 + 历史名 → 云端名）；
  - 歧义或无命中抛错，错误信息附候选名（≤5）；
  - 列举失败在错误信息中追加原因。
- [ ] 新增私有助手 `_child_names(candidates, limit)` 与
      `_not_found_message(anchor_path, segment, match, candidates)`，
      避免超长字符串拼接散落在主流程。
- [ ] `plugin_version` 升级并同步 `package.v3.json`（+ history）。

### 4. 测试

- [ ] `tests/v3/gdrivetree/test_name_match.py`：
  - 全角/半角（`，` vs `,`、`。` vs `.`）；
  - 大小写差异；
  - 首尾空白与连续空格；
  - 零宽字符；
  - 同层两个候选 → `ambiguous`；
  - 完全一致 → `MATCH_EXACT`。
- [ ] `tests/v3/gdrivetree/test_gdrive_tree_client.py`：
  - `list_children` 请求参数（`q` 不含 `name =`）；
  - 分页令牌跟随（两页）。
- [ ] `tests/v3/gdrivetree/test_plugin_runtime.py`：
  - 云端名全角、归档路径半角 → 入树成功，`tree_node.name` 为云端名；
  - 空白差异 → 入树成功；
  - 歧义 → 抛错，`archive_link` 记 `failed`，错误含两个候选；
  - 精确命中时替身 `list_children` 调用次数为 0。

### 5. 验证

```bash
python -m pytest tests/v3/gdrivetree -q
python -m pytest tests/v3 -q
python -m py_compile plugins.v3/gdrivetree/__init__.py \
  plugins.v3/gdrivetree/gdrive_tree_client.py \
  plugins.v3/gdrivetree/name_match.py
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
```

## 风险与回滚

| 文件 | 风险 | 控制 |
| --- | --- | --- |
| `__init__.py` | 归档主流程回归 | 仅在 None 分支加逻辑；精确命中零改动 |
| `gdrive_tree_client.py` | 新增请求可能触发限流 | 仅在精确查询失败时调用，最多 5 页 |
| `name_match.py` | 归一化过度导致误配 | 同层多候选一律报错，不做相似度 |

回滚：删除 None 分支的回退代码即恢复原行为；`name_match.py` 可整体移除。
