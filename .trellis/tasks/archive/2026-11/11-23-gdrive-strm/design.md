# Google STRM 管理 — 技术设计

## 1. 架构与边界

```
GDriveTree (plugins.v3/gdrivetree)
  ├─ tree_node / tree_meta（插件自有库，目录树快照）
  └─ 变更后定向事件 EventType.PluginTriggered
        payload: {plugin_id, event_name: "tree.changed", data: {...}}
        + __mp_target_plugin_id = <配置的 STRM 插件 ID>
              │
              ▼
GDriveStrm (plugins.v3/gdrivestrm)
  ├─ 事件处理器：过滤来源与 event_name，投递到后台工作线程
  ├─ 定时服务：按间隔入队增量对账
  ├─ 工作线程：合并事件 + 互斥执行同步
  ├─ tree_source：只读访问 GDriveTree 自有库（get_database(plugin_id)）
  ├─ strm_mapper（纯逻辑）：路径映射 / 后缀过滤 / URL 渲染
  ├─ strm_syncer：写文件、删失效、原子替换、清理空目录
  └─ strm_record / strm_meta（插件自有库）
```

设计原则：

- **事件只做失效通知与增量提示**，不携带整棵树；大变更退化为全量信号，由本插件读库对账。
- **目录树数据库是唯一事实来源**；STRM 侧记录只用于差量、清理与幂等，不作为树数据来源。
- **所有落盘由一个后台线程串行执行**，事件、定时、手动按钮都只入队，避免并发写同一路径。
- GDriveTree 的通知是**可选、失败隔离**的：通知失败不影响刷新结果。

## 2. 文件布局

```text
plugins.v3/gdrivestrm/
  __init__.py         # GDriveStrm 类、DB 模型、配置表单、API、页面、工作线程
  strm_mapper.py      # 纯函数：后缀、路径、URL、目标集合
  tree_source.py      # 跨插件只读读取 tree_meta / tree_node
  strm_syncer.py      # 文件系统同步引擎（可注入记录/文件系统，便于单测）
  README.md

plugins.v3/gdrivetree/
  tree_events.py      # 新增：纯函数构建 tree.changed 事件载荷
  __init__.py         # 新增配置、变更收集、事件发布
  README.md

tests/v3/gdrivestrm/
  _plugin_loader.py
  test_strm_mapper.py
  test_strm_syncer.py
  test_plugin_runtime.py
  test_plugin_contract.py
tests/v3/gdrivetree/
  test_tree_events.py
  test_plugin_runtime.py（补充事件发布断言）
```

## 3. 数据模型（GDriveStrm 自有库）

```python
class StrmRecord(_PluginDBBase):
    __tablename__ = "strm_record"
    id: int                 # PK
    rel_path: str           # 相对输出根的 POSIX 路径，唯一索引
    source_file_id: str     # Drive 文件 ID（快捷方式为展示节点 ID）
    source_path: str        # 树中完整路径
    url: str                # 写入内容
    size: int
    modified_time: float
    updated_at: datetime

class StrmMeta(_PluginDBBase):
    __tablename__ = "strm_meta"   # 固定单行 id=1
    tree_plugin_id, output_dir, status
    last_started_at, last_finished_at, last_success_at, last_event_at
    target_count, record_count, added_count, updated_count,
    removed_count, skipped_count, error, updated_at
```

- `strm_record.rel_path` 唯一约束；`source_file_id` 普通索引（删除/改名按 ID 定位）。
- 表由宿主按 `get_database_models()` 建立；不做迁移。
- `record_count` 包含清理关闭时保留的失效记录；`target_count` 为本次期望的 STRM 数。

## 4. 纯逻辑：路径与后缀

`strm_mapper.py`：

```python
DEFAULT_VIDEO_EXTENSIONS = (
    "mkv mp4 avi ts m2ts mts iso rmvb wmv mov flv webm mpg mpeg m4v vob "
    "3gp f4v asf divx ogm ogv tp trp m2v mxf"
)
VIDEO_MIME_HINTS = ("video/",)  # 仅用于日志/诊断，不替代后缀判断

parse_extensions(raw) -> frozenset[str]     # 逗号/空格/换行分隔，小写，去前导点
is_video_name(name, extensions) -> bool     # 大小写不敏感；始终排除 strm
strip_root_path(tree_path, root_path) -> str | None
build_rel_target(tree_file, root_path, extensions, url_template) -> StrmTarget | None
render_url(template, file_id) -> str        # 模板必须含 {fileId}
```

