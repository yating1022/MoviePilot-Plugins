"""strm_mapper 的纯逻辑单元测试。"""

from __future__ import annotations

from typing import Any

import pytest

from _gdrivestrm_loader import load_module

mapper = load_module("strm_mapper")
EXTENSIONS = mapper.parse_extensions(None)
URL = mapper.DEFAULT_URL_TEMPLATE


def _file(**overrides: Any) -> Any:
    """构造树文件。"""
    values = {
        "file_id": "file-1",
        "name": "a.mkv",
        "path": "/A/B/C/D/a.mkv",
        "size": 10,
        "modified_time": 1.0,
        "shortcut_target_id": "",
    }
    values.update(overrides)
    return mapper.TreeFile(**values)


def test_parse_default_extensions() -> None:
    """默认后缀覆盖常见视频格式，且不包含 strm。"""
    assert "mkv" in EXTENSIONS
    assert "mp4" in EXTENSIONS
    assert "iso" in EXTENSIONS
    assert "strm" not in EXTENSIONS


def test_parse_custom_extensions() -> None:
    """自定义后缀支持点、逗号、空白与大小写。"""
    parsed = mapper.parse_extensions(".MKV, mp4;mov\navi strm")
    assert parsed == frozenset({"mkv", "mp4", "mov", "avi"})


def test_normalize_root_path() -> None:
    """根路径统一为以斜杠开头结尾。"""
    assert mapper.normalize_root_path("") == "/"
    assert mapper.normalize_root_path("GDrive") == "/GDrive/"
    assert mapper.normalize_root_path("/GDrive//") == "/GDrive/"


def test_strip_root_path() -> None:
    """去掉根前缀并保留相对层级。"""
    assert mapper.strip_root_path("/A/B/a.mkv", "/") == "A/B/a.mkv"
    assert mapper.strip_root_path("/GDrive/A/B/a.mkv", "/GDrive/") == "A/B/a.mkv"
    assert mapper.strip_root_path("/GDrive/", "/GDrive/") is None
    assert mapper.strip_root_path("/Other/a.mkv", "/GDrive/") is None


def test_build_target_maps_example() -> None:
    """PRD 示例：/A/B/C/D/a.mkv → A/B/C/D/a.strm。"""
    target = mapper.build_target(_file(), "/", EXTENSIONS, URL)
    assert target is not None
    assert target.rel_path == "A/B/C/D/a.strm"
    assert target.url == (
        "https://www.googleapis.com/drive/v3/files/file-1"
        "?alt=media&supportsAllDrives=true"
    )


def test_build_target_strips_root_prefix() -> None:
    """树根为 /GDrive/ 时输出不含 GDrive 层。"""
    target = mapper.build_target(
        _file(path="/GDrive/Movies/a.mkv"), "/GDrive/", EXTENSIONS, URL
    )
    assert target is not None
    assert target.rel_path == "Movies/a.strm"


def test_build_target_skips_non_video() -> None:
    """非视频后缀不生成目标。"""
    assert mapper.build_target(_file(name="a.srt", path="/a.srt"), "/", EXTENSIONS, URL) is None
    assert mapper.build_target(
        _file(name="poster.jpg", path="/poster.jpg"), "/", EXTENSIONS, URL
    ) is None


def test_build_target_uses_shortcut_target() -> None:
    """文件快捷方式优先使用目标文件 ID。"""
    target = mapper.build_target(
        _file(shortcut_target_id="target-9"), "/", EXTENSIONS, URL
    )
    assert target is not None
    assert "target-9" in target.url


def test_build_target_rejects_unsafe_path() -> None:
    """越界相对路径被拒绝。"""
    assert mapper.is_safe_rel_path("../a.strm") is False
    assert mapper.is_safe_rel_path("A/../a.strm") is False
    assert mapper.is_safe_rel_path("A/B/a.strm") is True
    assert mapper.build_target(
        _file(path="/A/../a.mkv"), "/", EXTENSIONS, URL
    ) is None


def test_render_url_requires_placeholder() -> None:
    """模板缺少占位符或 ID 为空时返回 None。"""
    assert mapper.render_url(URL, "x") == URL.replace("{fileId}", "x")
    assert mapper.render_url("https://example.com/x", "x") is None
    assert mapper.render_url(URL, "") is None


