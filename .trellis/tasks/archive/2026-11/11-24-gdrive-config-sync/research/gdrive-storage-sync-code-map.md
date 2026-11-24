# GDriveStorage 配置同步 — 代码地图（研究）

> 目的：为子任务 A/B 提供准确的集成点，避免重复扫描 87KB 的 `__init__.py`。
> 行号基于 2026-11-24 工作区 `plugins.v3/gdrivestorage/__init__.py`。

## 1. 插件类与生命周期

| 位置 | 内容 |
| --- | --- |
| `__init__.py:480` | `class GDriveStorage(_PluginBase)` |
| `:484-493` | 类属性：`plugin_version = "1.1.3"`、`plugin_config_prefix` 等 |
| `:504-516` | `__init__`：初始化锁/配置/日志/客户端 |
| `:518-550` | `init_plugin`：`stop_service()` → `_normalize_config` → 读取 `logs`/`quota` → 构建 client/provider → 注册存储 |
| `:552` | `get_state()` 返回 `_enabled` |
| `:561-571` | `stop_service()`：关闭 HTTP 客户端，可重复调用 |
| `:573-594` | `_build_client()` |
| `:596-612` | `_rebuild_client()` |

同步功能的运行时状态、`get_service`、`sync_from_scheduler`、`_perform_sync` 应放在生命周期与配置之间或配置之后，保持在类内。

## 2. 配置

| 位置 | 内容 |
| --- | --- |
| `:52-69` | `FORM_KEYS` 白名单（系统存储保存时使用） |
| `:621-653` | `_normalize_config()` 返回完整配置字典 |
| `:654-665` | `_save_config()` 合并后 `update_config` |
| `:1511-1767` | `get_form()`：VForm 组件树 + 默认值字典 |
| `:1700-1760` | 末尾的删除方式/分片/超时分组（新同步分组插在其后） |

注意：`tests/v3/gdrivestorage/test_plugin_contract.py:69` 的 `REQUIRED_FORM_KEYS` 断言默认值键集合恰好相等，新增配置键必须同步更新测试。

## 3. API 与页面

| 位置 | 内容 |
| --- | --- |
| `:1138-1196` | `get_api()` 端点声明列表 |
| `:1200-1236` | `api_test()`：三键 envelope 示例 |
| `:1437-1449` | `api_logs_page(payload: dict = Body(...))`：带 Body 参数写法 |
| `:1769-1925` | `get_page()`：告警、按钮、状态卡片、日志表格 |
| `:1927-1938` | `_alert()`：固定高度容器内 `style: flex: 0 0 auto;` |
| `:1940-1966` | `_page_button()`：调用 `plugin/{class}{path}`，`method: post` |

页面按钮事件路径格式：`plugin/GDriveStorage/sync/push` → `get_api` 中声明 `path: "/sync/push"`。

## 4. 调度器模式参考

`plugins.v3/u115autoupload/__init__.py:1118-1132`：

```python
def get_service(self) -> Optional[List[Dict[str, Any]]]:
    if not self._enabled or not self._watch_dir or not self._target_dir:
        return []
    return [{
        "id": "U115AutoUpload",
        "name": "115 目录秒传上传（定时兜底）",
        "trigger": "interval",
        "func": self.scan_once,
        "kwargs": {"seconds": self._scan_interval},
    }]
```

`plugins.v3/gdrivetree/__init__.py:607-636` 展示多服务注册与 `refresh_from_scheduler()`（只启动后台线程立即返回）。

## 5. HTTP 传输层模式

`plugins.v3/gdrivestorage/gdrive_client.py:144-156`：

```python
try:
    import httpx2 as _httpx
except ImportError:
    import httpx as _httpx
self._client = _httpx.Client(timeout=timeout, follow_redirects=True)
```

同步推送应使用 `follow_redirects=False`。测试可仿照 `tests/v3/gdrivetree/test_gdrive_tree_client.py` 注入 FakeTransport。

## 6. 现有测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/v3/gdrivestorage/test_plugin_contract.py` | AST 检查 `get_api()` 每个端点的 return 字典恰好三键；表单默认键集合；版本/history/README 一致性；宿主桩运行时行为 |
| `tests/v3/gdrivestorage/test_gdrive_client.py` | 客户端纯逻辑 |

新增 `api_sync_push` 的所有 `return` 字典字面量必须正好三键（`test_page_api_returns_exactly_three_keys` 会遍历）。

## 7. 发布信息

| 位置 | 当前值 |
| --- | --- |
| `__init__.py:485` | `plugin_version = "1.1.3"` |
| `package.v3.json` → `GDriveStorage.version` | `1.1.3` |
| `package.v3.json` → `GDriveStorage.history` | 顶部 `v1.1.3` |
| `plugins.v3/gdrivestorage/README.md` | 需新增「配置同步」章节 |

## 8. 约束提醒

- `_PluginBase`、`logger`、`eventmanager` 来自 `app.sdk.*`；不导入 `app.core`/`app.helper`/`app.utils`/`app.db.models`（契约测试会检查）。
- 页面 API 失败时宿主只弹 toast、不刷新；成功才刷新。
- 本仓库根目录当前无 `docs/`，文档任务需新建该目录。
