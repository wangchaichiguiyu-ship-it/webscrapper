from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from bili_audio.config import Settings
from bili_audio.downloader import download
from bili_audio.errors import MediaExpiredError
from tests.helpers import make_stream

AUDIO = b"0123456789abcdef" * 64  # 1024 bytes


async def test_streams_to_file(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "a.part"
    events: list[tuple[int, int | None]] = []
    with respx.mock:
        respx.get("https://example.invalid/a.m4s").mock(
            return_value=httpx.Response(
                200, content=AUDIO, headers={"content-type": "audio/mp4"}
            )
        )
        stream = make_stream("https://example.invalid/a.m4s")
        n, total = await download(
            stream,
            destination,
            settings,
            on_progress=lambda done, t: events.append((done, t)),
        )
    assert n == len(AUDIO)
    assert total == len(AUDIO)
    assert destination.read_bytes() == AUDIO
    assert events[-1][0] == len(AUDIO)


async def test_range_resume(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "a.part"
    destination.write_bytes(AUDIO[:400])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("range") == "bytes=400-"
        return httpx.Response(
            206,
            content=AUDIO[400:],
            headers={
                "content-type": "audio/mp4",
                "content-range": f"bytes 400-{len(AUDIO) - 1}/{len(AUDIO)}",
            },
        )

    with respx.mock:
        respx.get("https://example.invalid/a.m4s").mock(side_effect=handler)
        await download(make_stream("https://example.invalid/a.m4s"), destination, settings)
    assert destination.read_bytes() == AUDIO


async def test_backup_url_used(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "a.part"
    with respx.mock:
        respx.get("https://example.invalid/primary.m4s").mock(
            return_value=httpx.Response(403, content=b"denied")
        )
        respx.get("https://example.invalid/backup.m4s").mock(
            return_value=httpx.Response(
                200, content=AUDIO, headers={"content-type": "audio/mp4"}
            )
        )
        stream = make_stream(
            "https://example.invalid/primary.m4s",
            backups=["https://example.invalid/backup.m4s"],
        )
        n, _ = await download(stream, destination, settings)
    assert n == len(AUDIO)
    assert destination.read_bytes() == AUDIO


async def test_html_response_is_media_expired(settings: Settings, tmp_path: Path) -> None:
    destination = tmp_path / "a.part"
    with respx.mock:
        respx.get("https://example.invalid/a.m4s").mock(
            return_value=httpx.Response(
                200, content=b"<html>risk control</html>", headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(MediaExpiredError):
            await download(make_stream("https://example.invalid/a.m4s"), destination, settings)
    assert not destination.exists()
