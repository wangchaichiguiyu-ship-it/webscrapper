from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from bili_audio.config import Settings
from bili_audio.converter import convert, probe_audio
from bili_audio.models import MediaInfo
from bili_audio.naming import extension_for
from tests.helpers import make_stream


def _make_tone(path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.8",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
    )


async def test_convert_to_flac(settings: Settings, tmp_path: Path) -> None:
    source_path = tmp_path / "tone.m4a"
    _make_tone(source_path)
    output = tmp_path / "out.flac"
    stream = make_stream()
    media = MediaInfo(
        canonical_url="https://www.bilibili.com/video/BV1GJ411x7h7",
        title="测试标题",
        uploader="测试UP",
        duration=0.8,
    )
    events: list[float] = []
    size, probe = await convert(
        source_path,
        output,
        "flac",
        stream,
        media,
        settings,
        on_progress=events.append,
    )
    assert output.exists()
    assert size == output.stat().st_size
    assert probe.codec in {"aac", "flac", ""}
    assert events and events[-1] == pytest.approx(1.0)
    probed = await probe_audio(output, settings)
    assert probed.codec == "flac"
    assert probed.duration > 0
    assert extension_for("flac", "m4a", False) == "flac"
