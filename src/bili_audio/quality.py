"""Audio stream model and the "best available" ranking policy.

Policy, in order:
1. Hi-Res lossless FLAC (if the account is entitled to it)
2. Dolby Atmos audio
3. Highest-bandwidth regular DASH audio (Bilibili usually 30280 / 192 kbps)
4. Muxed durl fallback (legacy videos; the video stream is downloaded and the
   audio extracted — quality is not ideal, but it keeps old URLs usable)

Among streams in the same tier we pick the highest declared bandwidth. The
downloader still tries candidates in order and only downgrades after a real
failure, because Bilibili occasionally publishes dead URLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Well-known Bilibili dash audio ids. Labels are cosmetic only; unknown ids are
# rendered from the declared bandwidth so a new id does not break the app.
KNOWN_QUALITY_LABELS = {
    30216: "64K AAC",
    30232: "132K AAC",
    30280: "192K AAC",
    30250: "Dolby Atmos",
    30251: "Hi-Res 无损",
}

TIER_LOSSLESS = 0
TIER_DOLBY = 1
TIER_REGULAR = 2
TIER_MUXED = 3

_TIER_LABELS = {
    TIER_LOSSLESS: "无损优先",
    TIER_DOLBY: "Dolby 优先",
    TIER_REGULAR: "最高码率优先",
    TIER_MUXED: "兼容模式",
}


def format_bandwidth(bandwidth: int | None) -> str:
    if not bandwidth:
        return "码率未知"
    if bandwidth >= 1_000_000:
        return f"{bandwidth / 1_000_000:.1f} Mbps"
    return f"{round(bandwidth / 1000)} kbps"


def quality_label(quality_id: int | None, bandwidth: int | None, codecs: str = "") -> str:
    if quality_id in KNOWN_QUALITY_LABELS:
        return KNOWN_QUALITY_LABELS[quality_id]  # type: ignore[index]
    if "flac" in codecs.lower():
        return f"无损 FLAC · {format_bandwidth(bandwidth)}"
    if "ec-3" in codecs.lower() or "ac-3" in codecs.lower():
        return f"Dolby · {format_bandwidth(bandwidth)}"
    return format_bandwidth(bandwidth)


@dataclass
class AudioStream:
    """One downloadable candidate. ``urls`` is ordered: primary + backups."""

    urls: list[str]
    quality_id: int
    label: str
    bandwidth: int
    mime_type: str
    codecs: str
    container: str  # m4a | flac | mp4
    lossless: bool
    tier: int
    source: str  # bilibili_api | yt_dlp
    note: str = ""
    is_muxed: bool = False
    http_headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_dolby(self) -> bool:
        return self.tier == TIER_DOLBY

    @property
    def is_muxed_stream(self) -> bool:
        return self.is_muxed

    def public(self) -> dict[str, Any]:
        return {
            "quality_id": self.quality_id,
            "label": self.label,
            "lossless": self.lossless,
            "bandwidth": self.bandwidth,
            "codec": self.codecs,
            "container": self.container,
            "note": self.note,
            "tier": self.tier,
            "tier_label": _TIER_LABELS.get(self.tier, ""),
        }


def stream_sort_key(stream: AudioStream) -> tuple[int, int, int]:
    # tier first, then bandwidth descending, lossless before lossy as a tiebreak.
    return (stream.tier, -int(stream.bandwidth or 0), 0 if stream.lossless else 1)


def rank_streams(streams: list[AudioStream]) -> list[AudioStream]:
    seen: set[str] = set()
    unique: list[AudioStream] = []
    for stream in streams:
        if not stream.urls:
            continue
        key = stream.urls[0]
        if key in seen:
            continue
        seen.add(key)
        unique.append(stream)
    return sorted(unique, key=stream_sort_key)


def best_stream(streams: list[AudioStream]) -> AudioStream | None:
    ranked = rank_streams(streams)
    return ranked[0] if ranked else None


def source_quality_note(stream: AudioStream, target_format: str) -> str:
    """Human explanation shown under the task, without over-promising fidelity."""
    if stream.lossless:
        base = "已捕获无损/Hi-Res 音轨"
        if target_format == "flac":
            return f"{base}，FLAC 封装无二次损失"
        if target_format == "wav":
            return f"{base}，解码为 PCM WAV"
        return f"{base}，原始音轨不转码"
    base = f"源音轨为 {stream.label}（有损）"
    if target_format == "flac":
        return f"{base}；转为 FLAC 只是无损封装，不会提升原音质"
    if target_format == "wav":
        return f"{base}；解码为 WAV 不会提升原音质"
    return f"{base}；保留原始编码，避免二次损失"
