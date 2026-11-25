"""name_match 的纯逻辑测试。"""

from __future__ import annotations

from typing import Any, Dict, List

from _plugin_loader import load_module

match_module = load_module("name_match")


def _items(*names: str) -> List[Dict[str, Any]]:
    """构造云端子项。"""
    return [{"id": f"id-{index}", "name": name} for index, name in enumerate(names)]


def test_exact_match_uses_lowest_level() -> None:
    """原样相等时命中层级 0。"""
    result = match_module.match_child(_items("a.mkv", "b.mkv"), "a.mkv")
    assert result.matched
    assert result.level == match_module.MATCH_EXACT
    assert result.item["id"] == "id-0"


def test_full_width_punctuation_matches() -> None:
    """全角逗号/句号与半角等价。"""
    cloud = "明天，美食广场见。.2025.S01E02.第2集.mkv"
    archive = "明天,美食广场见..2025.S01E02.第2集.mkv"
    result = match_module.match_child(_items(cloud), archive)
    assert result.matched
    assert result.level == match_module.MATCH_NORMALIZED
    assert result.item["name"] == cloud


def test_case_difference_matches() -> None:
    """大小写差异通过 NFKC + casefold 匹配。"""
    result = match_module.match_child(_items("Show.S01E01.MKV"), "show.s01e01.mkv")
    assert result.matched
    assert result.level == match_module.MATCH_NORMALIZED


def test_whitespace_difference_matches() -> None:
    """首尾与连续空白差异由文档层归一化吸收。"""
    result = match_module.match_child(_items("  a   b.mkv"), "a b.mkv")
    assert result.matched
    assert result.level == match_module.MATCH_NORMALIZED


def test_space_removal_level_matches() -> None:
    """去掉全部空白后仍可匹配，且层级更高。"""
    result = match_module.match_child(_items("a b.mkv"), "ab.mkv")
    assert result.matched
    assert result.level == match_module.MATCH_NO_SPACE


def test_zero_width_characters_ignored() -> None:
    """零宽字符不参与比较。"""
    result = match_module.match_child(_items("a​.mkv"), "a.mkv")
    assert result.matched
    assert result.item["name"] == "a​.mkv"


def test_ambiguous_candidates_are_reported() -> None:
    """同层多个命中的候选返回歧义而不是任选。"""
    result = match_module.match_child(_items("Show。S01.mkv", "Show.S01.mkv"), "show.s01.mkv")
    assert result.matched is False
    assert set(result.ambiguous) == {"Show。S01.mkv", "Show.S01.mkv"}


def test_no_match_returns_empty() -> None:
    """完全没有候选时返回未匹配。"""
    result = match_module.match_child(_items("b.mkv"), "a.mkv")
    assert result.matched is False
    assert result.ambiguous == ()


def test_episode_numbers_are_not_conflated() -> None:
    """不做相似度匹配：第1集 不能匹配到 第2集（含全角数字折叠）。"""
    result = match_module.match_child(
        _items("Show.S01E02.第2集.mkv"), "Show.S01E01.第1集.mkv"
    )
    assert result.matched is False
    assert result.ambiguous == ()
    full_width = match_module.match_child(
        _items("Show.S01E01.第１集.mkv"), "Show.S01E01.第1集.mkv"
    )
    assert full_width.matched
    assert full_width.level == match_module.MATCH_NORMALIZED


def test_duplicate_exact_names_are_ambiguous() -> None:
    """同目录存在完全同名文件时也不任选。"""
    result = match_module.match_child(_items("a.mkv", "a.mkv"), "a.mkv")
    assert result.matched is False
    assert result.ambiguous == ("a.mkv", "a.mkv")


def test_empty_candidates() -> None:
    """空候选列表直接返回未匹配。"""
    result = match_module.match_child([], "a.mkv")
    assert result.matched is False


def test_candidate_names_limit() -> None:
    """候选名列表有数量上限。"""
    names = match_module.candidate_names(_items(*(f"f{i}" for i in range(9))), limit=3)
    assert names == ("f0", "f1", "f2")
