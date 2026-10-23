"""In-memory task queue and download/convert pipeline.

Why in-memory: the app is a local, single-user tool. Keeping queue state in RAM
avoids a database dependency, reduces idle memory and means pasted URLs/cookies
never linger on disk. Finished audio files persist; the UI simply starts empty
after a restart. This trade-off is documented in the README.

The pipeline is deliberately linear per task:

    resolve -> pick best stream -> download (with candidate fallback) -> convert
            -> atomically move into the output folder

Only one task runs by default (`BILI_MAX_CONCURRENT=1`), keeping the Air cool and
memory flat. Progress events are throttled to avoid flooding the SSE channel.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import Settings
from .converter import convert, output_extension
from .downloader import download
from .errors import (
    AppError,
    DownloadError,
    DuplicateTaskError,
    MediaExpiredError,
    PipelineCancelled,
    QueueFullError,
    TaskNotFoundError,
    TaskStateError,
)
from .extractors import ExtractorChain
from .models import (
    STAGE_LABELS,
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_CONVERTING,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RESOLVING,
    Task,
    stats_for,
    utc_now_iso,
)
from .naming import build_stem, is_inside, unique_path
from .quality import rank_streams, source_quality_note
from .urls import parse_video_url

logger = logging.getLogger(__name__)

_EVENT_QUEUE_SIZE = 64
_PUBLISH_INTERVAL = 0.35
_MAX_WARNING = 12


class EventBus:
    """Fan-out bus for Server-Sent Events. Subscribers are bounded queues."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A slow tab must not slow down downloads. Drop the oldest update;
                # task snapshots are idempotent, so the UI self-heals.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass


