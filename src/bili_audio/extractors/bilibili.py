"""Official Bilibili web-API extractor.

Endpoint knowledge (checked 2026-10, see tests/fixtures for the exact shape):

* ``GET /x/web-interface/view``        -> title + pages (cid)
* ``GET /x/player/playurl``            -> DASH audio / Dolby / FLAC streams

The ``fnval=4048`` bit-mask asks for every currently known stream type, and
``fourk=1`` unlocks 4K/Dolby/Hi-Res where the account is entitled. The parser is
deliberately tolerant of snake_case/camelCase and missing optional fields, but
raises ``SchemaChangedError`` when a critical field disappears; that is the
signal for the extractor chain to fall back to yt-dlp.

Do not add HTML scraping here. Bilibili's rendered pages are heavily obfuscated
and change far more often than the JSON API; yt-dlp already maintains that kind
of parser, so it is our fallback instead.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..auth import AuthStore
from ..config import Settings
from ..errors import (
    InvalidUrlError,
    SchemaChangedError,
    UpstreamError,
)
from ..http_client import base_headers, media_headers
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

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAYURL_URL = "https://api.bilibili.com/x/player/playurl"
FINGER_SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"

# Bilibili's public error codes we can translate into actionable Chinese text.
_BILI_ERRORS = {
    -404: "视频不存在、已被删除或不可见",
    -403: "没有访问权限（可能是地区限制、年龄限制或付费内容）",
    -352: "B 站风控校验失败，请稍后重试；登录 Cookie 通常可以降低触发概率",
    -412: "B 站风控校验失败，请稍后重试；登录 Cookie 通常可以降低触发概率",
    -10403: "该视频在当前地区不可播放",
    62002: "稿件不可见（可能已删除或设为私密）",
    62004: "稿件正在审核中，暂时无法播放",
    62012: "仅 UP 主自己可见",
}


async def resolve_short_link(
    client: httpx.AsyncClient, settings: Settings, url: str, *, max_hops: int = 5
) -> str:
    """Follow a b23.tv redirect without downloading the whole landing page."""
    current = url
    for _ in range(max_hops):
        try:
            async with client.stream(
                "GET", current, headers=base_headers(settings)
            ) as response:
                final = str(response.url)
                if final and final != current:
                    current = final
                    if "bilibili.com" in urlsplit(current).netloc:
                        return current
                    continue
                # A 200 response means the server rendered a hop page; the final
                # URL is still usable if it points at bilibili.com.
                if "bilibili.com" in urlsplit(final).netloc:
                    return final
                return final
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "短链接解析失败，请检查网络后重试",
                hint="也可以直接粘贴完整视频链接（https://www.bilibili.com/video/BV...）。",
            ) from exc
    raise UpstreamError("短链接跳转次数过多，请粘贴完整视频链接")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _first_url(raw: Mapping[str, Any]) -> str | None:
    for key in ("baseUrl", "base_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return None


def _backup_urls(raw: Mapping[str, Any]) -> list[str]:
    for key in ("backupUrl", "backup_url", "backup_urls"):
        value = raw.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.startswith("http")]
    return []


def _guess_container(mime: str, codecs: str, url: str, lossless: bool) -> str:
    lower = f"{mime} {codecs} {url}".lower()
    if lossless or "flac" in lower:
        return "flac"
    if "mp4" in lower or "m4a" in lower or "aac" in lower:
        return "m4a"
    path = urlsplit(url).path.lower()
    for ext in ("flac", "m4a", "mp4", "aac", "ogg"):
        if path.endswith("." + ext):
            return "m4a" if ext == "aac" else ext
    return "m4a"


def _make_stream(
    raw: Mapping[str, Any],
    *,
    tier: int,
    settings: Settings,
    default_quality_id: int = 0,
    note: str = "",
    is_muxed: bool = False,
) -> AudioStream | None:
    primary = _first_url(raw)
    if not primary:
        return None
    mime = str(raw.get("mimeType") or raw.get("mime_type") or "")
    codecs = str(raw.get("codecs") or "")
    quality_id = _as_int(raw.get("id"), default_quality_id)
    bandwidth = _as_int(raw.get("bandwidth") or raw.get("size") or 0)
    lossless = tier == TIER_LOSSLESS or "flac" in f"{mime} {codecs}".lower()
    container = _guess_container(mime, codecs, primary, lossless)
    label = quality_label(quality_id, bandwidth, codecs)
    if tier == TIER_DOLBY:
        label = f"Dolby Atmos · {format_bandwidth(bandwidth)}"
    if is_muxed:
        label = "兼容模式（音视频混合流）"
    return AudioStream(
        urls=[primary, *_backup_urls(raw)],
        quality_id=quality_id,
        label=label,
        bandwidth=bandwidth,
        mime_type=mime,
        codecs=codecs,
        container=container,
        lossless=lossless,
        tier=tier,
        source="bilibili_api",
        note=note,
        is_muxed=is_muxed,
        http_headers=media_headers(settings),
    )


def parse_playurl(
    data: Mapping[str, Any],
    *,
    settings: Settings,
    has_cookie: bool,
    warnings: list[str] | None = None,
) -> list[AudioStream]:
    """Pure parser for the ``data`` object of ``x/player/playurl``.

    Kept pure and fixture-testable so a schema diff can be diagnosed without
    hitting the network. Unknown extra keys are ignored on purpose.
    """
    warnings = warnings if warnings is not None else []
    streams: list[AudioStream] = []
    dash = data.get("dash")
    if isinstance(dash, dict):
        flac = dash.get("flac")
        flac_display = bool(flac.get("display")) if isinstance(flac, dict) else False
        flac_entries = _as_dict_list(flac.get("audio")) if isinstance(flac, dict) else []
        for entry in flac_entries:
            stream = _make_stream(
                entry,
                tier=TIER_LOSSLESS,
                settings=settings,
                default_quality_id=30251,
                note="B 站 Hi-Res 无损音轨",
            )
            if stream:
                streams.append(stream)
        if flac_display and not flac_entries:
            warnings.append(
                "该视频提供 Hi-Res 无损音轨，但当前账号无权访问；"
                "在 .env 中配置 BILI_SESSDATA 后可尝试获取。"
            )

        dolby = dash.get("dolby")
        dolby_entries = _as_dict_list(dolby.get("audio")) if isinstance(dolby, dict) else []
        for entry in dolby_entries:
            stream = _make_stream(
                entry,
                tier=TIER_DOLBY,
                settings=settings,
                default_quality_id=30250,
                note="Dolby Atmos 音轨",
            )
            if stream:
                streams.append(stream)
        if isinstance(dolby, dict) and _as_int(dolby.get("type"), 0) != 0 and not dolby_entries:
            warnings.append("该视频提供 Dolby 音轨，但当前账号无权访问，已回退到常规音质。")

        for entry in _as_dict_list(dash.get("audio")):
            stream = _make_stream(entry, tier=TIER_REGULAR, settings=settings)
            if stream:
                best_note = "B 站当前账号可见的最高常规码率音轨"
                stream.note = best_note
                streams.append(stream)
    else:
        warnings.append("接口未返回 DASH 流，尝试兼容模式（音视频混合流）。")

    # Legacy videos (or an API variant) may only expose muxed durl streams.
    if not streams:
        durl_entries = _as_dict_list(data.get("durl"))
        quality_id = _as_int(data.get("quality"), 0)
        for entry in durl_entries:
            stream = _make_stream(
                entry,
                tier=TIER_MUXED,
                settings=settings,
                default_quality_id=quality_id,
                note="兼容模式：下载音视频混合流后自动抽取音轨",
                is_muxed=True,
            )
            if stream:
                streams.append(stream)
        if streams:
            warnings.append(
                "该稿件仅返回音视频混合流（兼容模式）。下载体积会大于纯音频，"
                "但音频仍会被无损抽取。"
            )

    if not streams:
        raise SchemaChangedError(
            "没有在播放接口响应中找到音频流",
            hint="B 站接口结构可能已变化，已自动尝试 yt-dlp 兼容解析。",
        )

    # On a normal video, regular audio always exists; mark only the top regular
    # stream as "highest" for a clearer UI.
    ranked = rank_streams(streams)
    if not has_cookie:
        # Only a soft, once-per-task hint. It is not an error: unauthenticated
        # requests commonly still get 192 kbps AAC.
        warnings.append("未配置登录 Cookie：部分 Hi-Res / Dolby 音轨可能不可见。")
    return ranked


class BilibiliApiExtractor:
    name = "bilibili_api"

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        auth: AuthStore | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.auth = auth
        self._buvid_refreshed = False

    @property
    def has_cookie(self) -> bool:
        return self.auth.configured if self.auth is not None else self.settings.has_cookie

    async def _get_api(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self.client.get(
                    url, params=params, headers=base_headers(self.settings)
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    continue
                raise UpstreamError(
                    "无法连接 B 站接口，请检查网络或代理设置",
                    hint="可运行 `biliaudio doctor` 检查网络连通性。",
                ) from exc

            if response.status_code == 412:
                if not self._buvid_refreshed:
                    await self._refresh_buvid()
                last_error = UpstreamError("B 站返回 412（风控校验）", http_status=412)
                if attempt == 0:
                    continue
                raise last_error

            if response.status_code >= 500:
                last_error = UpstreamError(f"B 站服务暂时不可用（HTTP {response.status_code}）")
                if attempt == 0:
                    continue
                raise last_error

            if response.status_code == 403:
                raise UpstreamError(
                    "B 站拒绝访问（HTTP 403）",
                    hint="可能是地区限制或风控；配置登录 Cookie 后重试。",
                    http_status=403,
                )
            if response.status_code >= 400:
                raise UpstreamError(f"B 站接口返回 HTTP {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise SchemaChangedError(
                    "B 站接口返回的不是 JSON（可能被风控或返回了网页）",
                    http_status=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise SchemaChangedError("B 站接口返回结构异常")

            code = _as_int(payload.get("code"), 0)
            if code != 0:
                message = _BILI_ERRORS.get(code) or f"B 站接口错误（code={code}）"
                detail = payload.get("message") or payload.get("msg") or ""
                raise UpstreamError(
                    message,
                    bili_code=code,
                    hint=(
                        "如果频繁出现，请在 .env 中配置 BILI_SESSDATA（见 README）。"
                        if code in {-352, -412}
                        else None
                    ),
                    extra={"upstream_message": str(detail)[:200]} if detail else None,
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise SchemaChangedError("B 站接口响应缺少 data 对象")
            return data

        raise UpstreamError("B 站接口请求失败") from last_error

    async def _refresh_buvid(self) -> None:
        """Fetch anonymous browser cookies; lowers (not eliminates) 412 risk."""
        self._buvid_refreshed = True
        try:
            response = await self.client.get(
                FINGER_SPI_URL, headers=base_headers(self.settings), timeout=10
            )
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                if isinstance(data.get("b_3"), str):
                    self.client.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
                if isinstance(data.get("b_4"), str):
                    self.client.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
        except (httpx.HTTPError, ValueError):
            logger.debug("Could not refresh buvid cookies", exc_info=True)

    async def _fetch_view(self, ref: VideoRef) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if ref.bvid:
            params["bvid"] = ref.bvid
        elif ref.aid:
            params["aid"] = ref.aid
        else:
            raise InvalidUrlError("链接中缺少 BV 号或 av 号")
        data = await self._get_api(VIEW_URL, params)
        if not isinstance(data.get("title"), str):
            raise SchemaChangedError("视频信息接口缺少 title 字段")
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            raise SchemaChangedError("视频信息接口缺少分P列表（pages）")
        if ref.page > len(pages):
            raise InvalidUrlError(
                f"该视频只有 {len(pages)} 个分P，无法获取 P{ref.page}",
                hint="请检查链接中的 ?p= 参数。",
            )
        page_data = pages[ref.page - 1]
        if not isinstance(page_data, dict) or not _as_int(page_data.get("cid"), 0):
            raise SchemaChangedError("视频信息接口的分P缺少 cid")
        return data

    async def _fetch_playurl(
        self, ref: VideoRef, view: dict[str, Any], cid: int
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cid": cid,
            "qn": 127,
            "fnval": 4048,
            "fnver": 0,
            "fourk": 1,
            "platform": "pc",
            "high_quality": 1,
        }
        if ref.bvid:
            params["bvid"] = ref.bvid
        elif ref.aid:
            params["avid"] = ref.aid
        return await self._get_api(PLAYURL_URL, params)

    async def extract(self, ref: VideoRef) -> MediaInfo:
        view = await self._fetch_view(ref)
        pages = view["pages"]
        page_data = pages[ref.page - 1]
        cid = _as_int(page_data.get("cid"))
        play = await self._fetch_playurl(ref, view, cid)

        warnings: list[str] = []
        streams = parse_playurl(
            play, settings=self.settings, has_cookie=self.has_cookie, warnings=warnings
        )
        best = streams[0] if streams else None
        if best is None:  # defensive; parse_playurl already raises
            raise SchemaChangedError("解析结果为空")

        title = str(view.get("title") or page_data.get("part") or "未命名视频")
        total_pages = len(pages)
        page_title = str(page_data.get("part") or "")
        duration = _as_float(page_data.get("duration"), _as_float(view.get("duration"), 0.0))
        uploader = ""
        owner = view.get("owner")
        if isinstance(owner, dict):
            uploader = str(owner.get("name") or "")
        cover = view.get("pic") if isinstance(view.get("pic"), str) else None

        return MediaInfo(
            canonical_url=ref.canonical_url,
            title=title,
            uploader=uploader,
            duration=duration,
            page=ref.page,
            page_title=page_title,
            total_pages=total_pages,
            cover_url=cover,
            extractor=self.name,
            streams=streams,
            warnings=warnings,
            extra={
                "bvid": view.get("bvid"),
                "aid": view.get("aid"),
                "cid": cid,
                "quality_count": len(streams),
                "playurl_keys": sorted(play.keys())[:30],
            },
        )
