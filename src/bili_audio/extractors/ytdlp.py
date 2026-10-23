"""yt-dlp compatibility extractor.

Why this exists: Bilibili changes its page/API regularly. yt-dlp has a large
maintainer community and ships frequent fixes, so instead of reimplementing
HTML/JS parsing we call it as a *fallback* (and for pages our primary extractor
does not cover, such as bangumi). It is used only to resolve metadata + media
URLs; downloading and conversion stay in our own streaming pipeline so progress
and memory usage remain predictable.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..auth import AuthStore
from ..config import Settings
from ..errors import SchemaChangedError, UpstreamError
from ..http_client import safe_http_headers
from ..models import MediaInfo
from ..quality import (
    TIER_DOLBY,
    TIER_LOSSLESS,
    TIER_MUXED,
    TIER_REGULAR,
    AudioStream,
    format_bandwidth,
    quality_label,
    rank_streams,
)
from ..urls import VideoRef

logger = logging.getLogger(__name__)

_LOSSLESS_CODECS = {"flac", "alac"}
_DOLBY_CODECS = {"ec-3", "ac-3", "eac3"}


class _YdlLogger:
    """Tiny logger adapter; avoids yt-dlp printing to our console."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.messages.append(str(msg))

    def error(self, msg: str) -> None:
        self.messages.append(str(msg))


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _format_quality(format_id: Any, abr: float, codec: str, ext: str) -> tuple[int, str]:
    qid = 0
    match = re.search(r"(\d{4,6})", str(format_id or ""))
    if match:
        qid = int(match.group(1))
    label = quality_label(qid, int(abr * 1000) if abr else 0, codec)
    if not qid and ext:
        label = f"{label} · {ext}" if abr else ext.upper()
    return qid, label


def _parse_entry(entry: dict[str, Any], settings: Settings) -> AudioStream | None:
    url = entry.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    ext = str(entry.get("ext") or "").lower()
    acodec = str(entry.get("acodec") or entry.get("audio_codec") or "").lower()
    codec = acodec
    vcodec = str(entry.get("vcodec") or "")
    lossless = ext == "flac" or acodec in _LOSSLESS_CODECS or "flac" in acodec
    is_dolby = acodec in _DOLBY_CODECS or any(c in acodec for c in _DOLBY_CODECS)
    is_muxed = bool(vcodec and vcodec != "none")
    abr = _num(entry.get("abr") or entry.get("tbr") or entry.get("bitrate"))
    if lossless:
        tier = TIER_LOSSLESS
    elif is_dolby:
        tier = TIER_DOLBY
    elif is_muxed:
        tier = TIER_MUXED
    else:
        tier = TIER_REGULAR
    if is_muxed and not lossless:
        tier = TIER_MUXED
    qid, label = _format_quality(entry.get("format_id"), abr, codec, ext)
    if is_muxed:
        label = "兼容模式（音视频混合流）"
    headers = safe_http_headers(entry.get("http_headers") or {}, settings)
    return AudioStream(
        urls=[url],
        quality_id=qid,
        label=label,
        bandwidth=int(abr * 1000) if abr else 0,
        mime_type=str(entry.get("mime_type") or ""),
        codecs=codec,
        container="flac" if lossless else ("m4a" if ext in {"m4a", "mp4", "aac"} else ext or "m4a"),
        lossless=lossless,
        tier=tier,
        source="yt_dlp",
        note=(
            "yt-dlp 兼容解析出的无损音轨"
            if lossless
            else f"yt-dlp 兼容解析 · {format_bandwidth(int(abr * 1000) if abr else 0)}"
        ),
        is_muxed=is_muxed,
        http_headers=headers,
    )


def _extract_sync(url: str, settings: Settings, cookie: str = "") -> dict[str, Any]:
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UpstreamError(
            "兼容解析器 yt-dlp 未安装",
            hint="运行 `pip install -e .` 或 `pip install yt-dlp` 后重启。",
        ) from exc

    logger_adapter = _YdlLogger()
    headers = {
        "User-Agent": settings.user_agent,
        "Referer": "https://www.bilibili.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "socket_timeout": 30,
        "retries": 2,
        "cachedir": False,
        "logger": logger_adapter,
        "http_headers": headers,
        "nocheckcertificate": False,
    }
    if settings.cookie_file and not cookie:
        options["cookiefile"] = str(settings.cookie_file)
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        message = str(exc)
        # Strip ANSI and the "ERROR: " prefix, keep it short for the UI.
        message = re.sub(r"\x1b\[[0-9;]*m", "", message).replace("ERROR:", "").strip()
        raise UpstreamError(
            "兼容解析器无法读取该页面",
            hint=message[:240] or None,
        ) from exc
    except Exception as exc:  # yt-dlp can raise a wide range of errors
        raise UpstreamError("兼容解析器发生异常", hint=str(exc)[:200]) from exc
    if not isinstance(info, dict):
        raise SchemaChangedError("yt-dlp 没有返回视频信息")
    return info


class YtDlpExtractor:
    name = "yt_dlp"

    def __init__(self, settings: Settings, auth: AuthStore | None = None) -> None:
        self.settings = settings
        self.auth = auth

    async def extract(self, ref: VideoRef) -> MediaInfo:
        cookie = self.auth.cookie if self.auth is not None else self.settings.cookie
        info = await asyncio.to_thread(
            _extract_sync, ref.canonical_url, self.settings, cookie
        )
        warnings: list[str] = []

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            entries = [entry for entry in entries if isinstance(entry, dict)]
            if not entries:
                raise UpstreamError("该页面没有可下载的视频")
            info = entries[0]
            warnings.append("该页面是合集，已选择其中的第一个视频。")

        formats = [entry for entry in (info.get("formats") or []) if isinstance(entry, dict)]
        audio_formats = [
            entry
            for entry in formats
            if str(entry.get("acodec") or "none") != "none"
            and entry.get("url")
            and (
                str(entry.get("vcodec") or "none") == "none"
                or entry.get("acodec")
            )
        ]
        # Prefer audio-only streams; only use muxed formats when nothing else is available.
        audio_only = [entry for entry in audio_formats if str(entry.get("vcodec") or "none") == "none"]
        candidates = audio_only or audio_formats
        if not candidates and isinstance(info.get("url"), str):
            candidates = [info]
        streams = [stream for entry in candidates if (stream := _parse_entry(entry, self.settings))]
        streams = rank_streams(streams)
        if not streams:
            raise SchemaChangedError(
                "yt-dlp 没有返回可用的音频格式",
                hint="请更新 yt-dlp：`pip install -U yt-dlp`。",
            )

        title = str(info.get("title") or "未命名视频")
        uploader = str(info.get("uploader") or info.get("channel") or "")
        duration = _num(info.get("duration"))
        cover = info.get("thumbnail") if isinstance(info.get("thumbnail"), str) else None
        webpage = info.get("webpage_url")
        canonical = webpage if isinstance(webpage, str) and webpage.startswith("http") else ref.canonical_url
        return MediaInfo(
            canonical_url=canonical,
            title=title,
            uploader=uploader,
            duration=duration,
            page=ref.page,
            page_title="",
            total_pages=1,
            cover_url=cover,
            extractor=self.name,
            streams=streams,
            warnings=warnings,
            extra={"format_count": len(streams)},
        )
