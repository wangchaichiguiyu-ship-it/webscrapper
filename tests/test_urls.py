from __future__ import annotations

import pytest

from bili_audio.errors import UnsupportedUrlError
from bili_audio.urls import (
    extract_first_url,
    is_probable_bilibili_url,
    is_short_link,
    parse_video_url,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("BV1GJ411x7h7", "https://www.bilibili.com/video/BV1GJ411x7h7"),
        ("av80433022", "https://www.bilibili.com/video/av80433022"),
        (
            "看这个 https://www.bilibili.com/video/BV1GJ411x7h7?p=2 很好听。",
            "https://www.bilibili.com/video/BV1GJ411x7h7?p=2",
        ),
        ("https://b23.tv/abcdefg", "https://b23.tv/abcdefg"),
        ("https://m.bilibili.com/video/BV1GJ411x7h7", "https://m.bilibili.com/video/BV1GJ411x7h7"),
    ],
)
def test_extract_first_url(text: str, expected: str) -> None:
    assert extract_first_url(text) == expected


def test_parse_pages_and_bv() -> None:
    ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=3&vd_source=x")
    assert ref.bvid == "BV1GJ411x7h7"
    assert ref.page == 3
    assert ref.canonical_url.endswith("BV1GJ411x7h7?p=3")
    assert ref.video_key == "bili:BV1GJ411x7h7:p3"


def test_parse_av() -> None:
    ref = parse_video_url("https://www.bilibili.com/video/av80433022")
    assert ref.aid == 80433022
    assert ref.bvid is None
    assert "av80433022" in ref.canonical_url


def test_b23_detected() -> None:
    assert is_short_link("https://b23.tv/abc123")
    assert not is_short_link("https://www.bilibili.com/video/BV1GJ411x7h7")


def test_bangumi_goes_to_fallback() -> None:
    ref = parse_video_url("https://www.bilibili.com/bangumi/play/ep123456")
    assert ref.kind == "bangumi"


def test_non_bilibili_rejected() -> None:
    assert not is_probable_bilibili_url("https://example.com/video/BV1GJ411x7h7")
    with pytest.raises(UnsupportedUrlError):
        parse_video_url("https://evilbilibili.com/video/BV1GJ411x7h7")


def test_unsupported_page_rejected() -> None:
    with pytest.raises(UnsupportedUrlError):
        parse_video_url("https://www.bilibili.com/read/cv12345")


def test_aid_query_variant() -> None:
    ref = parse_video_url("https://www.bilibili.com/video?bvid=BV1GJ411x7h7&p=2")
    assert ref.bvid == "BV1GJ411x7h7"
    assert ref.page == 2


def test_page_is_sanitized() -> None:
    ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=0")
    assert ref.page == 1
    ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=banana")
    assert ref.page == 1
