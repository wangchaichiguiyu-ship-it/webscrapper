from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from bili_audio.config import Settings
from bili_audio.errors import SchemaChangedError, UpstreamError
from bili_audio.extractors.bilibili import BilibiliApiExtractor, parse_playurl
from bili_audio.urls import parse_video_url

FIXTURES = Path(__file__).parent / "fixtures"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAY_URL = "https://api.bilibili.com/x/player/playurl"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def envelope(data: dict) -> dict:
    return {"code": 0, "message": "OK", "ttl": 1, "data": data}


@pytest.fixture()
def ref():
    return parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7")


async def test_extract_regular(settings: Settings, ref) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(VIEW_URL).mock(return_value=httpx.Response(200, json=envelope(load("view.json"))))
        router.get(PLAY_URL).mock(
            return_value=httpx.Response(200, json=envelope(load("playurl_regular.json")))
        )
        async with httpx.AsyncClient() as client:
            extractor = BilibiliApiExtractor(client, settings)
            media = await extractor.extract(ref)
    assert "Never Gonna Give You Up" in media.title
    assert media.uploader == "索尼音乐中国"
    assert media.duration == pytest.approx(213)
    assert media.streams[0].quality_id == 30280
    assert media.extractor == "bilibili_api"


async def test_extract_hires_flac(settings: Settings, ref) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(VIEW_URL).mock(return_value=httpx.Response(200, json=envelope(load("view.json"))))
        router.get(PLAY_URL).mock(
            return_value=httpx.Response(200, json=envelope(load("playurl_flac.json")))
        )
        async with httpx.AsyncClient() as client:
            media = await BilibiliApiExtractor(client, settings).extract(ref)
    assert media.streams[0].quality_id == 30251
    assert media.streams[0].lossless is True


async def test_api_error_is_friendly(settings: Settings, ref) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(VIEW_URL).mock(
            return_value=httpx.Response(200, json={"code": -404, "message": "啥都木有", "data": None})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(UpstreamError) as exc:
                await BilibiliApiExtractor(client, settings).extract(ref)
    assert "视频不存在" in exc.value.message


async def test_missing_dash_triggers_schema_error(settings: Settings, ref) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(VIEW_URL).mock(return_value=httpx.Response(200, json=envelope(load("view.json"))))
        router.get(PLAY_URL).mock(return_value=httpx.Response(200, json=envelope({"quality": 64})))
        async with httpx.AsyncClient() as client:
            with pytest.raises(SchemaChangedError):
                await BilibiliApiExtractor(client, settings).extract(ref)


async def test_page_out_of_range(settings: Settings) -> None:
    ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=99")
    with respx.mock(assert_all_called=False) as router:
        router.get(VIEW_URL).mock(return_value=httpx.Response(200, json=envelope(load("view.json"))))
        async with httpx.AsyncClient() as client:
            with pytest.raises(Exception) as exc:
                await BilibiliApiExtractor(client, settings).extract(ref)
    assert "分P" in str(exc.value)


def test_parse_urls_are_absolute(settings: Settings) -> None:
    streams = parse_playurl(load("playurl_flac.json"), settings=settings, has_cookie=True)
    for stream in streams:
        assert all(url.startswith("https://") for url in stream.urls)
