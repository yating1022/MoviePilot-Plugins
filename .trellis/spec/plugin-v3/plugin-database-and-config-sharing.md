# Scenario: 插件间共享配置与插件自有数据库

## 1. Scope / Trigger

适用于需要 **复用另一个插件配置** 或 **持久化自有数据** 的 MoviePilot V3 插件。

触发条件：

- 新插件要读取 `GDriveStorage` 等已有插件的 OAuth/连接配置；
- 插件需要新建表保存记录、快照或状态；
- 插件刷新/同步任务需要「成功整体替换、失败保留旧数据」。

证据基线：MoviePilot `v3` 分支 `app/sdk/plugin/base.py`、`app/sdk/database.py`、`app/db/plugin/base.py`、`app/db/plugin/container.py`；参考实现 `plugins.v3/gdrivetree/`、`plugins.v3/u115autoupload/`。

## 2. 跨插件读取配置

`_PluginBase` 提供了带 `plugin_id` 参数的公开方法：

```python
def get_config(self, plugin_id: Optional[str] = None) -> Any:
    if not plugin_id:
        plugin_id = self.__class__.__name__
    return self._plugininstance.get_config_data(plugin_id)
```

规则：

1. `plugin_id` 默认是插件类名（例如 `GDriveStorage`）。
2. 插件分身使用分身 ID；把源插件 ID 做成配置项，默认填类名。
3. 返回的是配置 `config_data` 的深拷贝；不存在返回 `None`。
4. 只读复用，不写回源插件配置。
5. 源配置缺失时给可读提示，不要抛未捕获异常。

```python
raw = self.get_config(self._source_plugin_id)
if not isinstance(raw, dict):
    raise MyAuthError(f"未找到插件 {self._source_plugin_id} 的配置")
client_id = str(raw.get("client_id") or "").strip()
```

### 2.1 ID 归一化

源插件允许用户填分享链接时，消费方必须自行提取纯 ID，不要直接拼进 REST 路径：

```python
_FOLDER = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_QUERY_ID = re.compile(r"[?&]id=([A-Za-z0-9_-]+)")
```

否则 URL 会被 `quote()` 编码成 `/files/https%3A%2F%2F...`，必然 404。

## 3. 插件自有数据库

### 3.1 声明

```python
from app.sdk.database import plugin_declarative_base

_PluginDBBase = plugin_declarative_base()

class MyRecord(_PluginDBBase):
    __tablename__ = "my_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ...

def get_database_models(self):
    return [MyRecord]
```

规则：

1. 每次 `plugin_declarative_base()` 返回带独立 `MetaData` 的基类，热重载安全。
2. 同一插件所有模型继承同一个基类。
3. 表由宿主按 `get_database_models()` 建立；声明了 `get_database_migrations()` 时改用 Alembic。
4. 表名在插件 schema 内（PostgreSQL），SQLite 下为独立引擎；不要加插件前缀。
5. 不要在模块导入期/类定义期建连接或查询。
6. 字段名、索引按查询路径设计；分页排序必须有唯一 tiebreaker（如 `id`），否则 `OFFSET/LIMIT` 可能重复/漏行。

### 3.2 会话

```python
with self.get_database().session() as session:
    session.add(record)
    session.commit()
```

- `session()` 每次返回新会话，调用方负责 commit/rollback/close（`with` 负责 close）。
- PostgreSQL 下会话事务开始时把 `search_path` 限定到插件 schema。
- 捕获 `SQLAlchemyError` 并记录日志；页面查询失败不要向页面抛 500。

### 3.3 快照式刷新

成功整体替换、失败保留旧数据：

```python
# 1. 先写 running（同步，页面可见）
# 2. 抓取/构建全部在内存完成
# 3. 单事务：delete 旧行 + bulk_insert 新行 + 更新 meta + commit
# 4. 失败/取消只更新 meta.status，不 touch 数据表
```

注意：

