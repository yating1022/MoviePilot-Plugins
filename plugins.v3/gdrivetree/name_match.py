"""归档路径中的名称与云端子项名称的等价匹配。

只做等价类归一化，不做相似度：同层出现多个候选时返回歧义，交由调用方报错，
避免把 A 文件误判成 B 文件写进目录树。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

MATCH_EXACT = 0
MATCH_NORMALIZED = 1
MATCH_NO_SPACE = 2

# 零宽与不可见控制字符：NFKC 不会移除它们，但会干扰名称比较
_INVISIBLE = frozenset(
    {
        "​",  # zero width space
        "‌",  # zero width non-joiner
        "‍",  # zero width joiner
        "‎",  # left-to-right mark
        "‏",  # right-to-left mark
        "⁠",  # word joiner
        "﻿",  # zero width no-break space
        "­",  # soft hyphen
    }
)

# NFKC 不会折叠的 CJK 标点：命名模板与云端常在中式标点和半角标点之间摇摆。
# 半角形式（｡､）已由 NFKC 折叠为对应的 CJK 标点，这里只需覆盖全角形态。
_PUNCTUATION = str.maketrans(
    {
        "。": ".",  # U+3002 IDEOGRAPHIC FULL STOP
        "、": ",",  # U+3001 IDEOGRAPHIC COMMA
    }
)


@dataclass(frozen=True, slots=True)
class NameMatch:
    """一次名称匹配的结果。"""

    level: int
    item: Optional[dict] = None
    ambiguous: Tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        """是否唯一命中。"""
        return self.item is not None


def normalize_name(name: Any, level: int = MATCH_NORMALIZED) -> str:
    """
    把名称归一化为用于等价比较的文本。

    :param name: 原始名称
    :param level: ``MATCH_EXACT`` 原样；``MATCH_NORMALIZED`` 折叠全角/大小写/
        CJK 标点/零宽字符，并裁剪首尾空白、折叠内部连续空白；
        ``MATCH_NO_SPACE`` 在上者基础上删除全部空白
    :return: 归一化文本
    """
    text = str(name or "")
    if level <= MATCH_EXACT:
        return text
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCTUATION)
    text = "".join(char for char in text if char not in _INVISIBLE)
    # 首尾修剪后再把内部连续空白折叠为单个空格：
    # "a   b" 与 "a b" 在命名模板下视为同一名字，但 "ab" 不属于本层
    text = " ".join(text.casefold().split())
    if level >= MATCH_NO_SPACE:
        text = text.replace(" ", "")
    return text


def _name_of(item: Any) -> str:
    """读取候选项名称。"""
    if isinstance(item, Mapping):
        return str(item.get("name") or "")
    return str(getattr(item, "name", "") or "")


def match_child(
    candidates: Sequence[Any], wanted: Any
) -> NameMatch:
    """
    在候选子项中按归一化层级查找唯一匹配。

    层级从 ``MATCH_EXACT`` 到 ``MATCH_NO_SPACE`` 依次尝试；某一层有唯一命中即
    返回该层结果，出现多个命中则返回歧义且不再向更宽松的层级下探。

    :param candidates: 目录下的子项（字典或含 ``name`` 属性的对象）
    :param wanted: 归档路径中的名称
    :return: 匹配结果
    """
    if not candidates:
        return NameMatch(level=MATCH_EXACT)
    for level in (MATCH_EXACT, MATCH_NORMALIZED, MATCH_NO_SPACE):
        key = normalize_name(wanted, level)
        if level != MATCH_EXACT and not key:
            continue
        hits = [
            item
            for item in candidates
            if normalize_name(_name_of(item), level) == key
        ]
        if not hits:
            continue
        if len(hits) == 1:
            return NameMatch(level=level, item=hits[0])
        return NameMatch(
            level=level,
            ambiguous=tuple(_name_of(item) for item in hits),
        )
    return NameMatch(level=MATCH_EXACT)


def candidate_names(candidates: Sequence[Any], limit: int = 5) -> Tuple[str, ...]:
    """
    取候选名称列表，用于错误信息。

    :param candidates: 目录下的子项
    :param limit: 最多返回数量
    :return: 名称元组
    """
    names = [_name_of(item) for item in candidates]
    return tuple(name for name in names[: max(0, limit)] if name)
