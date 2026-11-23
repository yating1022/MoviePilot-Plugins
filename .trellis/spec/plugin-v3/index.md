# MoviePilot V3 插件规范

> 本目录记录 MoviePilot V3 插件（`plugins.v3/<plugin>/__init__.py`）的宿主接口契约与实现约定。
> 证据基线：MoviePilot 主程序/前端 v3.1.8，Vuetify 3.7.3。

## 索引

| 文档 | 说明 |
| --- | --- |
| [Page API Envelope & Layout](./page-api-envelope.md) | 详情页（`get_page`）按钮 API 响应契约、页面事件刷新机制、Vuetify 布局陷阱 |
| [Custom Storage Backend](./storage-extension.md) | 通过插件注册自定义存储、`StorageOperSelection` 适配器、`get_module` 存储方法与严格查询语义 |
| [Plugin Database & Config Sharing](./plugin-database-and-config-sharing.md) | 通过 `get_config(plugin_id)` 复用其他插件配置、插件自有表、快照式刷新与后台线程/取消模式 |
| [Cross-plugin Events](./cross-plugin-events.md) | `EventType.PluginTriggered` 定向事件、载荷上限与 `tree.changed` 契约 |