规则：

- `root_path` 归一化为以 `/` 开头结尾；`tree_path` 去前缀后得到相对路径。
- 相对路径按 POSIX 处理，拒绝空值、绝对路径、`..` 段、包含 NUL 的段。
- 目录节点（`is_dir=True`）直接忽略；根节点忽略。
- 输出相对路径：`<rel 去原后缀>.strm`，保留原文件名其余部分。
- `file_id` 优先取 `shortcut_target_id`（文件快捷方式），否则取 `file_id`。

`StrmTarget`（frozen dataclass）：`rel_path`, `source_file_id`, `source_path`,
`url`, `size`, `modified_time`。

## 5. 跨插件读取目录树

`tree_source.py`：

```python
class TreeSourceError(Exception): ...

@dataclass(frozen=True)
class TreeFile:
    file_id, name, path, size, modified_time, shortcut_target_id

def read_root_path(database) -> str
def iter_tree_files(database, stop_event=None) -> Iterator[TreeFile]
```

- 使用 `self.get_database(tree_plugin_id).session()` 新建会话。
- `SELECT root_path FROM tree_meta WHERE id = 1`；无行时抛
  `TreeSourceError("目录树尚未构建，请先在 GDriveTree 执行一次刷新")`。
- `SELECT file_id, name, path, size, modified_time, shortcut_target_id
   FROM tree_node WHERE is_dir = :is_dir`，参数 `False`；按需 `yield_per(2000)`。
- 表不存在或连接错误统一包装为可读中文错误；**不清空已有 STRM**。
- 会话结束关闭；不使用 ORM 映射其他插件的模型。

## 6. 同步引擎

`strm_syncer.py` 只依赖标准库，文件系统通过 `root: Path` 直接操作，
记录以 `dict[str, StrmRecordData]` 注入，便于单测。

```python
@dataclass(frozen=True)
class StrmRecordData:
    rel_path: str
    source_file_id: str
    source_path: str
    url: str
    size: int
    modified_time: float

@dataclass
class SyncOutcome:
    added: list[StrmRecordData]
    updated: list[StrmRecordData]
    removed: list[str]            # 实际删除/丢弃的 rel_path
    skipped: int
    errors: list[str]

class StrmSyncer:
    def __init__(self, root: Path, cleanup: bool, stop_event: threading.Event | None)
    def full_sync(targets, records) -> SyncOutcome         # 全量对账，逐文件校验内容
    def apply_changes(added, updated, removed_files,
                      root_path, extensions, url_template, records) -> SyncOutcome
```

### 6.1 全量同步（full_sync）

1. 遍历 `targets`（rel_path → StrmTarget）：
   - 目标文件不存在 → 写入；
   - 记录存在且 `url` 相同 → 跳过（不碰 mtime）；
   - 记录缺失但文件存在 → 读文件比较内容，一致则只补记录；
   - `url` 变化或内容不一致 → 原子重写。
2. 遍历 `records` 中不在 `targets` 的条目：
   - `cleanup=True` → 删除文件（仅 `.strm`）并产出 removed；
   - `cleanup=False` → 保留记录与文件，计入 skipped。
3. 对被删除路径的父目录自底向上 `rmdir`，遇到非空/根目录停止。
4. 单个文件失败记入 `errors` 并继续；停用信号在每个条目间检查。

### 6.2 增量事件（apply_changes）

- `added` / `updated` 节点：
  - 命中视频后缀 → 生成目标；
  - 按 `source_file_id` 找到旧记录；若旧 `rel_path` 不同（改名/移动）先删旧文件与旧记录；
  - 内容一致跳过，否则原子写入。
- 节点变为非视频后缀 → 视为删除（受 `cleanup` 控制）。
- `removed` 节点：按 `source_file_id` 查找记录；只有节点没有 ID 时才按源路径兜底，
  且不匹配带 ID 的记录。这样同一路径被新文件 ID 替换时，删除事件不会误删刚写入的新文件。
  `cleanup=True` 时删除文件与记录，否则保留。
- 增量事件不扫描整棵树，保证秒级落盘；记录按 `file_id` / `source_path` 建立索引，
  非视频更新只做索引命中，不做全表扫描。

### 6.3 写文件与安全

- 写入：`tmp = 目标目录/.<name>.tmp-<uuid>` → `tmp.write_text(url, "utf-8")` →
  `os.replace(tmp, target)`；失败删除临时文件。
