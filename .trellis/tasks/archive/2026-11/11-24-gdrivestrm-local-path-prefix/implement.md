# 实施计划：STRM 本地路径内容与前缀

## 前置

- 先执行 `python ./.trellis/scripts/task.py start` 再改代码。
- 阅读 `.trellis/spec/plugin-v3/page-api-envelope.md`（版本/表单/页面契约）。
- 事实来源：`strm_mapper.py`（内容生成）、`strm_syncer.py`（内容比较）、
  `__init__.py`（配置与执行器）。

## 实施步骤

### 1. 纯逻辑：路径内容生成

- [ ] `strm_mapper.py` 新增：
  - `CONTENT_MODE_URL` / `CONTENT_MODE_PATH` / `DEFAULT_CONTENT_MODE` /
    `DEFAULT_PATH_PREFIX`；
  - `join_path_prefix(prefix, relative)`（反斜杠转换、合并斜杠、去尾斜杠、
    空前缀补前导 `/`）。
- [ ] `build_target()` 增加关键字参数 `content_mode`、`path_prefix`：
  - 路径模式使用 `join_path_prefix()`，不校验 `{fileId}`；
  - URL 模式保持现状。
- [ ] `build_targets()` 透传新参数。
- [ ] 更新 `StrmTarget.url` 与相关 docstring，说明它是通用「内容」。

验证：

```bash
python -m pytest tests/v3/gdrivestrm/test_gdrivestrm_mapper.py -q
```

### 2. 同步器透传

- [ ] `StrmSyncer.apply_changes()` 增加 `content_mode`、`path_prefix` 关键字参数，
      传给 `build_target()`。
- [ ] 更新 docstring 与参数说明。

验证：

```bash
python -m pytest tests/v3/gdrivestrm/test_gdrivestrm_syncer.py -q
```

### 3. 插件配置与校验

- [ ] `__init__.py` 导入新常量，`__init__` 增加 `_content_mode`、`_path_prefix`。
- [ ] `_normalize_config()` 增加 `content_mode`（只接受 `path`，否则 `url`）
      与 `path_prefix`。
- [ ] `init_plugin()` 赋值并输出模式日志；同步开始时日志带模式/前缀。
- [ ] `get_form()`：
  - 新增内容模式 `VSelect` 与前缀 `VTextField`；
  - 默认值补充两项；
  - 顶部提示文案说明两种模式。
- [ ] `_execute_locked()` 校验改为按模式：
  - 路径模式空前缀 → meta failed + 返回；
  - URL 模式缺 `{fileId}` → 保持现有失败逻辑。
- [ ] `build_targets()` / `apply_changes()` 调用处传入模式与前缀。
- [ ] `get_page()` 状态信息可选补充当前内容模式（不改变表格结构）。
- [ ] `plugin_version` 升级为 `1.0.2`。

### 4. 测试

- [ ] `test_gdrivestrm_mapper.py` 增加：
  - 路径模式示例（前缀 `/home/googleDevice`）；
  - 前缀尾斜杠、前缀 `/`、空前缀、`D:/Media`；
  - `root_path=/GDrive/` 裁剪；
  - 路径模式忽略 `shortcut_target_id`；
  - 非法相对路径仍被拒绝。
- [ ] `test_gdrivestrm_syncer.py` 增加：
  - `apply_changes(..., content_mode="path", path_prefix=...)` 新增文件内容正确；
  - 更新事件内容变化后重写。
- [ ] `test_gdrivestrm_runtime.py` 增加：
  - `_new_plugin(..., content_mode="path", path_prefix=...)` 全量同步写出预期内容；
  - 路径模式空前缀 → meta failed，文件不存在；
  - URL→路径模式切换后旧文件被重写；
  - 现有 URL 模式用例全部通过。
- [ ] 检查 `_new_plugin` 辅助函数是否接受额外配置；必要时扩展。

验证：

```bash
python -m pytest tests/v3/gdrivestrm -q
```

### 5. 文档与清单

- [ ] `plugins.v3/gdrivestrm/README.md`：
  - 功能说明增加本地路径模式；
  - 配置表增加 `内容模式`、`本地路径前缀`；
  - 常见问题增加「内容为本地路径时媒体服务器读不到」的排查项。
- [ ] `package.v3.json`：
  - `GDriveStrm.version` → `1.0.2`；
  - `history` 增加 `v1.0.2` 条目；
  - 描述按需补充「可写本地路径」。

### 6. 全量验证

```bash
python -m pytest tests/v3/gdrivestrm -q
python -m py_compile plugins.v3/gdrivestrm/__init__.py \
  plugins.v3/gdrivestrm/strm_mapper.py \
  plugins.v3/gdrivestrm/strm_syncer.py \
  plugins.v3/gdrivestrm/tree_source.py
python -c "import json; json.load(open('package.v3.json', encoding='utf-8'))"
```

## 风险文件与回滚点

| 文件 | 风险 | 控制 |
| --- | --- | --- |
| `strm_mapper.py` | 内容生成契约变化 | 新参数都有默认值，URL 路径不回归 |
| `strm_syncer.py` | 事件增量漏传参数 | 参数有默认值；补单元测试 |
| `__init__.py` | 配置/表单/校验遗漏 | 默认值、归一化、表单三处同步修改 |
| `package.v3.json` | 版本不一致导致契约测试失败 | 与 `plugin_version` 同步修改 |

回滚点：

1. mapper/syncer 改动完成后可单独提交，默认 URL 模式行为不变。
2. 配置页改动未完成前，用户仍可用 URL 模式。
3. 出现异常时把 `content_mode` 改回 `url` 再同步一次即可恢复直链内容。
