# Google Drive 目录树插件 — 技术设计

## 1. 目录与模块边界

```text
plugins.v3/gdrivetree/
├── __init__.py            # GDriveTree 插件：生命周期、配置、刷新编排、数据库、页面/API
├── gdrive_tree_client.py  # 纯 REST/OAuth 客户端，不依赖 app.*
├── tree_builder.py        # 纯函数：从文件元数据构建并裁剪目录树
└── README.md

tests/v3/gdrivetree/
├── test_gdrive_tree_client.py   # 注入 FakeTransport
├── test_tree_builder.py         # 建树/裁剪/统计纯逻辑
└── test_plugin_contract.py      # AST/JSON/版本/数据库声明静态检查
```

- `gdrive_tree_client.py`、`tree_builder.py` 不导入 `app.*`，可在无宿主环境直接加载测试。
- 不导入 `plugins.v3.gdrivestorage.*`，只通过 `get_config("GDriveStorage")` 共享配置。
- `httpx2` 延迟导入；测试通过 `transport` 注入替身。
- 无新增 `pyproject.toml` / 第三方依赖。

## 2. 配置模型

`get_form()` 默认值：

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | false | 启用插件 |
| `source_plugin_id` | str | `GDriveStorage` | 读取配置的源插件 ID |
| `auto_refresh` | bool | false | 启用定时刷新 |
| `refresh_interval_hours` | int | 24 | 定时刷新间隔，1–168 |
| `include_trashed` | bool | false | 是否包含回收站文件 |
| `max_pages` | int | 2000 | 单次拉取最大页数 |
| `max_nodes` | int | 200000 | 单次最大入库节点数 |
| `page_size` | int | 20 | 详情页每页节点数，5–100 |

`plugin_config_prefix = "gdrivetree_"`，`plugin_version = "1.0.0"`。

配置归一化：所有数值经 `_clamp_int`；`source_plugin_id` 去空白，空则回退 `GDriveStorage`。

## 3. 客户端 `gdrive_tree_client.py`

### 3.1 构造

```python
GDriveTreeClient(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    timeout: float = 60,
    transport: Optional[HttpTransport] = None,
)
```

`transport` 为空时创建 `_HttpxTransport`（延迟 `import httpx2`，失败回退 `httpx`）。仅需要只读能力。

### 3.2 公开方法

| 方法 | 返回 | 说明 |
| --- | --- | --- |
| `refresh_access_token(force=False)` | `str` | 带锁缓存，`expires_at - 60s` 提前过期 |
| `get_file(file_id, fields=...)` | `dict` | `GET /drive/v3/files/{id}`，`supportsAllDrives=true` |
| `resolve_root(root_folder_id)` | `RootInfo` | 解析 `root` 别名、快捷方式、`driveId`，决定 `corpora` |
| `iter_files(corpora, drive_id, include_trashed, max_pages, stop_event)` | `Iterator[dict]` | 分页 `files.list` |
| `close()` | `None` | 关闭传输层 |

`RootInfo` 为 dataclass：

```python
@dataclass(frozen=True)
class RootInfo:
    file_id: str          # 真实根 ID
    name: str             # 根显示名
    drive_id: str         # 共享盘 ID；空表示 user corpora
    corpora: str          # "user" | "drive"
```

### 3.3 根解析

1. `root_folder_id` 为 `root` / 空：`GET /files/root?fields=id,name` 得到真实 ID。
2. 否则 `get_file(root_id)`。
3. 若 `mimeType == application/vnd.google-apps.shortcut`，取 `shortcutDetails.targetId` 后再次 `get_file`。
4. 若最新元数据有 `driveId`：`corpora="drive"`、`drive_id=<driveId>`；否则 `corpora="allDrives"`（覆盖「共享给我」文件夹与可访问共享盘，避免只抓到根节点）。
5. 根名优先取元数据 `name`；元数据缺失时用 `GDriveStorage.root_path` 的末段或 `/`。

### 3.4 分页