def test_build_targets_deduplicates_by_rel_path() -> None:
    """同一相对路径只保留最后一个目标。"""
    targets = mapper.build_targets(
        [_file(file_id="one"), _file(file_id="two")], "/", EXTENSIONS, URL
    )
    assert list(targets) == ["A/B/C/D/a.strm"]
    assert targets["A/B/C/D/a.strm"].source_file_id == "two"


def test_join_path_prefix_normalizes() -> None:
    """路径前缀统一分隔符、去尾斜杠，并处理根目录与空值。"""
    assert (
        mapper.join_path_prefix("/home/googleDevice", "A/B/C/a.mkv")
        == "/home/googleDevice/A/B/C/a.mkv"
    )
    assert (
        mapper.join_path_prefix("/home/googleDevice/", "A/B/C/a.mkv")
        == "/home/googleDevice/A/B/C/a.mkv"
    )
    assert (
        mapper.join_path_prefix("\\home\\googleDevice\\", "A/B/C/a.mkv")
        == "/home/googleDevice/A/B/C/a.mkv"
    )
    assert (
        mapper.join_path_prefix("//home//googleDevice//", "A/B/C/a.mkv")
        == "/home/googleDevice/A/B/C/a.mkv"
    )
    assert mapper.join_path_prefix("/", "A/B/C/a.mkv") == "/A/B/C/a.mkv"
    assert mapper.join_path_prefix("", "A/B/C/a.mkv") == "/A/B/C/a.mkv"
    assert (
        mapper.join_path_prefix("D:/Media", "A/B/C/a.mkv")
        == "D:/Media/A/B/C/a.mkv"
    )


def test_build_target_path_mode_maps_example() -> None:
    """路径模式：前缀 + 相对树路径，忽略 URL 模板和文件 ID。"""
    target = mapper.build_target(
        _file(),
        "/",
        EXTENSIONS,
        "",
        content_mode=mapper.CONTENT_MODE_PATH,
        path_prefix="/home/googleDevice",
    )
    assert target is not None
    assert target.rel_path == "A/B/C/D/a.strm"
    assert target.url == "/home/googleDevice/A/B/C/D/a.mkv"


def test_build_target_path_mode_strips_root_prefix() -> None:
    """路径模式同样去掉目录树根路径，不包含虚拟根这一层。"""
    target = mapper.build_target(
        _file(path="/GDrive/Movies/a.mkv"),
        "/GDrive/",
        EXTENSIONS,
        "",
        content_mode=mapper.CONTENT_MODE_PATH,
        path_prefix="/home/googleDevice",
    )
    assert target is not None
    assert target.rel_path == "Movies/a.strm"
    assert target.url == "/home/googleDevice/Movies/a.mkv"


def test_build_target_path_mode_ignores_shortcut_target() -> None:
    """路径模式使用快捷方式节点自身路径，不使用目标文件 ID。"""
    target = mapper.build_target(
        _file(shortcut_target_id="target-9"),
        "/",
        EXTENSIONS,
        "",
        content_mode=mapper.CONTENT_MODE_PATH,
        path_prefix="/home/googleDevice",
    )
    assert target is not None
    assert target.url == "/home/googleDevice/A/B/C/D/a.mkv"


def test_build_target_path_mode_rejects_unsafe_path() -> None:
    """路径模式仍拒绝越界相对路径。"""
    assert (
        mapper.build_target(
            _file(path="/A/../a.mkv"),
            "/",
            EXTENSIONS,
            "",
            content_mode=mapper.CONTENT_MODE_PATH,
            path_prefix="/home/googleDevice",
        )
        is None
    )


def test_build_targets_path_mode() -> None:
    """build_targets 透传内容模式与前缀。"""
    targets = mapper.build_targets(
        [_file()],
        "/",
        EXTENSIONS,
        "",
        content_mode=mapper.CONTENT_MODE_PATH,
        path_prefix="/home/googleDevice",
    )
    assert targets["A/B/C/D/a.strm"].url == "/home/googleDevice/A/B/C/D/a.mkv"
