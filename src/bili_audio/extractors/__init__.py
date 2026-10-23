"""Extractor chain: official Bilibili API first, yt-dlp as compatibility net.

Keeping both paths is the core maintenance strategy. When Bilibili changes a
field, the official parser raises ``SchemaChangedError`` and the chain
transparently retries with yt-dlp (which the project keeps updatable). The
``biliaudio doctor`` command reports which path is healthy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..errors import AppError, InvalidUrlError, SchemaChangedError, UpstreamError
from ..models import MediaInfo
from ..quality import best_stream
from ..urls import (
    VideoRef,
    extract_first_url,
    is_short_link,
    normalize_url,
    parse_video_url,
)
from .bilibili import BilibiliApiExtractor, resolve_short_link
from .ytdlp import YtDlpExtractor

logger = logging.getLogger(__name__)


@dataclass
class ExtractResult:
    media: MediaInfo
    ref: VideoRef
    used_fallback: bool = False


class ExtractorChain:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.primary = BilibiliApiExtractor(client, settings)
        self.fallback = YtDlpExtractor(settings)

    async def resolve_url(self, raw_input: str) -> VideoRef:
        """Extract one URL, resolve b23.tv redirects, and parse a VideoRef."""
        raw_url = extract_first_url(raw_input)
        url = normalize_url(raw_url)
        if is_short_link(url):
            url = await resolve_short_link(self.client, self.settings, url)
            url = normalize_url(url)
        ref = parse_video_url(url)
        if ref.kind == "short":
            raise InvalidUrlError("短链接没有跳转到视频页，请复制浏览器地址栏中的完整链接")
        return ref

    async def extract(self, ref: VideoRef) -> ExtractResult:
        primary_error: AppError | None = None
        if ref.kind == "video":
            try:
                media = await self.primary.extract(ref)
                if best_stream(media.streams):
                    return ExtractResult(media=media, ref=ref)
                raise SchemaChangedError("官方接口没有返回任何音频流")
            except SchemaChangedError as exc:
                primary_error = exc
                logger.warning("Bilibili API schema changed, trying yt-dlp: %s", exc.message)
            except UpstreamError as exc:
                # Actionable per-video/per-account errors will fail in yt-dlp too.
                if exc.bili_code in {-404, -403, 62002, 62004, -10403}:
                    raise
                primary_error = exc
                logger.warning("Bilibili API failed (%s), trying yt-dlp", exc.code)
            except AppError:
                raise

        try:
            media = await self.fallback.extract(ref)
        except AppError as fallback_error:
            if primary_error is not None:
                raise UpstreamError(
                    "官方接口解析失败，兼容解析器也未能完成",
                    hint=(
                        "B 站页面或接口可能已更新。请先运行 `biliaudio doctor`，"
                        "并尝试 `pip install -U yt-dlp`；若仍失败请查看 MAINTENANCE.md。"
                    ),
                    extra={
                        "primary_error": primary_error.message,
                        "fallback_error": fallback_error.message,
                    },
                ) from fallback_error
            raise
        if primary_error is not None:
            media.warnings.append("官方接口异常，已使用 yt-dlp 兼容解析（音质选项可能略少）")
        return ExtractResult(media=media, ref=ref, used_fallback=ref.kind != "video" or primary_error is not None)


__all__ = [
    "BilibiliApiExtractor",
    "ExtractResult",
    "ExtractorChain",
    "YtDlpExtractor",
    "resolve_short_link",
]
