# Scenario: 插件间定向事件（PluginTriggered）

## 1. Scope / Trigger

适用于一个插件需要把自身状态变化通知**指定**另一个插件的场景，例如目录树刷新后
通知 STRM 插件增量落盘。

触发条件：

- 需要跨插件触发动作，但不希望广播给所有订阅者；
- 需要稳定的事件名与可演进的数据结构；
- 发布方选择关闭时不得影响自身主流程。

参考实现：`plugins.v3/gdrivetree/tree_events.py` + `_publish_tree_change`
→ `plugins.v3/gdrivestrm/on_plugin_triggered`。

## 2. 签名与载荷

宿主提供 `EventType.PluginTriggered`（`plugin.triggered`），载荷类型为
`PluginTriggeredEventData`：

```python
{
    "plugin_id": "发布插件实例 ID",
    "event_name": "插件定义的稳定事件名",
    "data": {...},                      # 插件自定义
    "__mp_target_plugin_id": "目标实例 ID",  # 定向键，派发前会被移除
}
```

发布：

```python
eventmanager.send_event(EventType.PluginTriggered, payload)
```

订阅：

```python
@eventmanager.register(EventType.PluginTriggered)
def on_plugin_triggered(self, event: Event) -> None:
    data = getattr(event, "event_data", event)
    ...
```

## 3. 定向规则

- `__mp_target_plugin_id` 由宿主派发器读取并从 payload 移除，只投递给模块路径为
  `app.plugins.<instance_id>` 的处理器；比较大小写不敏感。
- 本体插件实例 ID 等于类名；分身由宿主把类 `__name__` 改写为分身 ID，并设置
  `plugin_instance_id` 类属性。发布方应使用
  `getattr(type(self), "plugin_instance_id", "") or type(self).__name__`。
- 目标插件未安装、未启用或没有注册处理器时，事件被丢弃；发布方不得依赖送达。

## 4. 载荷设计规则

1. `event_name` 发布后保持稳定，版本演进靠 `data` 内新增可选字段。
2. 单条事件载荷必须有上限（参考实现 2000 个节点）；超限退化为
   `data.full = true` 的「请自行全量对账」信号。
3. 变更明细只带消费方需要的字段；消费方必须容忍缺字段与未知字段。
4. 发布放在数据提交成功之**后**，且包裹 try/except，失败只记日志。
5. 消费方处理器只做入队，不在事件分发线程里做磁盘/网络操作。

## 5. 事件名约定（本项目）

| event_name | 发布方 | 说明 |
| --- | --- | --- |
| `tree.changed` | GDriveTree | 目录树刷新/归档/清空；`data.kind` 为 refresh/archive/clear |
| `tree.changed` 载荷 | — | `{kind, full, truncated, node_count, added, updated, removed}` |

节点载荷：`file_id, path, name, is_dir, size, modified_time, shortcut_target_id`；
删除节点仅需 `file_id, path`。

## 6. Validation & Error Matrix

| 条件 | 行为 | 修复 |
| --- | --- | --- |
| 缺少 `plugin_id` / `event_name` | 消费方忽略 | 发布方补齐 |
| 目标插件 ID 不匹配 | 宿主不投递 / 消费方忽略 | 对齐本体或分身 ID |
| 变更超过上限 | 置 `full=true` 并清空明细 | 消费方走全量对账 |
| 发布异常 | 记 warning，不中断主流程 | 检查目标配置/事件总线 |
| 消费方在处理器内同步写盘 | 阻塞事件线程 | 改为入队 + 后台线程 |
| 事件丢失 | 消费方数据滞后 | 定时对账兜底 |

## 7. Tests Required

1. 纯逻辑测试：载荷字段、上限退化、定向键、`full` 信号。
2. 发布方运行时测试：开关开启时发送、关闭时不发送、发布异常不影响刷新。
3. 消费方测试：来源/事件名过滤、合并规则、删除与幂等。
4. 定向键不出现在消费方业务数据中（宿主会移除）。
