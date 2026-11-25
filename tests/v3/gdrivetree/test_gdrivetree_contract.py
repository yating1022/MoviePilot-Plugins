"""GDriveTree 插件的静态契约检查（AST / package.v3.json）。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterable, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_PATH = REPO_ROOT / "plugins.v3" / "gdrivetree" / "__init__.py"
PACKAGE_PATH = REPO_ROOT / "package.v3.json"
ICONS_DIR = REPO_ROOT / "icons"
ENVELOPE_KEYS = {"success", "message", "data"}
FORBIDDEN_IMPORT_PREFIXES = (
    "app.core",
    "app.helper",
    "app.utils",
    "app.db",
    "app.plugins",
)


def _tree() -> ast.Module:
    """解析插件源码。"""
    return ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    """查找指定类定义。"""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _method_node(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    """查找类中的指定方法。"""
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found")


def _class_attr(class_node: ast.ClassDef, name: str) -> Optional[ast.AST]:
    """读取类级赋值。"""
    for node in class_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.value
    return None


def _dict_keys(node: ast.Dict) -> Set[str]:
    """读取字典字面量的字符串键。"""
    keys: Set[str] = set()
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


def _return_dicts(func: ast.FunctionDef) -> Iterable[ast.Dict]:
    """遍历函数体内 return 的字典字面量。"""
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            yield node.value


def test_plugin_directory_matches_class_name() -> None:
    """插件目录必须是类名的小写形式。"""
    class_node = _class_node(_tree(), "GDriveTree")
    assert PLUGIN_PATH.parent.name == class_node.name.lower()


def test_plugin_identity_attributes() -> None:
    """插件标识、版本前缀必须符合约定。"""
    class_node = _class_node(_tree(), "GDriveTree")
    version = _class_attr(class_node, "plugin_version")
    prefix = _class_attr(class_node, "plugin_config_prefix")
    assert isinstance(version, ast.Constant) and version.value == "1.2.6"
    assert isinstance(prefix, ast.Constant) and prefix.value == "gdrivetree_"


def test_get_api_returns_exactly_three_keys() -> None:
    """每个页面端点都必须返回三键 envelope。"""
    class_node = _class_node(_tree(), "GDriveTree")
    get_api = _method_node(class_node, "get_api")
    list_returns = [
        node.value for node in ast.walk(get_api)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.List)
    ]
    assert list_returns, "get_api must return a list literal"
    endpoints: List[str] = []
    for element in list_returns[0].elts:
        assert isinstance(element, ast.Dict)
        for key, value in zip(element.keys, element.values):
            if isinstance(key, ast.Constant) and key.value == "endpoint":
                assert isinstance(value, ast.Attribute)
                endpoints.append(value.attr)
    assert endpoints, "get_api must declare at least one endpoint"
    for endpoint in endpoints:
        method = _method_node(class_node, endpoint)
        returns = list(_return_dicts(method))
        assert returns, f"{endpoint} must return a dict envelope"
        for node in returns:
            assert _dict_keys(node) == ENVELOPE_KEYS, (
                f"{endpoint} envelope keys must be {ENVELOPE_KEYS}, "
                f"got {_dict_keys(node)}"
            )


def test_database_models_are_declared() -> None:
    """必须声明两张插件自有表并交给宿主建表。"""
    tree = _tree()
    class_node = _class_node(tree, "GDriveTree")
    get_models = _method_node(class_node, "get_database_models")
    list_returns = [
        node.value for node in ast.walk(get_models)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.List)
    ]
    assert list_returns, "get_database_models must return a list literal"
    names = {
        element.id
        for element in list_returns[0].elts
        if isinstance(element, ast.Name)
    }
    assert names == {"TreeMeta", "TreeNode", "ArchiveLink"}

    for model_name, table_name in (
        ("TreeMeta", "tree_meta"),
        ("TreeNode", "tree_node"),
        ("ArchiveLink", "archive_link"),
    ):
        model = _class_node(tree, model_name)
        base_names = {base.id for base in model.bases if isinstance(base, ast.Name)}
        assert "_PluginDBBase" in base_names, f"{model_name} must inherit plugin base"
        tablename = _class_attr(model, "__tablename__")
        assert isinstance(tablename, ast.Constant) and tablename.value == table_name


def test_plugin_reuses_source_config_via_public_api() -> None:
    """插件通过 get_config(source_plugin_id) 读取源配置。"""
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert "self.get_config(self._source_plugin_id)" in source
    assert "GDriveStorage" in source
    assert "build_tree(" in source
    assert "list_all(" in source


def test_no_forbidden_host_imports() -> None:
    """不得导入宿主内部模块。"""
    tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            assert not module.startswith(FORBIDDEN_IMPORT_PREFIXES), (
                f"forbidden import: {module}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_IMPORT_PREFIXES), (
                    f"forbidden import: {alias.name}"
                )


def test_package_metadata_matches_plugin_version() -> None:
    """package.v3.json 的版本、历史与图标必须与插件一致。"""
    class_node = _class_node(_tree(), "GDriveTree")
    version_node = _class_attr(class_node, "plugin_version")
    assert isinstance(version_node, ast.Constant)
    version = version_node.value
    package = json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))
    entry = package["GDriveTree"]
    assert entry["version"] == version
    assert entry["icon"]
    assert (ICONS_DIR / entry["icon"]).is_file()
    assert next(iter(entry["history"])) == f"v{version}"


def test_scheduled_service_is_declared() -> None:
    """必须提供定时刷新服务入口。"""
    class_node = _class_node(_tree(), "GDriveTree")
    _method_node(class_node, "get_service")
    _method_node(class_node, "refresh_from_scheduler")
    source = PLUGIN_PATH.read_text(encoding="utf-8")
    assert '"trigger": "interval"' in source
