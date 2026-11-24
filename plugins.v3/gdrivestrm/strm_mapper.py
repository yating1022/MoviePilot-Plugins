"""STRM 路径映射的纯逻辑。

本模块不依赖 MoviePilot 宿主，也不读写文件系统，便于单独测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, Optional

DEFAULT_URL_TEMPLATE = (
    "https://www.googleapis.com/drive/v3/files/"
    "{fileId}?alt=media&supportsAllDrives=true"
)
URL_PLACEHOLDER = "{fileId}"

# STRM 内容模式：Google Drive 直链，或本地挂载路径
CONTENT_MODE_URL = "url"
CONTENT_MODE_PATH = "path"
DEFAULT_CONTENT_MODE = CONTENT_MODE_URL
DEFAULT_PATH_PREFIX = ""

DEFAULT_VIDEO_EXTENSIONS = (
    "mkv mp4 avi ts m2ts mts iso rmvb wmv mov flv webm mpg mpeg m4v vob "
    "3gp f4v asf divx ogm ogv tp trp m2v mxf"
)
# 永远不会作为视频源处理的扩展名（避免 strm 套 strm）
_NEVER_EXTENSIONS = frozenset({"strm"})
_SPLIT = re.compile(r"[,;\s]+")


@dataclass(frozen=True, slots=True)
class TreeFile:
    """目录树中的一个文件节点。"""

    file_id: str
    name: str
    path: str
    size: int = 0
    modified_time: float = 0.0
    shortcut_target_id: str = ""


@dataclass(frozen=True, slots=True)
class StrmTarget:
    """一个待写出的 STRM 目标。

    ``url`` 保存最终写入 ``.strm`` 的内容：URL 模式下是 Drive 直链，
    路径模式下是本地挂载路径；字段名沿用 ``url`` 以兼容数据库列。
    """

    rel_path: str
    source_file_id: str
    source_path: str
    url: str
    size: int = 0
    modified_time: float = 0.0


def normalize_root_path(value: Any) -> str:
    """
    把根路径归一化为以 ``/`` 开头和结尾的 POSIX 路径。

    :param value: 用户配置或 tree_meta 中的根路径
    :return: 例如 ``/``、``/GDrive/``
    """
    text = str(value or "/").strip().replace("\\", "/")
    if not text:
        text = "/"
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    if not text.endswith("/"):
        text += "/"
    return text


def parse_extensions(raw: Any) -> FrozenSet[str]:
    """
    解析后缀白名单。

    :param raw: 逗号/分号/空白分隔的字符串，或字符串序列
    :return: 小写、不带前导点的后缀集合
    """
    if raw is None:
        return frozenset(DEFAULT_VIDEO_EXTENSIONS.split())
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts: Iterable[str] = (str(item) for item in raw)
    else:
        parts = _SPLIT.split(str(raw))
    extensions = set()
    for part in parts:
        text = str(part or "").strip().lower().lstrip(".")
        if text and text not in _NEVER_EXTENSIONS:
            extensions.add(text)
    return frozenset(extensions) or frozenset(
        DEFAULT_VIDEO_EXTENSIONS.split()
    )


def extension_of(name: Any) -> Optional[str]:
    """
    取文件名最后一个点之后的小写后缀。

    :param name: 文件名或路径
    :return: 后缀（不含点）；没有后缀时返回 None
    """
    text = str(name or "").strip().rstrip("/")
    base = text.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    suffix = base.rsplit(".", 1)[-1].strip().lower()
    return suffix or None


def is_video_name(name: Any, extensions: FrozenSet[str]) -> bool:
    """
    判断文件名是否命中视频后缀白名单。

    :param name: 文件名或路径
    :param extensions: 已归一化的后缀集合
    :return: 命中返回 True
    """
    suffix = extension_of(name)
    return bool(suffix and suffix in extensions)


def is_safe_rel_path(value: Any) -> bool:
    """
    判断相对路径是否可以安全地拼接到输出根下。

    :param value: POSIX 风格相对路径
    :return: 安全返回 True
    """
    text = str(value or "")
    if not text or "\x00" in text or text.startswith("/"):
        return False
    for segment in text.split("/"):
        if segment in ("", ".", ".."):
            return False
    return True


def strip_root_path(tree_path: Any, root_path: Any) -> Optional[str]:
    """
    去掉目录树根路径前缀，得到相对树路径。

    :param tree_path: 节点在树中的完整路径
    :param root_path: ``tree_meta.root_path``
    :return: 相对路径；根节点或越界路径返回 None
    """
    path = str(tree_path or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        path = "/" + path
    root = normalize_root_path(root_path)
    root_body = root.rstrip("/")
    if path.rstrip("/") == root_body:
        return None
    if root == "/":
        relative = path.lstrip("/")
    elif path.startswith(root):
        relative = path[len(root):]
    elif path.rstrip("/") + "/" == root:
        return None
    else:
        return None
    relative = relative.strip("/")
    return relative or None


def render_url(template: Any, file_id: Any) -> Optional[str]:
    """
    按模板渲染 STRM 内容。

    :param template: 必须包含 ``{fileId}`` 的模板
    :param file_id: Drive 文件 ID
    :return: URL；模板或 ID 无效时返回 None
    """
    pattern = str(template or "").strip()
    identifier = str(file_id or "").strip()
    if not pattern or not identifier or URL_PLACEHOLDER not in pattern:
        return None
    return pattern.replace(URL_PLACEHOLDER, identifier)


def join_path_prefix(prefix: Any, relative: Any) -> str:
    """
    把路径前缀与相对路径拼成 STRM 内容。

    - 反斜杠统一为 POSIX 斜杠，合并重复斜杠，去掉前缀末尾斜杠；
    - 空前缀（或仅 ``/``）等价于根目录，结果以 ``/`` 开头；
    - 非空前缀原样保留（允许 ``D:/Media`` 这类挂载点），不强制补前导斜杠。

    :param prefix: 用户配置的本地路径前缀
    :param relative: 去掉目录树根后的相对路径，例如 ``A/B/C/a.mkv``
    :return: 拼接后的内容路径
    """
    text = str(prefix or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    text = text.rstrip("/")
    rel = str(relative or "").strip().lstrip("/")
    if not text:
        return "/" + rel
    return f"{text}/{rel}"


def build_target(
    tree_file: TreeFile,
    root_path: Any,
    extensions: FrozenSet[str],
    url_template: str,
    *,
    content_mode: str = DEFAULT_CONTENT_MODE,
    path_prefix: str = DEFAULT_PATH_PREFIX,
) -> Optional[StrmTarget]:
    """
    把一个树文件映射为 STRM 目标。

    :param tree_file: 目录树文件节点
    :param root_path: 目录树根路径
    :param extensions: 视频后缀集合
    :param url_template: URL 模式下的内容模板
    :param content_mode: ``url`` 写直链；``path`` 写本地路径
    :param path_prefix: 路径模式下的本地挂载前缀
    :return: 目标；不匹配、路径非法或内容无法生成时返回 None
    """
    if not is_video_name(tree_file.name or tree_file.path, extensions):
        return None
    relative = strip_root_path(tree_file.path, root_path)
    if not relative or not is_safe_rel_path(relative):
        return None
    if content_mode == CONTENT_MODE_PATH:
        content: Optional[str] = join_path_prefix(path_prefix, relative)
    else:
        identifier = str(
            tree_file.shortcut_target_id or tree_file.file_id or ""
        ).strip()
        content = render_url(url_template, identifier)
    if not content:
        return None
    return StrmTarget(
        rel_path=relative.rsplit(".", 1)[0] + ".strm",
        source_file_id=str(tree_file.file_id or "").strip(),
        source_path=str(tree_file.path or ""),
        url=content,
        size=int(tree_file.size or 0),
        modified_time=float(tree_file.modified_time or 0.0),
    )


def build_targets(
    files: Iterable[TreeFile],
    root_path: Any,
    extensions: FrozenSet[str],
    url_template: str,
    *,
    content_mode: str = DEFAULT_CONTENT_MODE,
    path_prefix: str = DEFAULT_PATH_PREFIX,
) -> Dict[str, StrmTarget]:
    """
    构建 rel_path → 目标 的映射。

    :param files: 目录树文件节点
    :param root_path: 目录树根路径
    :param extensions: 视频后缀集合
    :param url_template: URL 模式下的内容模板
    :param content_mode: ``url`` 写直链；``path`` 写本地路径
    :param path_prefix: 路径模式下的本地挂载前缀
    :return: 以相对 STRM 路径为键的目标字典
    """
    targets: Dict[str, StrmTarget] = {}
    for tree_file in files:
        target = build_target(
            tree_file,
            root_path,
            extensions,
            url_template,
            content_mode=content_mode,
            path_prefix=path_prefix,
        )
        if target is not None:
            targets[target.rel_path] = target
    return targets