- 目标路径解析后必须仍在输出根内（`Path.resolve().is_relative_to(root)`）。
- 只允许创建 `.strm` 文件；删除前校验后缀为 `.strm` 且位于根内。
- 输出根为空、`/`、或其父目录不可写 → 同步直接失败，不做任何删除。
- 文件名段拒绝 NUL 与 `/`；其他字符按 Linux 语义原样保留（PRD 已列为范围外）。

## 7. GDriveTree 侧改造

### 7.1 新增配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `strm_notify` | 关 | 目录树变化后通知 STRM 插件 |
| `strm_plugin_id` | `GDriveStrm` | 目标插件实例 ID（分身填分身 ID） |

`_normalize_config` / `get_form` / `__init__` / `init_plugin` 同步新增字段；
版本升到 `1.2.0`。

### 7.2 变更收集

`tree_events.py`（纯逻辑）：

```python
EVENT_NAME = "tree.changed"
MAX_EVENT_NODES = 2000

@dataclass(frozen=True)
class TreeChangeSet:
    kind: str                         # refresh | archive | clear
    added: tuple[dict, ...] = ()
    updated: tuple[dict, ...] = ()
    removed: tuple[dict, ...] = ()
    full: bool = False

def node_payload(node: TreeNodeData) -> dict
def removed_payload(file_id: str, path: str) -> dict
def build_payload(target_plugin_id: str, change: TreeChangeSet) -> dict
def payload_size(change) -> int
```

节点载荷字段：`file_id`, `path`, `name`, `is_dir`, `size`, `modified_time`,
`shortcut_target_id`。

超限或整体替换时：

```json
{"kind": "refresh", "full": true, "truncated": true, "node_count": 12345,
 "added": [], "updated": [], "removed": []}
```

### 7.3 发布点

- `_save_snapshot_incremental`：返回 `TreeChangeSet`（插入→added，变化→updated，
  删除前收集→removed）；`_save_snapshot` 透传；提交后由
  `_publish_tree_change` 发送。
- `_save_snapshot_replace`：返回 `TreeChangeSet(kind="refresh", full=True)`。
- `_upsert_nodes`：提交后发送 `kind="archive"` 的 added/updated（覆盖
  `TransferComplete` 实时入树与归档轮询两条路径）。
- `api_clear`：发布 `kind="clear", full=True`。
- 发送实现：

```python
eventmanager.send_event(EventType.PluginTriggered, build_payload(...))
```

- 仅在 `_enabled and _strm_notify and _strm_plugin_id` 时发送；所有异常捕获后
  记 `warning`，不影响刷新。
- 发布器实例 ID：`getattr(type(self), "plugin_instance_id", "") or type(self).__name__`
  （宿主会把分身类名改写为实例 ID）。

### 7.4 事件契约（稳定名 `tree.changed`）

```json
{
  "plugin_id": "GDriveTree",
  "event_name": "tree.changed",
  "data": {
    "kind": "refresh|archive|clear",
    "full": false,
    "truncated": false,
    "node_count": 3,
    "added":   [{"file_id": "...", "path": "/A/b.mkv", "name": "b.mkv",
                 "is_dir": false, "size": 1, "modified_time": 0.0,
                 "shortcut_target_id": ""}],
    "updated": [],
    "removed": [{"file_id": "...", "path": "/A/c.mkv"}]
  },
  "__mp_target_plugin_id": "GDriveStrm"
}
```

宿主派发时移除 `__mp_target_plugin_id`，只投递给目标实例。

## 8. GDriveStrm 插件类

### 8.1 配置

| 配置 | 默认 | 约束 |
| --- | --- | --- |
| `enabled` | 关 | 总开关 |
| `tree_plugin_id` | `GDriveTree` | 非空 |
| `output_dir` | `/config/strm` | 非空；相对路径按宿主配置目录解析 |
| `extensions` | 默认视频集合 | 逗号/空格/换行分隔 |
| `url_template` | Google Drive 直链 | 必须包含 `{fileId}` |
| `event_watch` | 开 | 是否处理 `tree.changed` |
| `cleanup` | 开 | 是否删除失效 STRM |
| `auto_sync` | 关 | 定时对账 |
| `sync_interval_minutes` | 10 | 1–1440 |
| `page_size` | 20 | 5–100 |

### 8.2 生命周期与线程

- `__init__` 创建 `_sync_lock`、`_queue`、`_worker_stop`；`init_plugin` 不替换锁。
- `init_plugin`：先 `stop_service()`，归一化配置，按 `enabled` 启动 daemon
  `GDriveStrm-worker` 线程。
