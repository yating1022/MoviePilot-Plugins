# 宿主跨插件事件与数据库能力（MoviePilot v3）

> 证据来源：`jxxghp/MoviePilot` 分支 `v3` 浅克隆源码；本仓库 `plugins.v3/gdrivetree`。
> 用途：Google STRM 管理插件的事件通知与目录树读取设计依据。

## 1. 跨插件读取配置

`app/sdk/plugin/base.py`：

```python
def get_config(self, plugin_id: Optional[str] = None) -> Any
def get_database(self, plugin_id: Optional[str] = None) -> PluginDatabaseHandle
def get_data_path(self, plugin_id: Optional[str] = None) -> Path
```

- `plugin_id` 缺省为当前类名；分身使用分身 ID。
- `get_database(plugin_id)` 通过 `app/db/plugin/registry.py:get_database` 按需建立句柄：
  - SQLite：每插件独立库文件，句柄独占引擎；
  - PostgreSQL：宿主引擎派生 `schema_translate_map`，事务开始 `SET LOCAL search_path`
    到插件 schema，原生 SQL 同样解析到插件自己的表。
- 因此消费方可直接用 `text()` 读取另一插件的表，无需导入对方 ORM 模型。

参考：`.trellis/spec/plugin-v3/plugin-database-and-config-sharing.md`。

## 2. 定向插件事件

`app/schemas/types.py`：

```python
EventType.PluginTriggered = "plugin.triggered"
```

`app/schemas/event.py`：

```python
class PluginTriggeredEventData(ExtensibleEventData):
    plugin_id: str      # 发布事件的插件 ID
    event_name: str     # 插件定义的稳定事件名
    data: Any
```

派发定向：

- `app/runtime/event/contracts.py:214`：`_TARGET_PLUGIN = {PluginAction, PluginTriggered}`
  视为 `EventVisibility.TARGET_PLUGIN`。
- `app/runtime/event/dispatch.py:141`：从事件数据读取
  `__mp_target_plugin_id`，只投递给该实例，随后从 payload 移除该键。
- `dispatch.py:414 should_dispatch_to_target_plugin`：按处理器模块中的实例 ID
  （`app.plugins.<instance_id>`）大小写不敏感匹配；非插件处理器回落到类名。

发布示例：

```python
eventmanager.send_event(EventType.PluginTriggered, {
    "plugin_id": self.__class__.__name__,
    "event_name": "tree.changed",
    "data": {...},
    "__mp_target_plugin_id": target_plugin_id,
})
```

订阅示例：

```python
@eventmanager.register(EventType.PluginTriggered)
def on_plugin_triggered(self, event: Event) -> None:
    data = event.event_data
    ...
```

## 3. 插件实例 ID

- 本体：类名即插件 ID。
- 分身：`app/runtime/extensions/plugin/loader.py:519` 把类 `__name__` 改写为
  `instance.instance_id`，并设置 `plugin_instance_id` 类属性；类体内函数
  `__module__` 也改写到 `app.plugins.<instance_id>`，保证事件处理器按实例注册。

因此发布方使用：

```python
getattr(type(self), "plugin_instance_id", "") or type(self).__name__
```

## 4. GDriveTree 目录树契约

- 表：`tree_meta`（单行 id=1）、`tree_node`。
- `tree_node` 关键列：`file_id`、`parent_file_id`、`name`、`mime_type`、`is_dir`、
  `size`、`modified_time`、`path`、`depth`、`shortcut_target_id`。
- 路径由 `tree_builder._join_path` 生成，目录带结尾 `/`；根节点 path 等于
  `normalize_root_path(root_path)`，例如 `/` 或 `/GDrive/`。
- 刷新编排：`_run_refresh` → `_save_snapshot` → `_save_snapshot_incremental`
  （删除缺失 / 插入新增 / 更新变化）或 `_save_snapshot_replace`。
- 归档增量：`on_transfer_complete` / `scan_archive` → `_upsert_nodes`。
- 清空：`api_clear` 删除 `tree_node` 并重置统计。

## 5. 对本任务的结论

1. STRM 插件用 `get_database("GDriveTree")` 原生 SQL 读取树，包一层
   `tree_source.py` 隔离表结构。
2. 通知使用 `PluginTriggered` + `__mp_target_plugin_id`，事件名 `tree.changed`；
   载荷携带变更节点，超限退化为 `full=True`。
3. 发布点放在 GDriveTree 写库提交之后，异常隔离，默认关闭。
4. 事件只提示增量；定时与全量对账始终以 `tree_node` 为事实来源。
