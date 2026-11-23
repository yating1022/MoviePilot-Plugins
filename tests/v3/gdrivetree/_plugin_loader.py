"""按包上下文加载 gdrivetree 的纯逻辑/客户端模块，避免依赖 MoviePilot 宿主。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[3] / "plugins.v3" / "gdrivetree"
PACKAGE_NAME = "gdrivetree_under_test"
_PRELOAD = ("errors", "tree_builder")


def load_package() -> types.ModuleType:
    """创建以插件目录为搜索路径的虚拟包。"""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = package
    for name in _PRELOAD:
        load_module(name)
    return package


def load_module(name: str) -> types.ModuleType:
    """加载插件包中的指定模块。"""
    full_name = f"{PACKAGE_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    if PACKAGE_NAME not in sys.modules:
        load_package()
    spec = importlib.util.spec_from_file_location(full_name, PLUGIN_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module
