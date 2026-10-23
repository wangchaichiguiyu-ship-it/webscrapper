from __future__ import annotations

import json
from pathlib import Path

import pytest

from bili_audio.config import Settings
from bili_audio.errors import SchemaChangedError
from bili_audio.extractors.bilibili import parse_playurl
from bili_audio.quality import (
    TIER_DOLBY,
    TIER_REGULAR,
    AudioStream,
    best_stream,
    rank_streams,
    source_quality_note,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_regular_prefers_available_192k(settings: Settings) -> None:
    streams = parse_playurl(load("playurl_regular.json"), settings=settings, has_cookie=False)
    assert len(streams) == 3
    assert streams[0].quality_id == 30280
    assert streams[0].lossless is False
    assert streams[0].label == "192K AAC"


def test_hires_flac_wins(settings: Settings) -> None:
    streams = parse_playurl(load("playurl_flac.json"), settings=settings, has_cookie=True)
    best = best_stream(streams)
    assert best is not None
    assert best.quality_id == 30251
    assert best.lossless is True
    assert best.container == "flac"
    # Dolby is the second choice, regular AAC third.
    assert [s.quality_id for s in streams] == [30251, 30250, 30280]


def test_snake_case_is_tolerated(settings: Settings) -> None:
    payload = {
        "dash": {
            "audio": [
                {
                    "id": 30280,
                    "base_url": "https://example.invalid/a.m4s",
                    "backup_url": ["https://example.invalid/b.m4s"],
                    "bandwidth": 200000,
                    "mime_type": "audio/mp4",
                }
            ]
        }
    }
    streams = parse_playurl(payload, settings=settings, has_cookie=True)
    assert streams[0].urls == [
        "https://example.invalid/a.m4s",
        "https://example.invalid/b.m4s",
    ]


def test_durl_fallback(settings: Settings) -> None:
    payload = {"quality": 64, "durl": [{"url": "https://example.invalid/legacy.mp4", "size": 123}]}
    streams = parse_playurl(payload, settings=settings, has_cookie=False)
    assert streams[0].is_muxed is True
    assert streams[0].tier == 3


def test_missing_streams_raises_schema_error(settings: Settings) -> None:
    with pytest.raises(SchemaChangedError):
        parse_playurl({}, settings=settings, has_cookie=False)


def test_ranking_of_manual_candidates() -> None:
    lossless = AudioStream(
        urls=["https://x/flac"],
        quality_id=30251,
        label="Hi-Res",
        bandwidth=1_411_200,
        mime_type="audio/flac",
        codecs="flac",
        container="flac",
        lossless=True,
        tier=0,
        source="test",
    )
    dolby = AudioStream(
        urls=["https://x/dolby"],
        quality_id=30250,
        label="Dolby",
        bandwidth=448_000,
        mime_type="audio/mp4",
        codecs="ec-3",
        container="m4a",
        lossless=False,
        tier=TIER_DOLBY,
        source="test",
    )
    regular = AudioStream(
        urls=["https://x/aac"],
        quality_id=30280,
        label="192K",
        bandwidth=203_786,
        mime_type="audio/mp4",
        codecs="mp4a.40.2",
        container="m4a",
        lossless=False,
        tier=TIER_REGULAR,
        source="test",
    )
    ranked = rank_streams([regular, dolby, lossless, regular])
    assert [s.quality_id for s in ranked] == [30251, 30250, 30280]


def test_honest_quality_note() -> None:
    stream = AudioStream(
        urls=["https://x/a"],
        quality_id=30280,
        label="192K AAC",
        bandwidth=203786,
        mime_type="audio/mp4",
        codecs="mp4a.40.2",
        container="m4a",
        lossless=False,
        tier=2,
        source="test",
    )
    note = source_quality_note(stream, "flac")
    assert "不会提升" in note


def test_missing_flac_entitlement_warns(settings: Settings) -> None:
    payload = load("playurl_flac.json")
    payload["dash"]["flac"]["audio"] = None
    payload["dash"]["flac"]["display"] = True
    streams = parse_playurl(payload, settings=settings, has_cookie=False)
    assert streams[0].quality_id == 30250
    # The warning list is passed by reference; capture it explicitly.
    warnings: list[str] = []
    parse_playurl(payload, settings=settings, has_cookie=False, warnings=warnings)
    assert any("Hi-Res" in w for w in warnings)