```python
params = {
    "q": "trashed = false" if not include_trashed else None,
    "fields": "nextPageToken,files(id,name,mimeType,parents,size,modifiedTime,createdTime,md5Checksum,trashed,driveId,shortcutDetails)",
    "pageSize": 1000,
    "supportsAllDrives": "true",
    "includeItemsFromAllDrives": "true",
    "corpora": corpora,
}
if corpora == "drive":
    params["driveId"] = drive_id
```

- 每页产出 `files`；`include_trashed=False` 时在客户端再次过滤 `trashed`。
- `fields` 同时请求 `incompleteSearch`；为 `true` 时中止（避免把部分结果标成完整快照）。
- `nextPageToken` 重复 → 抛 `GDriveTreeError("分页令牌重复")`。
- 超过 `max_pages` 或累计文件数超过 `max_files` → 抛错，提前中止。
- `stop_event.is_set()` → 抛 `GDriveTreeCancelled`，由编排层按「中止」处理，不写 success。
- 退避等待使用 `stop_event.wait()`，停用/重载可立即打断。

### 3.5 错误

```text
GDriveTreeError
├── GDriveTreeAuthError      # 配置缺失、401 刷新失败、invalid_grant
└── GDriveTreeCancelled      # 用户停用/重载中止
```

- 401：强制刷新一次并重试；仍失败抛 `GDriveTreeAuthError`。
- 403 `rateLimitExceeded` / `userRateLimitExceeded` / `backendError`、429、5xx：指数退避，最多 3 次。
- 传输层异常（连接重置、超时、TLS）：按同样策略重试，最终统一抛 `GDriveTreeError`。
- 403/404：`GDriveTreeError` 携带 `status_code`；根目录解析失败时补上根 ID 与共享盘提示。
- 根节点最终不是文件夹 → 抛错，不生成单节点假树。
- 不记录 token/secret。

## 4. 建树 `tree_builder.py`

```python
@dataclass
class TreeNodeData:
    file_id: str
    parent_file_id: Optional[str]
    name: str
    mime_type: str
    is_dir: bool
    size: int
    modified_time: float
    created_time: float
    path: str
    depth: int
    md5: str
    trashed: bool
    shortcut_target_id: str

@dataclass
class TreeStats:
    node_count: int
    dir_count: int
    file_count: int
    total_size: int
    max_depth: int

def build_tree(
    files: Iterable[Mapping[str, Any]],
    *,
    root_id: str,
    root_name: str,
    root_path: str = "/",
    max_nodes: int = 200000,
) -> tuple[list[TreeNodeData], TreeStats]:
    ...
```

规则：

- 按文件**全部** `parents` 建立子节点索引；BFS 首次访问到的父节点决定 `parent_file_id`，避免仅通过第二个父节点可达的节点被误裁。
- 从 `root_id` BFS；不可达节点（根子树之外）丢弃。
- 根节点若不在 `files` 中，合成一条：`file_id=root_id`、`parent_file_id=None`、`is_dir=True`、`depth=0`、`path=root_path`。
- 子节点 `path = parent.path.rstrip("/") + "/" + name`；目录再补尾斜杠。
- `depth = parent.depth + 1`；`max_depth` 取最大深度。
- `is_dir = mimeType == application/vnd.google-apps.folder`。
- `size` 缺失/非法按 0；`modifiedTime`/`createdTime` 用 `datetime.fromisoformat` 容错，转 Unix 秒。
- 快捷方式：保留为普通节点，`shortcut_target_id` 填 `shortcutDetails.targetId`；当 `shortcutDetails.targetMimeType` 是文件夹（或 target 有子项）时 `is_dir=True`，并以 `targetId` 作为子项查找键展开，子节点的 `parent_file_id` 仍指向快捷方式本身。
- `node_count > max_nodes` → 抛 `GDriveTreeError`，由编排层中止。
- 统计：`dir_count` 含根节点；`file_count` 为文件数；`total_size` 只累加文件。
- 结果按 `(depth, path)` 排序，保证入库顺序稳定、便于分页。

## 5. 数据库模型

