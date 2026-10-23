"""Streaming media downloader.

Design constraints:
* One audio file is streamed in 64 KiB chunks, so memory use is flat regardless
  of file size.
* ``.part`` files support HTTP Range resume on transient network errors.
* Candidates (primary URL + CDN backups) are tried in order. The pipeline only
  falls back to a lower-quality candidate after a real failure, never silently.
* Login cookies are not sent to CDN hosts; only Referer/User-Agent are.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

import httpx

from .config import Settings
from .errors import DownloadError, MediaExpiredError, PipelineCancelled
from .http_client import media_headers
from .quality import AudioStream

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
_HTML_TYPES = ("text/html", "application/json", "text/plain", "application/xml")


class _CandidateFailed(Exception):
    def __init__(self, reason: str, *, retryable: bool = True, expired: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.expired = expired


def _parse_content_range(value: str | None) -> tuple[int, int | None] | None:
    if not value:
        return None
    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value, re.IGNORECASE)
    if not match:
        return None
    start = int(match.group(1))
    total = None if match.group(3) == "*" else int(match.group(3))
    return start, total


async def _stream_one_url(
    client: httpx.AsyncClient,
    stream: AudioStream,
    url: str,
    destination: Path,
    settings: Settings,
    on_progress: Callable[[int, int | None], None] | None,
    cancel_event: asyncio.Event,
) -> tuple[int, int | None]:
    existing = destination.stat().st_size if destination.exists() else 0
    headers = media_headers(settings, stream.http_headers)
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"

    try:
        async with client.stream("GET", url, headers=headers) as response:
            status = response.status_code
            if status == 416:
                # Stale/oversized part file: restart from scratch next attempt.
                destination.unlink(missing_ok=True)
                raise _CandidateFailed("Range 请求被拒绝，准备重新下载")

            if status in (401, 403):
                raise _CandidateFailed(f"CDN 拒绝访问（HTTP {status}）", retryable=False, expired=True)
            if status in (404, 410):
                raise _CandidateFailed(f"媒体链接已失效（HTTP {status}）", retryable=False, expired=True)
            if status == 429:
                raise _CandidateFailed("CDN 限流（HTTP 429）")
            if status >= 500:
                raise _CandidateFailed(f"CDN 服务错误（HTTP {status}）")
            if status >= 400:
                raise _CandidateFailed(f"下载失败（HTTP {status}）", retryable=False)

            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if any(content_type.startswith(t) for t in _HTML_TYPES):
                raise _CandidateFailed(
                    "返回了网页而不是媒体文件（链接可能已过期或被风控）",
                    retryable=False,
                    expired=True,
                )

            if status == 206:
                range_info = _parse_content_range(response.headers.get("content-range"))
                if existing == 0 or range_info is None or range_info[0] != existing:
                    # Server ignored our offset or the file changed; restart.
                    existing = 0
                    destination.unlink(missing_ok=True)
                    raise _CandidateFailed("续传位置不一致，准备重新下载")
                total = range_info[1] if range_info[1] is not None else existing + _content_length(response)
                mode = "ab"
            else:
                existing = 0
                total = _content_length(response)
                mode = "wb"

            downloaded = existing
            if on_progress:
                on_progress(downloaded, total)
            with destination.open(mode) as handle:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancel_event.is_set():
                        raise PipelineCancelled()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
                handle.flush()
            if total is not None and downloaded < total:
                raise _CandidateFailed(
                    f"连接提前中断（已下载 {downloaded}/{total} 字节），准备续传"
                )
            if downloaded == 0:
                raise _CandidateFailed("下载结果为空")
            return downloaded, total
    except httpx.TimeoutException as exc:
        raise _CandidateFailed("下载超时") from exc
    except httpx.TransportError as exc:
        raise _CandidateFailed(f"网络错误：{type(exc).__name__}") from exc
    except OSError as exc:
        raise DownloadError(f"写入文件失败：{exc}") from exc


def _content_length(response: httpx.Response) -> int | None:
    raw = response.headers.get("content-length")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


async def download(
    stream: AudioStream,
    destination: Path,
    settings: Settings,
    *,
    on_progress: Callable[[int, int | None], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[int, int | None]:
    """Download one stream to ``destination``; returns (bytes, total)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Do NOT delete an existing .part here: retries resume with HTTP Range.
    # The pipeline removes stale parts before starting a fresh candidate.
    cancel_event = cancel_event or asyncio.Event()

    timeout = httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=20.0)
    last_error: _CandidateFailed | None = None
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        proxy=settings.proxy,
    ) as client:
        for url_index, url in enumerate(stream.urls):
            if url_index > 0:
                # Different CDN host: don't resume bytes from the previous one.
                destination.unlink(missing_ok=True)
            for attempt in range(1, settings.download_retries + 1):
                if cancel_event.is_set():
                    raise PipelineCancelled()
                try:
                    return await _stream_one_url(
                        client,
                        stream,
                        url,
                        destination,
                        settings,
                        on_progress,
                        cancel_event,
                    )
                except PipelineCancelled:
                    raise
                except _CandidateFailed as exc:
                    last_error = exc
                    logger.info(
                        "download attempt failed (quality=%s attempt=%s/%s): %s",
                        stream.quality_id,
                        attempt,
                        settings.download_retries,
                        exc.reason,
                    )
                    if not exc.retryable or attempt >= settings.download_retries:
                        break
                    await asyncio.sleep(min(0.6 * attempt, 2.0))
                except DownloadError:
                    raise

    destination.unlink(missing_ok=True)
    reason = last_error.reason if last_error else "未知错误"
    if last_error and last_error.expired:
        raise MediaExpiredError(
            "媒体链接已过期或 CDN 拒绝访问，请点击重试",
            hint=reason,
        )
    raise DownloadError(
        "音频下载失败，已尝试所有候选地址",
        hint=reason,
        extra={"quality": stream.label},
    )