- 工作线程：

```text
job = queue.get(timeout=1s)
batch = [job] + 继续收集 COALESCE_SECONDS（1.0s）内的 job
merged = merge(batch)
    任一 full → 全量
    任一 diff → 记录对账
    否则 → 事件增量（按 file_id/path 去重，后到覆盖先到）
执行；异常写 meta.status=failed，不杀线程
```

- 合并后变更节点超过 5000 → 升级为 diff 对账。
- `stop_service()`：置位 `_worker_stop`，可选哨兵入队，最多 join 5 秒；关闭时正在
  写单个文件允许完成（原子替换）。

### 8.3 作业类型

| 作业 | 触发 | 行为 |
| --- | --- | --- |
| `full` | 「全量同步」按钮 | 读全树 + 逐目标校验内容 + 清理失效 |
| `diff` | 定时服务、「立即同步」按钮 | 读全树，按记录跳过一致项；清理失效 |
| `event` | `tree.changed` | 只处理事件携带的变更；`full=True` 时转 diff |
| `clear` | 「清空记录」 | 删除 record 与 meta 统计，不删文件 |

### 8.4 API 与页面

| API | 说明 |
| --- | --- |
| `POST /sync` | 入队 diff，立即返回 |
| `POST /sync/full` | 入队 full，立即返回 |
| `POST /status` | 返回 meta + 树源可用性 |
| `POST /records/page` | 记录分页（按 `rel_path` + `id` 排序） |
| `POST /clear` | 清空记录与统计 |

- 全部返回 `{success, message, data}` 三键。
- 页面：VAlert（`style: flex: 0 0 auto`）、状态行、三个按钮、记录分页表。
- 页面按钮不直接执行同步，只入队，避免 HTTP 线程被长任务占用。

## 9. 错误与边界

| 场景 | 行为 |
| --- | --- |
| 源插件库/表不存在 | meta `failed`，提示先启用并刷新 GDriveTree；不动已有 STRM |
| `tree_meta` 无行（未建树） | 同上；不做清理 |
| 输出目录为空 / `/` / 不可写 | 同步失败，明确提示；不做删除 |
| 单个文件写入失败 | 记入 errors，继续其它文件；meta 显示错误摘要 |
| 事件来自其它插件或其它 `event_name` | 忽略 |
| 事件目标插件未启用 | 同步不启动；事件不会到达 |
| 事件丢失 / 插件重启 | 定时对账与全量同步补偿 |
| 同一文件 ID 改名 | 事件 updated 携带新 path，按 ID 识别旧记录并迁移 |
| 树刷新进行中读取 | 根路径与节点在同一事务内读取，得到一致快照；异常则本次失败，下次重试 |
| 输出目录或目录树切换 | 检测到变化时重置记录并跳过本次清理，避免删除新根目录中的用户文件 |
| 同一路径被新文件 ID 替换 | 按 `source_file_id` 严格匹配删除，删除事件不会误删新文件 |
| 用户手动修改 `.strm` | 全量同步会按内容校正；diff/事件仅按记录判断 |

## 10. 关键权衡

- **读库 vs 调 GDriveTree API**：读库避免分页与网络开销，并能拿到与树一致的快照；
  代价是强耦合表结构，因此把 SQL 集中在 `tree_source.py`，并对缺表给出可读错误。
- **事件携带变更 vs 只发信号**：携带变更可秒级落盘；超过 2000 节点退化为全量信号，
  避免事件 payload 过大与事件总线阻塞。
- **记录表 vs 每次扫目录**：记录表让定时对账只做内存差量；文件内容仍以磁盘为准，
  记录缺失时读文件兜底，避免记录丢失造成重复改写。
- **清理只删已记录文件**：满足媒体库一致性，同时把误删风险限制在本插件写过的文件。

## 11. 兼容与回滚

- GDriveTree 1.1.0 → 1.2.0：新增配置默认关闭，不影响既有行为；无表结构变更。
- GDriveStrm 1.0.0：全新插件、全新表，无迁移。
- 回滚：关闭本插件并停止定时服务即可停止写文件；关闭 GDriveTree 的「通知 STRM
  插件」即可停止事件；代码层按插件目录整体回退。
- 测试：`python -m pytest tests/v3/gdrivestrm tests/v3/gdrivetree -q`，
  `python -m py_compile`，`json.load(package.v3.json)`。
