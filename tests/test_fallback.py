from __future__ import annotations

import httpx
import pytest
import respx

from bili_audio.config import Settings
from bili_audio.errors import SchemaChangedError
from bili_audio.extractors import ExtractorChain
from bili_audio.extractors.bilibili import resolve_short_link
from bili_audio.extractors.ytdlp import _parse_entry
from bili_audio.models import MediaInfo
from bili_audio.urls import parse_video_url
from tests.helpers import make_stream

REF = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7")


class _PrimaryFails:
    async def extract(self, ref):
        raise SchemaChangedError("simulated upstream schema change")


class _FallbackWorks:
    async def extract(self, ref):
        return MediaInfo(
            canonical_url=ref.canonical_url,
            title="兜底视频",
            extractor="yt_dlp",
            streams=[make_stream()],
            warnings=["fallback warning"],
        )


async def test_chain_falls_back_on_schema_change(settings: Settings) -> None:
    chain = ExtractorChain.__new__(ExtractorChain)
    chain.settings = settings
    chain.primary = _PrimaryFails()
    chain.fallback = _FallbackWorks()
    result = await chain.extract(REF)
    assert result.used_fallback is True
    assert result.media.extractor == "yt_dlp"
    assert "官方接口异常" in " ".join(result.media.warnings)


async def test_b23_redirect_resolution(settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://b23.tv/abc123").mock(
            return_value=httpx.Response(
                302, headers={"location": "https://www.bilibili.com/video/BV1GJ411x7h7"}
            )
        )
        router.get("https://www.bilibili.com/video/BV1GJ411x7h7").mock(
            return_value=httpx.Response(200, text="<html>ok</html>")
        )
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resolved = await resolve_short_link(client, settings, "https://b23.tv/abc123")
    assert resolved.startswith("https://www.bilibili.com/video/BV1GJ411x7h7")


def test_ytdlp_entry_ranking(settings: Settings) -> None:
    entry = {
        "url": "https://example.invalid/a.m4s",
        "format_id": "30280",
        "ext": "m4a",
        "acodec": "mp4a.40.2",
        "vcodec": "none",
        "abr": 203.786,
        "http_headers": {"Referer": "https://www.bilibili.com/"},
    }
    stream = _parse_entry(entry, settings)
    assert stream is not None
    assert stream.quality_id == 30280
    assert stream.label == "192K AAC"
    assert stream.http_headers["Referer"] == "https://www.bilibili.com/"


@pytest.mark.parametrize(
    "codec, ext, lossless",
    [
        ("flac", "flac", True),
        ("ec-3", "m4a", False),
        ("mp4a.40.2", "m4a", False),
    ],
)
def test_ytdlp_codec_classification(settings: Settings, codec: str, ext: str, lossless: bool) -> None:
    entry = {
        "url": f"https://example.invalid/{ext}",
        "format_id": "30251" if lossless else "30280",
        "ext": ext,
        "acodec": codec,
        "vcodec": "none",
        "abr": 1000 if lossless else 200,
    }
    stream = _parse_entry(entry, settings)
    assert stream is not None
    assert stream.lossless is lossless
