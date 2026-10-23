"""Domain models shared by the extractor, pipeline and HTTP API."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .quality import AudioStream

STATUS_QUEUED = "queued"
STATUS_RESOLVING = "resolving"
STATUS_DOWNLOADING = "downloading"
STATUS_CONVERTING = "converting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

ACTIVE_STATUSES = {STATUS_RESOLVING, STATUS_DOWNLOADING, STATUS_CONVERTING}
FINISHED_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}

STAGE_LABELS = {
    STATUS_QUEUED: "排队中",
    STATUS_RESOLVING: "解析视频信息",
    STATUS_DOWNLOADING: "下载最佳音轨",
    STATUS_CONVERTING: "转换 / 封装",
    STATUS_COMPLETED: "已完成",
    STATUS_FAILED: "失败",
    STATUS_CANCELED: "已取消",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    return "t_" + uuid.uuid4().hex[:12]


@dataclass
class MediaInfo:
    canonical_url: str
    title: str
    uploader: str = ""
    duration: float = 0.0
    page: int = 1
    page_title: str = ""
    total_pages: int = 1
    cover_url: str | None = None
    extractor: str = ""
    streams: list[AudioStream] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def stream_ranked(self) -> list[AudioStream]:
        from .quality import rank_streams

        return rank_streams(self.streams)


@dataclass
class Task:
    url: str
    canonical_url: str
    format: str
    url_key: str
    id: str = field(default_factory=new_task_id)
    status: str = STATUS_QUEUED
    stage_label: str = "排队中"
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    page: int = 1
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    selected_source: AudioStream | None = None
    output_filename: str | None = None
    output_size: int | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    attempt: int = 1
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    # Internal-only fields (never serialized).
    output_path: Path | None = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    download_epoch: int = 0

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED_STATUSES

    def public_source(self) -> dict[str, Any] | None:
        if not self.selected_source:
            return None
        return self.selected_source.public()

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title or "正在解析…",
            "uploader": self.uploader,
            "duration": self.duration,
            "page": self.page,
            "format": self.format,
            "status": self.status,
            "stage_label": self.stage_label,
            "progress": round(self.progress, 1),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "source": self.public_source(),
            "output_filename": self.output_filename,
            "output_size": self.output_size,
            "error": self.error,
            "warnings": self.warnings,
            "attempt": self.attempt,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    # -- transition helpers -------------------------------------------------
    def reset_for_retry(self) -> None:
        self.status = STATUS_QUEUED
        self.stage_label = STAGE_LABELS[STATUS_QUEUED]
        self.progress = 0.0
        self.downloaded_bytes = 0
        self.total_bytes = None
        self.speed_bps = None
        self.eta_seconds = None
        self.error = None
        self.warnings = []
        self.output_filename = None
        self.output_size = None
        self.output_path = None
        self.selected_source = None
        self.started_at = None
        self.finished_at = None
        self.cancel_event = asyncio.Event()
        self.download_epoch += 1
        self.attempt += 1

    def fail(self, message: str) -> None:
        self.status = STATUS_FAILED
        self.stage_label = STAGE_LABELS[STATUS_FAILED]
        self.error = message
        self.speed_bps = None
        self.eta_seconds = None
        self.finished_at = utc_now_iso()

    def cancel(self) -> None:
        self.cancel_event.set()

    def mark_canceled(self) -> None:
        self.status = STATUS_CANCELED
        self.stage_label = STAGE_LABELS[STATUS_CANCELED]
        self.error = None
        self.speed_bps = None
        self.eta_seconds = None
        self.finished_at = utc_now_iso()


def stats_for(tasks: list[Task]) -> dict[str, int]:
    counts = {
        "total": len(tasks),
        "queued": 0,
        "active": 0,
        "completed": 0,
        "failed": 0,
        "canceled": 0,
    }
    for task in tasks:
        if task.status == STATUS_QUEUED:
            counts["queued"] += 1
        elif task.status in ACTIVE_STATUSES:
            counts["active"] += 1
        elif task.status == STATUS_COMPLETED:
            counts["completed"] += 1
        elif task.status == STATUS_FAILED:
            counts["failed"] += 1
        elif task.status == STATUS_CANCELED:
            counts["canceled"] += 1
    return counts
