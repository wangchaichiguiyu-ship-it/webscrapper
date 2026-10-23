from __future__ import annotations

from bili_audio.quality import AudioStream


def make_stream(
    url: str = "https://example.invalid/audio-30280.m4s",
    *,
    quality_id: int = 30280,
    bandwidth: int = 203786,
    lossless: bool = False,
    container: str = "m4a",
    tier: int = 2,
    backups: list[str] | None = None,
) -> AudioStream:
    return AudioStream(
        urls=[url, *(backups or [])],
        quality_id=quality_id,
        label="192K AAC" if quality_id == 30280 else "test",
        bandwidth=bandwidth,
        mime_type="audio/mp4",
        codecs="mp4a.40.2",
        container=container,
        lossless=lossless,
        tier=tier,
        source="bilibili_api",
        note="test",
        http_headers={"Referer": "https://www.bilibili.com/"},
    )
