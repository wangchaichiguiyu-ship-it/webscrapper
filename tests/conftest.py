from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bili_audio.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    s = Settings(
        output_dir=tmp_path / "output",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        config_dir=tmp_path / "config",
        safe_mode=False,
        task_interval=0,
        max_concurrent=1,
        max_tasks=20,
    )
    s.validate()
    s.ensure_dirs()
    return s