class TaskManager:
    def __init__(
        self,
        settings: Settings,
        chain: ExtractorChain,
        event_bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.chain = chain
        self.bus = event_bus or EventBus()
        self.tasks: OrderedDict[str, Task] = OrderedDict()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._reserved_paths: set[str] = set()
        self._last_publish: dict[str, float] = {}
        self._speed_state: dict[str, tuple[float, int, float]] = {}
        self._last_task_started = 0.0
        self._stopping = False

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self.settings.ensure_dirs()
        self._cleanup_work_dir()
        for index in range(self.settings.max_concurrent):
            self._workers.append(asyncio.create_task(self._worker(index), name=f"audio-worker-{index}"))

    async def stop(self) -> None:
        self._stopping = True
        for task in self.tasks.values():
            if task.is_active:
                task.cancel_event.set()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def _cleanup_work_dir(self) -> None:
        """Remove crash leftovers older than 24h (never touch a running task)."""
        cutoff = time.time() - 24 * 3600
        try:
            for path in self.settings.work_dir.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not clean work dir", exc_info=True)

    # -- public state -------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        tasks = [task.to_public() for task in self.tasks.values()]
        return {"tasks": tasks, "stats": stats_for(list(self.tasks.values()))}

    def get(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("任务不存在或已被移除")
        return task

    def get_public(self, task_id: str) -> dict[str, Any]:
        return self.get(task_id).to_public()

    def audio_path(self, task_id: str) -> Path:
        task = self.get(task_id)
        if task.status != STATUS_COMPLETED or not task.output_path:
            raise TaskStateError("音频尚未完成，暂不可播放")
        path = task.output_path
        if not path.is_file() or not is_inside(path, self.settings.output_dir):
            raise TaskNotFoundError("输出文件不存在，可能已被移动或删除")
        return path

    # -- queue mutations ----------------------------------------------------
    async def enqueue(self, raw_url: str, audio_format: str | None = None) -> Task:
        if len(self.tasks) >= self.settings.max_tasks:
            raise QueueFullError(
                f"队列已达上限（{self.settings.max_tasks} 条）",
                hint="请先清理已完成/失败的任务，或调大 BILI_MAX_TASKS。",
            )
        fmt = (audio_format or self.settings.default_format).lower()
        if fmt not in {"flac", "wav", "m4a"}:
            raise TaskStateError("不支持的输出格式")

        # Network resolution happens outside the lock so other UI actions stay snappy.
        ref = await self.chain.resolve_url(raw_url)

        async with self._lock:
            for existing in self.tasks.values():
                if existing.url_key == ref.video_key and (existing.is_active or existing.status == STATUS_QUEUED):
                    raise DuplicateTaskError(
                        "这个视频已经在队列里了",
                        extra={"task_id": existing.id},
                    )
            if len(self.tasks) >= self.settings.max_tasks:
                raise QueueFullError(f"队列已达上限（{self.settings.max_tasks} 条）")
            task = Task(
                url=ref.canonical_url,
                canonical_url=ref.canonical_url,
                format=fmt,
                url_key=ref.video_key,
            )
            self.tasks[task.id] = task
            await self._queue.put(task.id)
            self._publish(task, force=True)
            return task

    async def cancel(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status == STATUS_QUEUED:
            task.status = STATUS_CANCELED
            task.stage_label = STAGE_LABELS[STATUS_CANCELED]
            task.finished_at = utc_now_iso()
            task.progress = 0.0
            self._publish(task, force=True)
            return task
        if task.is_active:
            task.cancel_event.set()
            task.stage_label = "正在取消…"
            self._publish(task, force=True)
            return task
        raise TaskStateError("任务已经结束，无需取消")

    async def retry(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status not in {STATUS_FAILED, STATUS_CANCELED}:
            raise TaskStateError("只有失败或已取消的任务可以重试")
        task.reset_for_retry()
        self._last_publish.pop(task.id, None)
        self._speed_state.pop(task.id, None)
        await self._queue.put(task.id)
        self._publish(task, force=True)
        return task

    async def delete(self, task_id: str) -> None:
        task = self.get(task_id)
        if task.is_active:
            raise TaskStateError("任务正在进行中，请先取消再移除")
        self.tasks.pop(task_id, None)
        self._last_publish.pop(task_id, None)
        self._speed_state.pop(task_id, None)
        self.bus.publish({"type": "deleted", "id": task_id})

    async def clear_finished(self) -> int:
        removed = [
            task_id
            for task_id, task in self.tasks.items()
            if task.is_finished
        ]
        for task_id in removed:
            self.tasks.pop(task_id, None)
            self._last_publish.pop(task_id, None)
            self._speed_state.pop(task_id, None)
            self.bus.publish({"type": "deleted", "id": task_id})
        return len(removed)

    @staticmethod
    async def _await_or_cancel(awaitable, cancel_event: asyncio.Event):
        """Await a long operation, aborting promptly when the user cancels.

        httpx extraction/connect can otherwise sit in a 20-90s timeout after the
        user pressed cancel. Cancelling the task closes the underlying request.
        """
        work = asyncio.ensure_future(awaitable)
        waiter = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_event.is_set():
                if not work.done():
                    work.cancel()
                with suppress(BaseException):
                    await work
                raise PipelineCancelled()
            return await work
        finally:
            if not waiter.done():
                waiter.cancel()
            with suppress(BaseException):
                await waiter
            if not work.done():
                work.cancel()
                with suppress(BaseException):
                    await work

    # -- worker -------------------------------------------------------------
    async def _worker(self, index: int) -> None:
        logger.debug("worker %s started", index)
        while True:
            task_id = await self._queue.get()
            try:
                task = self.tasks.get(task_id)
                if task is None or task.status != STATUS_QUEUED:
                    continue
                if self.settings.safe_mode and self._last_task_started:
                    interval = self.settings.task_interval + random.uniform(0, 1.2)
                    elapsed = time.monotonic() - self._last_task_started
                    if elapsed < interval:
                        await asyncio.sleep(interval - elapsed)
                self._last_task_started = time.monotonic()
                await self._run_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - last-resort safety net
                logger.exception("Unexpected worker failure")
            finally:
                self._queue.task_done()

    async def _run_task(self, task: Task) -> None:
        task.status = STATUS_RESOLVING
        task.stage_label = STAGE_LABELS[STATUS_RESOLVING]
        task.progress = 1.0
        task.started_at = utc_now_iso()
        task.error = None
        self._publish(task, force=True)

        part_path: Path | None = None
        converted_path: Path | None = None
        try:
            if task.cancel_event.is_set():
                raise PipelineCancelled()

            ref = parse_video_url(task.canonical_url)
            result = await self._await_or_cancel(
                self.chain.extract(ref), task.cancel_event
            )
            media = result.media
            task.title = media.title
            task.uploader = media.uploader
            task.duration = media.duration
            task.page = media.page
            self._merge_warnings(task, media.warnings)
            if result.used_fallback and "官方接口异常" not in " ".join(task.warnings):
                self._add_warning(task, "已使用 yt-dlp 兼容解析")
            self._publish(task, force=True)

            streams = rank_streams(media.streams)
            if not streams:
                raise DownloadError("没有找到可下载的音频流")
            target_ext = output_extension(task.format, streams[0])
            task.progress = 4.0

            # Try candidates in quality order; only downgrade after real failure.
            last_error: Exception | None = None
            part_path = self.settings.work_dir / f"{task.id}.audio.part"
            for index, stream in enumerate(streams):
                if task.cancel_event.is_set():
                    raise PipelineCancelled()
                task.status = STATUS_DOWNLOADING
                task.selected_source = stream
                task.stage_label = f"下载音轨 · {stream.label}"
                if index > 0:
                    self._add_warning(
                        task,
                        f"最佳音轨下载失败，已降级到 {stream.label}",
                    )
                part_path.unlink(missing_ok=True)
                task.downloaded_bytes = 0
                task.total_bytes = None
                task.progress = 5.0
                self._speed_state[task.id] = (time.monotonic(), 0, 0.0)
                self._publish(task, force=True)
                try:
                    await self._await_or_cancel(
                        download(
                            stream,
                            part_path,
                            self.settings,
                            on_progress=lambda done, total, t=task: self._on_download_progress(t, done, total),
                            cancel_event=task.cancel_event,
                        ),
                        task.cancel_event,
                    )
                    last_error = None
                    break
                except (DownloadError, MediaExpiredError) as exc:
                    last_error = exc
                    logger.warning("Candidate %s failed for %s: %s", stream.quality_id, task.id, exc.message)
                    task.downloaded_bytes = 0
                    task.total_bytes = None
                    continue

            if last_error is not None:
                raise last_error
            if part_path is None or not part_path.exists():
                raise DownloadError("下载完成后找不到临时音频文件")

            if task.cancel_event.is_set():
                raise PipelineCancelled()

            task.status = STATUS_CONVERTING
            task.stage_label = STAGE_LABELS[STATUS_CONVERTING]
            task.progress = 85.0
            task.speed_bps = None
            task.eta_seconds = None
            target_ext = output_extension(task.format, task.selected_source) if task.selected_source else target_ext
            converted_path = self.settings.work_dir / f"{task.id}.converted.{target_ext}"
            self._publish(task, force=True)

            file_size, _probe = await convert(
                part_path,
                converted_path,
                task.format,
                task.selected_source,
                media,
                self.settings,
                on_progress=lambda ratio, t=task: self._on_convert_progress(t, ratio),
                cancel_event=task.cancel_event,
            )

            stem = build_stem(
                media.title,
                page=media.page,
                page_title=media.page_title,
            )
            async with self._lock:
                final_path = unique_path(
                    self.settings.output_dir,
                    stem,
                    target_ext,
                    reserved=self._reserved_paths,
                )
                self._reserved_paths.add(str(final_path))
            try:
                os.replace(converted_path, final_path)
            except OSError as exc:
                self._reserved_paths.discard(str(final_path))
                raise AppError(f"无法写入输出文件：{exc}") from exc
            self._reserved_paths.discard(str(final_path))
            converted_path = None

            task.output_path = final_path
            task.output_filename = final_path.name
            task.output_size = final_path.stat().st_size if final_path.exists() else file_size
            task.progress = 100.0
            task.status = STATUS_COMPLETED
            task.stage_label = STAGE_LABELS[STATUS_COMPLETED]
            task.finished_at = utc_now_iso()
            note = source_quality_note(task.selected_source, task.format) if task.selected_source else ""
            if note:
                self._add_warning(task, note)
            logger.info("Task completed: %s -> %s", task.id, final_path.name)
        except PipelineCancelled:
            task.mark_canceled()
            logger.info("Task canceled: %s", task.id)
        except asyncio.CancelledError:
            task.mark_canceled()
            raise
        except AppError as exc:
            task.fail(exc.user_message)
            logger.warning("Task failed (%s): %s", exc.code, exc.message)
        except Exception:
            logger.exception("Unexpected task failure: %s", task.id)
            task.fail("处理失败：发生了未预期的内部错误（详情见终端日志）")
        finally:
            self._cleanup_paths(part_path, converted_path)
            self._speed_state.pop(task.id, None)
            if task.status in {STATUS_QUEUED, STATUS_RESOLVING, STATUS_DOWNLOADING, STATUS_CONVERTING}:
                # Defensive: never leave a task visually stuck.
                if task.cancel_event.is_set():
                    task.mark_canceled()
                else:
                    task.fail("任务意外中止")
            task.speed_bps = None
            task.eta_seconds = None
            self._publish(task, force=True)

    def _cleanup_paths(self, *paths: Path | None) -> None:
        for path in paths:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Could not remove temp file %s", path, exc_info=True)

    # -- progress -----------------------------------------------------------
    def _on_download_progress(self, task: Task, downloaded: int, total: int | None) -> None:
        now = time.monotonic()
        task.downloaded_bytes = downloaded
        task.total_bytes = total
        last_time, last_bytes, speed = self._speed_state.get(
            task.id, (now, downloaded, 0.0)
        )
        elapsed = now - last_time
        if elapsed >= 0.5:
            instant = max(0.0, (downloaded - last_bytes) / elapsed)
            speed = instant if speed <= 0 else (speed * 0.7 + instant * 0.3)
            last_time, last_bytes = now, downloaded
            task.speed_bps = speed if speed > 0 else None
            if total and speed > 0:
                task.eta_seconds = max(0.0, (total - downloaded) / speed)
            else:
                task.eta_seconds = None
            self._speed_state[task.id] = (last_time, last_bytes, speed)
        if total and total > 0:
            task.progress = 5.0 + min(downloaded / total, 1.0) * 80.0
        else:
            task.progress = max(task.progress, 5.0)
        self._publish(task)

    def _on_convert_progress(self, task: Task, ratio: float) -> None:
        task.progress = 85.0 + max(0.0, min(ratio, 1.0)) * 14.5
        self._publish(task)

    # -- events -------------------------------------------------------------
    def _publish(self, task: Task, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish.get(task.id, 0.0) < _PUBLISH_INTERVAL:
            return
        self._last_publish[task.id] = now
        if not self._stopping:
            self.bus.publish({"type": "task", "task": task.to_public()})

    # -- warnings -----------------------------------------------------------
    @staticmethod
    def _add_warning(task: Task, message: str) -> None:
        message = (message or "").strip()
        if not message or message in task.warnings:
            return
        if len(task.warnings) >= _MAX_WARNING:
            return
        task.warnings.append(message)

    def _merge_warnings(self, task: Task, messages: list[str]) -> None:
        for message in messages:
            self._add_warning(task, message)