```python
_PluginDBBase = plugin_declarative_base()

class TreeMeta(_PluginDBBase):
    __tablename__ = "tree_meta"
    id: int                 # 固定 1
    source_plugin_id: str
    root_file_id: str
    root_name: str
    root_path: str
    corpora: str
    drive_id: str
    status: str             # idle/running/success/failed/cancelled
    last_started_at: datetime | None
    last_finished_at: datetime | None
    last_success_at: datetime | None
    node_count: int
    dir_count: int
    file_count: int
    total_size: int
    max_depth: int
    page_count: int
    duration_seconds: float
    error: str
    updated_at: datetime

class TreeNode(_PluginDBBase):
    __tablename__ = "tree_node"
    id: int                 # 自增主键
    file_id: str            # index=True
    parent_file_id: str     # index=True
    name: str               # index=True
    mime_type: str
    is_dir: bool            # index=True
    size: int               # BigInteger
    modified_time: float
    created_time: float
    path: str               # index=True
    depth: int
    md5: str
    trashed: bool
    shortcut_target_id: str
    created_at: datetime
```

- `get_database_models()` 返回 `[TreeMeta, TreeNode]`。
- 不使用 Alembic；表在插件启动时自动建立。
- `file_id` 不设数据库唯一约束：刷新事务先 delete 再 insert，唯一性由建树阶段保证；避免插件热重载/旧数据导致的迁移问题。

## 6. 刷新编排

### 6.1 状态机

```text
idle -> running -> success
                -> failed
                -> cancelled
```

- `running` 由 `start_refresh()` 在拿到刷新锁后、启动线程前**同步**写入，保证页面重载时能立即看到「刷新中」。
- `success` 与节点替换在同一事务提交。
- `failed` / `cancelled` 只更新 meta，节点表保持上一次成功数据。

### 6.2 步骤

1. `_read_source_config()`：`self.get_config(self._source_plugin_id)`；校验 `client_id/client_secret/refresh_token`。
2. `resolve_root(root_folder_id)`。
3. `list_all(...)` 收集到 list（累计页数与文件数），每页检查 `stop_event`；`max_files` 提前中止。
4. `build_tree(files, root_id, root_name, root_path, max_nodes)`。
5. 单事务：
   ```python
   session.execute(delete(TreeNode))
   session.bulk_insert_mappings(TreeNode, rows)
   meta = session.get(TreeMeta, 1) or TreeMeta(id=1)
   # 更新成功字段
   session.commit()
   ```
6. 失败：单独事务写 `status=failed`、`error`、`last_finished_at`。

### 6.3 并发与线程

- `self._refresh_lock` 只在实例构造时创建；`init_plugin` **不替换**它，避免旧线程 finally 释放新锁。
- 每次 `start_refresh()` 创建独立的 `threading.Event`，作为参数传入 `_run_refresh(event, lock)`；旧线程只看到自己的事件，取消不会丢失。
- 刷新在线程中执行：`threading.Thread(target=self._run_refresh, args=(event, lock), daemon=True)`。
- 当前客户端保存在 `self._client`；`stop_service()` 置位当前事件并 `client.close()` 打断在途请求，然后最多等待 5 秒。
- 线程只释放自己拿到的锁（参数 `lock`），不读取可能已被替换的实例字段。
- `get_service()`：

```python
{
    "id": "GDriveTree",
    "name": "Google Drive 目录树（定时刷新）",
    "trigger": "interval",
    "func": self.refresh_from_scheduler,
    "kwargs": {"seconds": self._interval_hours * 3600},
}
```

`refresh_from_scheduler()` 只调用 `start_refresh()`，立即返回，避免占用调度线程。

## 7. 页面与 API

### 7.1 `get_page()`

固定高度 flex 容器（`height: 72vh; min-height: 520px; max-height: 800px; padding-bottom: 84px;`）：

1. 源配置状态 `VAlert`（`flex: 0 0 auto`）：可用 / 缺少哪一项。
2. 刷新状态与统计 `VAlert`：状态、根目录、corpora、最后成功时间、节点/目录/文件/总大小/最大深度/耗时/页数。
3. 最近错误 `VAlert`（有错时）。
4. 按钮行：立即刷新、刷新状态、清空数据、上一页/下一页。
5. `VTable` 节点列表：路径、类型、大小、修改时间；外层 `min-height: 0; overflow: auto;`。

