from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bili_audio.config import Settings
from bili_audio.errors import DownloadError, DuplicateTaskError
from bili_audio.extractors import ExtractResult
from bili_audio.models import MediaInfo
from bili_audio.naming import build_stem
from bili_audio.quality import TIER_LOSSLESS, AudioStream
from bili_audio.tasks import TaskManager
from bili_audio.urls import VideoRef, parse_video_url
from tests.helpers import make_stream


class FakeChain:
    def __init__(self, *, title: str = "测试视频", slow: float = 0.0) -> None:
        self.title = title
        self.slow = slow
        self.ref = parse_video_url("https://www.bilibili.com/video/BV1GJ411x7h7")

    async def resolve_url(self, raw: str):
        if self.slow:
            await asyncio.sleep(self.slow)
        return self.ref

    async def extract(self, ref: VideoRef) -> ExtractResult:
        media = MediaInfo(
            canonical_url=ref.canonical_url,
            title=self.title,
            uploader="UP主",
            duration=10.0,
            extractor="bilibili_api",
            streams=[make_stream()],
        )
        return ExtractResult(media=media, ref=ref)


def fake_download(stream, destination: Path, settings, **kwargs):
    async def _run():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-audio")
        cb = kwargs.get("on_progress")
        if cb:
            cb(5, 10)
            cb(10, 10)
        return 10, 10

    return _run()


def fake_convert(input_path, output_path, target_format, source, media, settings, **kwargs):
    async def _run():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-flac")
        cb = kwargs.get("on_progress")
        if cb:
            cb(1.0)
        return len(b"fake-flac"), None

    return _run()


async def wait_finished(manager: TaskManager, task_id: str, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = manager.get(task_id)
        if task.is_finished:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} did not finish, status={manager.get(task_id).status}")


async def test_pipeline_completes(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("bili_audio.tasks.download", fake_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    manager = TaskManager(settings, FakeChain())
    await manager.start()
    try:
        task = await manager.enqueue("BV1GJ411x7h7", "flac")
        finished = await wait_finished(manager, task.id)
    finally:
        await manager.stop()
    assert finished.status == "completed"
    assert finished.progress == 100.0
    assert finished.output_filename == "测试视频.flac"
    assert (settings.output_dir / "测试视频.flac").read_bytes() == b"fake-flac"
    assert finished.output_size == len(b"fake-flac")
    assert not list(settings.work_dir.iterdir())
    assert manager.snapshot()["stats"]["completed"] == 1


async def test_best_stream_is_used(settings: Settings, monkeypatch) -> None:
    chosen: list[int] = []

    def capturing_download(stream, destination, settings, **kwargs):
        chosen.append(stream.quality_id)
        return fake_download(stream, destination, settings, **kwargs)

    class MultiChain(FakeChain):
        async def extract(self, ref):
            media = MediaInfo(
                canonical_url=ref.canonical_url,
                title="多音轨",
                duration=10,
                extractor="bilibili_api",
                streams=[
                    make_stream("https://example.invalid/64.m4s", quality_id=30216, tier=2),
                    AudioStream(
                        urls=["https://example.invalid/flac"],
                        quality_id=30251,
                        label="Hi-Res 无损",
                        bandwidth=1_411_200,
                        mime_type="audio/flac",
                        codecs="flac",
                        container="flac",
                        lossless=True,
                        tier=TIER_LOSSLESS,
                        source="test",
                    ),
                ],
            )
            return ExtractResult(media=media, ref=ref)

    monkeypatch.setattr("bili_audio.tasks.download", capturing_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    manager = TaskManager(settings, MultiChain())
    await manager.start()
    try:
        task = await manager.enqueue("BV1GJ411x7h7", "flac")
        finished = await wait_finished(manager, task.id)
    finally:
        await manager.stop()
    assert chosen == [30251]
    assert finished.selected_source.quality_id == 30251


async def test_duplicate_queued_rejected(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("bili_audio.tasks.download", fake_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    manager = TaskManager(settings, FakeChain())  # not started: stays queued
    await manager.enqueue("BV1GJ411x7h7")
    with pytest.raises(DuplicateTaskError):
        await manager.enqueue("https://www.bilibili.com/video/BV1GJ411x7h7?p=1")


async def test_cancel_queued(settings: Settings, monkeypatch) -> None:
    manager = TaskManager(settings, FakeChain())
    task = await manager.enqueue("BV1GJ411x7h7")
    canceled = await manager.cancel(task.id)
    assert canceled.status == "canceled"
    assert manager.snapshot()["stats"]["canceled"] == 1


async def test_retry_after_download_failure(settings: Settings, monkeypatch) -> None:
    attempts = {"n": 0}

    def flaky_download(stream, destination, settings, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise DownloadError("temporary failure")

        return fake_download(stream, destination, settings, **kwargs)

    monkeypatch.setattr("bili_audio.tasks.download", flaky_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    manager = TaskManager(settings, FakeChain())
    await manager.start()
    try:
        task = await manager.enqueue("BV1GJ411x7h7", "flac")
        failed = await wait_finished(manager, task.id)
        assert failed.status == "failed"
        retried = await manager.retry(task.id)
        assert retried.attempt == 2
        completed = await wait_finished(manager, task.id)
    finally:
        await manager.stop()
    assert completed.status == "completed"


async def test_same_title_gets_suffix(settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr("bili_audio.tasks.download", fake_download)
    monkeypatch.setattr("bili_audio.tasks.convert", fake_convert)
    manager = TaskManager(settings, FakeChain())
    await manager.start()
    try:
        first = await manager.enqueue("BV1GJ411x7h7", "flac")
        await wait_finished(manager, first.id)
        second = await manager.enqueue("https://www.bilibili.com/video/BV1GJ411x7h7?p=2", "flac")
        second_done = await wait_finished(manager, second.id)
    finally:
        await manager.stop()
    assert second_done.output_filename == "测试视频 (2).flac"


def test_build_stem_used_for_multipart(settings: Settings) -> None:
    stem = build_stem("标题", 2, "第二集")
    assert "P2" in stem


async def test_cancel_interrupts_in_flight_extract(settings: Settings) -> None:
    class SlowExtractChain(FakeChain):
        async def extract(self, ref):
            await asyncio.sleep(30)
            raise AssertionError("should have been cancelled")

    manager = TaskManager(settings, SlowExtractChain())
    await manager.start()
    try:
        task = await manager.enqueue("BV1GJ411x7h7", "flac")
        for _ in range(100):
            if manager.get(task.id).status == "resolving":
                break
            await asyncio.sleep(0.01)
        await manager.cancel(task.id)
        canceled = await wait_finished(manager, task.id, timeout=2.0)
    finally:
        await manager.stop()
    assert canceled.status == "canceled"
