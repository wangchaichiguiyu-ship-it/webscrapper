"""Bilibili URL parsing and canonicalisation.

This is deliberately dependency-free and pure so it can be unit-tested offline.
Only Bilibili hosts are accepted: the app is not a generic downloader and must
never become an SSRF proxy for arbitrary URLs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .errors import InvalidUrlError, UnsupportedUrlError

BILIBILI_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "api.bilibili.com",
    "b23.tv",
    "www.b23.tv",
    "bili2233.cn",
    "www.bili2233.cn",
    "bilibili.tv",
    "www.bilibili.tv",
}

_URL_IN_TEXT = re.compile(r"https?://[^\s<>'\"，。；、！？）】》]+", re.IGNORECASE)
_BV = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV = re.compile(r"(?:^|[^A-Za-z0-9])av(\d{1,12})(?:[^0-9]|$)", re.IGNORECASE)
SHORT_HOSTS = {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}
_B23_PATH = re.compile(r"/([A-Za-z0-9]{4,})")

_TRAILING_PUNCT = ".,;:!?)]}>，。；：！？）】》」』、"


@dataclass(frozen=True)
class VideoRef:
    """A resolvable Bilibili video reference."""

    canonical_url: str
    bvid: str | None = None
    aid: int | None = None
    page: int = 1
    kind: str = "video"  # video | bangumi | unsupported
    raw_url: str = ""

    @property
    def video_key(self) -> str:
        if self.kind == "video":
            ident = self.bvid or f"av{self.aid}"
            return f"bili:{ident}:p{self.page}"
        return f"url:{self.canonical_url}"


def _host_allowed(host: str) -> bool:
    host = (host or "").lower().split(":")[0]
    if host in BILIBILI_HOSTS:
        return True
    return host.endswith((".bilibili.com", ".b23.tv", ".bili2233.cn"))


def extract_first_url(text: str) -> str:
    """Pull one URL (or bare BV/av id) out of arbitrary pasted text."""
    if not isinstance(text, str):
        raise InvalidUrlError("URL 必须是文本")
    text = text.strip()
    if not text:
        raise InvalidUrlError("请输入 B 站视频链接")
    if len(text) > 4096:
        raise InvalidUrlError("输入过长，请一次只粘贴一个链接")

    match = _URL_IN_TEXT.search(text)
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCT)

    # Allow pasting a bare BV id / av id, a convenient power-user shortcut.
    bv = _BV.search(text)
    if bv:
        return f"https://www.bilibili.com/video/{bv.group(1)}"
    av = _AV.search(text)
    if av:
        return f"https://www.bilibili.com/video/av{av.group(1)}"
    raise InvalidUrlError("没有识别到 B 站链接，请粘贴 https://www.bilibili.com/video/BV... 形式的链接")


def normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not _host_allowed(parts.hostname or ""):
        raise UnsupportedUrlError(
            "目前只支持 B 站链接（bilibili.com / b23.tv / bili2233.cn）",
            hint="请确认链接来自 B 站视频页。",
        )
    # Drop fragments, keep query.
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def is_short_link(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in SHORT_HOSTS


def parse_b23_code(url: str) -> str | None:
    parts = urlsplit(url)
    match = _B23_PATH.search(parts.path)
    return match.group(1) if match else None


def parse_video_url(url: str) -> VideoRef:
    """Parse a full Bilibili URL into a VideoRef (does not resolve short links)."""
    raw = url
    url = normalize_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path

    if host in SHORT_HOSTS:
        return VideoRef(canonical_url=url, kind="short", raw_url=raw)

    qs = parse_qs(parts.query)
    page_raw = (qs.get("p") or qs.get("page") or ["1"])[0]
    try:
        page = max(1, int(page_raw))
    except (TypeError, ValueError):
        page = 1

    # Normal video: /video/BVxxxx or /video/av123
    match = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d{1,12})", path, re.IGNORECASE)
    if match:
        token = match.group(1)
        if token.lower().startswith("av"):
            aid = int(token[2:])
            canonical = f"https://www.bilibili.com/video/av{aid}?p={page}"
            return VideoRef(canonical_url=canonical, aid=aid, page=page, raw_url=raw)
        bvid = "BV" + token[2:]
        canonical = f"https://www.bilibili.com/video/{bvid}?p={page}"
        return VideoRef(canonical_url=canonical, bvid=bvid, page=page, raw_url=raw)

    # Short-ish /video/ link with only an id in query (?bvid=...)
    bv_q = (qs.get("bvid") or [None])[0]
    if bv_q and _BV.fullmatch(bv_q):
        canonical = f"https://www.bilibili.com/video/{bv_q}?p={page}"
        return VideoRef(canonical_url=canonical, bvid=bv_q, page=page, raw_url=raw)

    # Bangumi / PGC pages are handed to the yt-dlp fallback, which understands them.
    if "/bangumi/play/" in path or re.search(r"/bangumi/play/(ep|ss)\d+", path):
        return VideoRef(canonical_url=url, page=page, kind="bangumi", raw_url=raw)

    raise UnsupportedUrlError(
        "这个 B 站页面类型暂不支持",
        hint="请使用普通视频页（/video/BV...）、短链（b23.tv/...）或番剧播放页（/bangumi/play/...）。",
    )


def canonical_key_for_url(url: str) -> str:
    try:
        return parse_video_url(url).video_key
    except (UnsupportedUrlError, InvalidUrlError):
        return f"url:{url}"


def is_probable_bilibili_url(text: str) -> bool:
    """Cheap front-end/API-side pre-check before doing any network work."""
    try:
        url = extract_first_url(text)
        normalize_url(url)
        return True
    except (UnsupportedUrlError, InvalidUrlError):
        return False
