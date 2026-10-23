from __future__ import annotations

from pathlib import Path

import pytest

from bili_audio.naming import (
    build_stem,
    extension_for,
    is_inside,
    sanitize_filename,
    unique_path,
)


@pytest.mark.parametrize(
    "raw, forbidden",
    [
        ("a/b:c*d?e\"f<g>h|i", "/:*?\"<>|"),
        ("  ..hidden..  ", None),
        ("line\nbreak", "\n"),
    ],
)
def test_sanitize_removes_unsafe(raw: str, forbidden: str | None) -> None:
    cleaned = sanitize_filename(raw)
    assert cleaned
    if forbidden:
        assert not any(ch in cleaned for ch in forbidden)
    assert not cleaned.startswith(".")


def test_long_chinese_title_fits_apfs_bytes() -> None:
    cleaned = sanitize_filename("测试标题" * 100)
    assert len(cleaned.encode("utf-8")) <= 200


def test_empty_falls_back() -> None:
    assert sanitize_filename("...") == "未命名音频"


def test_multipart_stem() -> None:
    assert build_stem("标题", page=1) == "标题"
    stem = build_stem("标题", page=2, page_title="第二集")
    assert stem.startswith("标题 - P2")
    assert "第二集" in stem


def test_extension_policy() -> None:
    assert extension_for("flac", "m4a", False) == "flac"
    assert extension_for("wav", "m4a", False) == "wav"
    assert extension_for("m4a", "m4a", False) == "m4a"
    assert extension_for("m4a", "flac", True) == "flac"


def test_unique_path(tmp_path: Path) -> None:
    (tmp_path / "a.flac").write_bytes(b"x")
    first = unique_path(tmp_path, "a", "flac")
    assert first.name == "a (2).flac"
    second = unique_path(tmp_path, "a", "flac", reserved={str(first)})
    assert second.name == "a (3).flac"


def test_is_inside(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.flac"
    assert is_inside(tmp_path / "a.flac", tmp_path)
    assert not is_inside(outside, tmp_path)