- 单事务替换简单且原子；10 万行级 bulk insert 应在数秒内完成并分批（每批 500 行，避免 SQL 参数上限）。
- 失败/取消必须回滚事务，旧数据保持不变。
- 写 `running` 要发生在后台线程启动**之前**，否则页面重载会读到旧状态。

### 3.4 后台刷新线程

```python
if not self._refresh_lock.acquire(blocking=False):
    return False
event = threading.Event()
self._stop_event = event
self._write_meta(status="running", ...)
thread = threading.Thread(
    target=self._run_refresh, args=(event, self._refresh_lock), daemon=True
)
thread.start()
```

规则：

1. 刷新锁只在实例构造时创建；`init_plugin` **不要替换**它，否则旧线程 finally 会释放新锁。
2. 每次刷新创建独立 `Event`，作为参数传入线程；不要在线程里读取可能已被替换的实例字段。
3. 线程只释放自己拿到的锁。
4. `stop_service()` 置位当前事件、关闭当前客户端、最多 join 5 秒。
5. 重试/退避等待用 `event.wait(delay)`，停用可立即打断。

## 4. Validation & Error Matrix

| 条件 | 行为 | 修复 |
| --- | --- | --- |
| 源配置不存在 | 状态 `failed`，提示源插件 ID | 先配置/启用源插件 |
| 源凭据缺字段 | 状态 `failed`，列出缺失键 | 回源插件补齐 |
| 源 ID 是分享链接 | 消费方提取纯 ID | 不要直接 URL-encode |
| 抓取不完整（`incompleteSearch=true`） | 中止，不写 success | 缩小范围或重试 |
| 刷新失败/取消 | 只更新 meta，旧数据保留 | 检查 error |
| 刷新中清空数据 | 拒绝 | 等刷新结束 |
| 排序无 tiebreaker | 翻页重复/漏行 | 追加 `id` |
| 在类定义期访问数据库 | 导入失败/连接泄漏 | 延迟到方法内 |

## 5. Tests Required

1. 纯逻辑模块按文件路径加载，不依赖 `app.*`。
2. 宿主桩：`app.sdk.database.plugin_declarative_base` 用真实 SQLAlchemy 基类；`app.sdk.plugin._PluginBase` 提供 `get_config/get_database`；用 SQLite `StaticPool` 内存库。
3. 覆盖：快照写入/查询、搜索转义、页码修正、失败保留旧数据、取消状态、源配置缺失、刷新编排成功/失败。
4. 静态检查：`get_api` 三键 envelope、`get_database_models` 表声明、`package.v3.json` 版本一致。
5. 测试中替换真实客户端，避免发起外部 HTTP 请求或遗留线程。

## 6. Wrong vs Correct

### Wrong

```python
# 初始化时替换锁，旧线程 finally 释放新锁
def init_plugin(self, config=None):
    self._refresh_lock = threading.Lock()

# 线程读取实例字段，取消事件可能已被替换
def _run_refresh(self):
    while not self._stop_event.is_set():
        ...
```

```python
# 分享链接直接当 ID
file_id = str(raw.get("root_folder_id"))
```

```python
# 失败时也 delete 旧行
session.execute(delete(TreeNode))
try:
    session.bulk_insert_mappings(TreeNode, rows)
except Exception:
    session.rollback()
```

### Correct

```python
def start_refresh(self) -> bool:
    if not self._refresh_lock.acquire(blocking=False):
        return False
    event = threading.Event()
    self._stop_event = event
    self._write_meta(status="running", ...)
    threading.Thread(
        target=self._run_refresh, args=(event, self._refresh_lock), daemon=True
    ).start()
    return True
```

```python
root_id = _extract_drive_id(raw.get("root_folder_id")) or "root"
```

```python
# 单事务整体替换；异常时回滚，旧数据保留
with self.get_database().session() as session:
    session.execute(delete(TreeNode))
    session.bulk_insert_mappings(TreeNode, rows)
    session.commit()
```