### 7.2 `get_api()`

| path | 参数 | 返回 data |
| --- | --- | --- |
| `/refresh` | 无 | `{"started": bool}` |
| `/status` | 无 | meta 摘要 + 源配置状态 |
| `/nodes/page` | `{"page": int}` | `{"page", "pages", "items": [...]}`；按 `(depth, path, id)` 排序保证翻页稳定 |
| `/search` | `{"keyword": str, "page": int}` | 同分页，`keyword` 过滤 `name`/`path` |
| `/clear` | 无 | `{"deleted": int}` |

- 全部 `auth="bear"`，返回恰好三键 envelope。
- 节点项字段：`file_id`、`name`、`path`、`is_dir`、`size`、`modified_time`（格式化文本）。
- 搜索使用 `LIKE %keyword%`，对 `%`/`_` 做转义；空关键字退化为分页。
- 页面第一版不放置文本输入框，搜索能力先通过 API 暴露；后续可加。

## 8. 错误矩阵

| 条件 | 行为 |
| --- | --- |
| 源插件配置不存在 | 状态 `failed`，提示「未找到 GDriveStorage 配置」；旧数据保留 |
| `refresh_token` 为空 | 状态 `failed`，提示「请先完成 GDriveStorage 授权」 |
| `invalid_grant` | 状态 `failed`，提示重新授权 |
| 根目录 404/无权限 | 状态 `failed`，带根 ID 与共享盘提示 |
| 共享盘根解析失败 | 状态 `failed`，**不回退**账号根，避免把错误范围标成成功 |
| 根指向文件 | 状态 `failed`，提示 root_folder_id 应指向文件夹 |
| `incompleteSearch=true` | 中止，状态 `failed`，避免不完整快照 |
| 分页令牌重复 | 中止，状态 `failed`，不清空旧数据 |
| 超过 `max_pages` / `max_nodes` | 中止，状态 `failed`，提示调大上限 |
| 传输层异常/403 限流 | 指数退避重试，最终失败则状态 `failed` |
| 插件停用/重载 | 置位 stop_event，状态 `cancelled`，旧数据保留 |
| 重复点击刷新 | 返回 `started=False`，页面提示「正在刷新」 |
| 数据库写失败 | 事务回滚，旧数据保留，状态 `failed` |

## 9. 测试策略

- `test_tree_builder.py`
  - 正常三层树、路径/深度/统计。
  - 根不在 files 中时合成根。
  - 根子树外节点被裁剪。
  - 多 `parents` 取第一个。
  - 快捷方式保留 `shortcut_target_id`。
  - 回收站过滤。
  - `max_nodes` 超限抛错。
- `test_gdrive_tree_client.py`
  - FakeTransport：分页拼接、`nextPageToken` 重复报错、`max_pages` 报错。
  - 401 → 强制刷新 → 重试成功；刷新失败抛 `GDriveTreeAuthError`。
  - `resolve_root`：`root` 别名、快捷方式 target、`driveId` 选择 corpora。
  - `include_trashed` 过滤。
- `test_plugin_contract.py`
  - `plugin_version` 与 `package.v3.json` 一致、history 顶部含该版本。
  - `get_api` 端点函数返回三键（AST/源码检查）。
  - `get_database_models` 返回两张表，模型继承 `plugin_declarative_base()`。
  - `get_service` 在未启用/未开定时时返回空。
  - 图标文件存在；`py_compile`。

## 10. 兼容、发布与回滚

- 发布：`package.v3.json` 新增 `GDriveTree`，`version=1.0.0`，`icon="gdrive.png"`（复用现有图标，不新增二进制），`system_version=">=3.0.0"`，`labels="网盘"`。
- 回滚：插件停用即停止定时任务；`/clear` 可清空节点；卸载插件不影响 `GDriveStorage` 配置与宿主存储。
- 不在 `GDriveStorage` 中新增任何字段或行为，保持单向依赖（GDriveTree 读 GDriveStorage 配置）。
