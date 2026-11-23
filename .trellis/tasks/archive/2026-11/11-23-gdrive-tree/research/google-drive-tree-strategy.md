# Google Drive 目录树抓取策略（研究记录）

> 证据基线：2026-11-23。参考文章 `carljin.com/google-drive-structure-如何快速生成谷歌drive的文件树/`，Google Drive API v3 文档，MoviePilot `v3` 分支源码（`app/sdk/plugin/base.py`、`app/db/oper/plugininstance.py`、`app/db/plugin/base.py`、`app/db/plugin/container.py`）。

## 1. 文章「方法三」的本质

- 文章方法三使用 Apps Script `DriveApp.searchFiles(String)`（或其底层 REST `files.list?q=...`）一次性批量获取文件元数据，每页最多 1000 条，然后在本地递归生成目录树。
- 文章原话：1 万份文件约 21 条请求；浏览器 console 计时约 273ms；实测 1 万+ 文件、5~6 级目录不到 1 分钟。
- 文章给出的缺点：会抓取账号下所有文件，无关文件多时会变慢；建议单独开账号只共享目标文件夹。

## 2. 为什么不能「只拉子树」

Drive 查询语言只有 `'<id>' in parents`，它匹配**直接子项**，没有递归子树条件。因此：

- 逐层 BFS（方法一/二）= 很多请求，深目录慢，配额风险高。
- 一次全量 + 本地建树（方法三）= 少量请求，但必须先确定一个可枚举范围。

可枚举范围：

| 范围 | 参数 | 说明 |
| --- | --- | --- |
| 我的云端硬盘 | `corpora=user` | 不含共享盘内容，且可能漏掉「共享给我」文件夹 |
| 我的云端硬盘 + 全部可访问共享盘 | `corpora=allDrives` | 无 driveId 时的默认，覆盖最广 |
| 指定共享盘 | `corpora=drive&driveId=<id>` | 覆盖该共享盘内用户可见文件 |
| 全部共享盘 | `corpora=allDrives` | 可能很大，gdrivestorage 列举时采用 |

结论：方法三必然「全量拉取 + 本地裁剪」。本插件在拉取后以 GDriveStorage 的 `root_folder_id` 为根做 BFS 裁剪，只把目标子树入库。

## 3. 根目录解析策略

`GDriveStorage` 的 `root_folder_id` 可能是：

- `root` / 空：账号「我的云端硬盘」根。
- 普通文件夹 ID。
- 共享盘内文件夹 ID（元数据带 `driveId`）。
- 快捷方式 ID（`mimeType=application/vnd.google-apps.shortcut`，`shortcutDetails.targetId` 指向目标）。

建议解析顺序：

1. `GET /drive/v3/files/{root_id}?fields=id,name,mimeType,driveId,parents,shortcutDetails,supportsAllDrives=true`。
2. 若是快捷方式，取 `shortcutDetails.targetId`，再次 GET。
3. 若元数据有 `driveId`，用 `corpora=drive&driveId=<driveId>`；否则 `corpora=allDrives`（只抓根节点/漏文件的常见原因就是误用 `corpora=user`）。
4. `root` 别名先 `GET /drive/v3/files/root?fields=id` 换成真实 ID。
5. 拉取全量后从根 ID 按 `parents` 做 BFS，只保留可达节点。

## 4. files.list 参数

```http
GET /drive/v3/files
  ?q=trashed = false
  &fields=nextPageToken,files(id,name,mimeType,parents,size,modifiedTime,createdTime,md5Checksum,trashed,driveId,shortcutDetails)
  &pageSize=1000
  &supportsAllDrives=true
  &includeItemsFromAllDrives=true
  &corpora=user|drive
  &driveId=<id>            # 仅 corpora=drive
  &pageToken=<token>
```

注意：

- `size` 对 Google 原生文档（`application/vnd.google-apps.*`）缺失，按 0 处理。
- 一个文件可能有多个 `parents`（Drive 允许）。建树时应按全部 `parents` 建立子节点索引，BFS 首次访问到的父节点决定展示父节点，否则仅通过第二个父可达的节点会被误裁。
- 快捷方式本身没有目标内容；树中可作为叶子保留，或解析 `targetId`（仅当目标在可枚举范围内）。
- 需要处理分页令牌重复，避免死循环；设置最大页数/最大节点数保护。
- `fields` 应同时请求 `incompleteSearch`；为 `true` 表示结果不完整，不能当作完整快照入库。
- `root_folder_id`/`shared_drive_id` 可能来自 GDriveStorage 的分享链接，使用前需提取 `/folders/<id>` 或 `?id=<id>` 中的纯 ID。

## 5. 跨插件读取配置（宿主公开契约）

`app/sdk/plugin/base.py`：

```python
def get_config(self, plugin_id: Optional[str] = None) -> Any:
    if not plugin_id:
        plugin_id = self.__class__.__name__
    return self._plugininstance.get_config_data(plugin_id)
```

`PluginInstanceOper.get_config_data(instance_id)` 返回该实例 `config_data` 的深拷贝，不存在返回 `None`。因此新插件可以直接：

```python
source = self.get_config(self._source_plugin_id) or {}
```

读取 `GDriveStorage` 的 `client_id`、`client_secret`、`refresh_token`、`root_folder_id`、`root_path`、`use_shared_drive`、`shared_drive_id`。这是宿主公开方法，不违反「不导入宿主内部模块」的约束。

`plugin_id` 默认等于类名 `GDriveStorage`；若用户使用插件分身，表单允许填写源插件 ID。

## 6. 插件自有数据库

`app/sdk/database.py` 公开 `plugin_declarative_base` 与 `PluginDatabaseHandle`：

```python
_PluginDBBase = plugin_declarative_base()

class TreeNode(_PluginDBBase):
    __tablename__ = "tree_node"
    ...

def get_database_models(self):
    return [TreeNode]

with self.get_database().session() as session:
    ...
```

- 每次调用 `plugin_declarative_base()` 得到独立 `MetaData`，热重载安全。
- SQLite 下插件句柄独占独立引擎；PostgreSQL 下会话 `search_path` 限定到插件 schema，表名无需前缀。
- 插件启动时按 `get_database_models()` 建表；不要同时声明 `get_database_migrations()`。

## 7. 刷新原子性

两种方案：

1. **单事务 delete + bulk insert**：实现简单，提交前其他会话仍读到旧数据，提交后整体切换；缺点是刷新期间持有写锁（1 万行 bulk insert 通常几秒）。
2. **run_id 两阶段**：先插入新 run_id 的节点，完成后删除旧 run_id；查询按当前 run_id 过滤。写锁短，但需要额外的 meta/run 标识。

第一版采用方案 1，并在应用层加刷新锁；失败时回滚事务，旧数据完整保留。超大规模（> 配置上限）在入库前中止，不触碰旧数据。

## 8. 仍待验证（实现阶段）

- MoviePilot 宿主实际运行时 `get_config("GDriveStorage")` 返回的键与 `init_plugin` 收到的配置一致（预期一致）。
- 共享盘 `corpora=drive` 下根文件夹自身是否出现在 `files.list` 结果中；若不出现，需单独 GET 根元数据补节点。
- PostgreSQL 插件 schema 下 `bulk_insert_mappings` 与自增主键行为。
