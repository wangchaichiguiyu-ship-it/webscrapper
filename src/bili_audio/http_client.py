"""HTTP client factory and Bilibili cookie handling."""

from __future__ import annotations

import logging
from http.cookies import CookieError
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


def base_headers(settings: Settings) -> dict[str, str]:
    return {
        "User-Agent": settings.user_agent,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def create_client(settings: Settings, *, with_cookies: bool = True) -> httpx.AsyncClient:
    """Create the shared API client.

    Cookies are scoped to ``.bilibili.com``. Media CDN domains (bilivideo.cn
    etc.) never receive the login cookie because httpx honours cookie domains.
    """
    settings.ensure_dirs()
    client = httpx.AsyncClient(
        headers=base_headers(settings),
        timeout=httpx.Timeout(connect=15.0, read=45.0, write=30.0, pool=15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        proxy=settings.proxy,
    )
    if with_cookies:
        apply_bilibili_cookies(client, settings)
    return client


def parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name.strip():
            cookies[name.strip()] = value.strip()
    return cookies


def clear_bilibili_cookies(client: httpx.AsyncClient) -> None:
    try:
        for cookie in list(client.cookies.jar):
            if "bilibili.com" in (cookie.domain or ""):
                client.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
    except Exception:
        logger.debug("Could not clear Bilibili cookies", exc_info=True)


def apply_cookie_header(client: httpx.AsyncClient, header: str) -> None:
    """Replace Bilibili cookies on the client with an explicit Cookie header."""
    clear_bilibili_cookies(client)
    for name, value in parse_cookie_header(header).items():
        try:
            client.cookies.set(name, value, domain=".bilibili.com", path="/")
        except (CookieError, ValueError):
            logger.debug("Skipping invalid cookie name")


def apply_bilibili_cookies(client: httpx.AsyncClient, settings: Settings) -> None:
    try:
        cookies = settings.cookie_jar
    except Exception as exc:  # noqa: BLE001 - config validation normally catches this
        logger.warning("Could not load cookies: %s", exc)
        return
    for name, value in cookies.items():
        if not name or value is None:
            continue
        try:
            client.cookies.set(name, value, domain=".bilibili.com", path="/")
        except (CookieError, ValueError):
            # Badly named cookies should not take the whole app down.
            logger.debug("Skipping invalid cookie name")


def media_headers(settings: Settings, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": settings.user_agent,
        "Referer": "https://www.bilibili.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if extra:
        # Never forward credentials to a CDN.
        for key, value in extra.items():
            if key.lower() in {"cookie", "authorization"}:
                continue
            headers[key] = value
    return headers


def safe_http_headers(raw: Any, settings: Settings) -> dict[str, str]:
    """Sanitize the headers yt-dlp reports before using them for downloads."""
    headers: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if key.lower() in {"cookie", "authorization", "proxy-authorization"}:
                continue
            headers[key] = value
    return media_headers(settings, headers)

