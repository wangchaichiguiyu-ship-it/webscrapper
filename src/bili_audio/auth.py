"""Optional Bilibili account login management.

Design goals, in order:

1. **No password handling.** We only accept an existing browser session cookie
   (SESSDATA), exactly what the user's own logged-in browser already has. There
   is no automated login, password storage, QR polling or device spoofing.
2. **Least privilege.** The cookie is only sent to ``*.bilibili.com`` API hosts;
   downloads to CDN hosts use Referer/UA only.
3. **Local-only storage.** The UI-managed cookie is written to
   ``~/.config/bilisonic/cookie.json`` with mode ``0600`` (directory ``0700``)
   and never returned by the API, logged, or committed to Git.
4. **Low request volume.** Safe mode (default) forces one task at a time with a
   randomized pause. Only two metadata requests per task use the account:
   ``view`` and ``playurl``.

This still cannot guarantee zero account risk: Bilibili's terms and risk
controls are outside our control, and any third-party client may be treated as
automation. The UI/docs state this explicitly; users choose their own account
and pace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings, parse_netscape_cookies
from .errors import (
    AuthNotConfiguredError,
    ConfigError,
    InvalidCookieError,
    UpstreamError,
)
from .http_client import apply_cookie_header, base_headers, parse_cookie_header

logger = logging.getLogger(__name__)

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
STORED_VERSION = 1
COOKIE_NAME = "cookie.json"
_MAX_COOKIE_LEN = 8192
# Only these are retained. SESSDATA is the read-only session credential; buvid3/4
# are anonymous device identifiers. bili_jct (CSRF token for write operations)
# and other cookies are deliberately discarded.
_RETAINED_COOKIES = ("SESSDATA", "buvid3", "buvid4")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_cookie_input(raw: str) -> str:
    """Accept a raw Cookie header or bare SESSDATA, keep only read-only cookies.

    Users often paste a full browser Cookie header. We deliberately persist only
    SESSDATA (plus anonymous buvid identifiers) and drop bili_jct and every other
    cookie so a copied config file cannot be used to perform account writes.
    """
    value = (raw or "").strip()
    if not value:
        raise InvalidCookieError("请粘贴 SESSDATA 或完整 Cookie")
    if "\n" in value or "\r" in value:
        raise InvalidCookieError("Cookie 不能包含换行")
    if len(value) > _MAX_COOKIE_LEN:
        raise InvalidCookieError("Cookie 过长，请只粘贴 SESSDATA 值")
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("sessdata:"):
        value = "SESSDATA=" + value.split(":", 1)[1].strip()
    if "=" not in value:
        # A bare SESSDATA token is the normal case copied from DevTools.
        value = f"SESSDATA={value}"
    cookies = parse_cookie_header(value)
    if not cookies.get("SESSDATA"):
        raise InvalidCookieError(
            "Cookie 中缺少 SESSDATA",
            hint="请在浏览器开发者工具 → Application → Cookies → bilibili.com 中复制 SESSDATA 的值。",
        )
    retained = [f"{name}={cookies[name]}" for name in _RETAINED_COOKIES if cookies.get(name)]
    return "; ".join(retained)


def _cookie_from_settings(settings: Settings) -> tuple[str, str]:
    if settings.cookie:
        return settings.cookie, "env"
    if settings.cookie_file:
        try:
            cookies = parse_netscape_cookies(settings.cookie_file)
        except ConfigError:
            logger.warning("Configured cookie file could not be read")
            cookies = {}
        if cookies:
            return "; ".join(f"{name}={value}" for name, value in cookies.items()), "file"
    return "", "none"


@dataclass
class AccountInfo:
    mid: int
    uname: str
    vip: bool = False
    vip_label: str = ""
    level: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "mid": self.mid,
            "uname": self.uname,
            "vip": self.vip,
            "vip_label": self.vip_label,
            "level": self.level,
        }


class AuthStore:
    """In-memory session cookie plus optional local persistence."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.config_dir / COOKIE_NAME
        self._cookie = ""
        self._source = "none"
        self._account: AccountInfo | None = None
        self._checked_at: str | None = None
        self._lock = asyncio.Lock()
        self._load()

    # -- reading ------------------------------------------------------------
    def _load(self) -> None:
        stored = self._read_stored()
        if stored:
            self._cookie = stored["cookie"]
            self._source = "stored"
            account = stored.get("account")
            if isinstance(account, dict) and account.get("uname"):
                try:
                    self._account = AccountInfo(
                        mid=int(account.get("mid") or 0),
                        uname=str(account.get("uname") or ""),
                        vip=bool(account.get("vip")),
                        vip_label=str(account.get("vip_label") or ""),
                        level=int(account.get("level") or 0),
                    )
                except (TypeError, ValueError):
                    self._account = None
            self._checked_at = stored.get("checked_at")
            return
        self._cookie, self._source = _cookie_from_settings(self.settings)

    def _read_stored(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable local cookie store %s", self.path)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("cookie"), str):
            return None
        try:
            cookie = normalize_cookie_input(payload["cookie"])
        except InvalidCookieError:
            logger.warning("Ignoring invalid local cookie store %s", self.path)
            return None
        return {
            "cookie": cookie,
            "account": payload.get("account"),
            "checked_at": payload.get("checked_at"),
        }

    # -- public state -------------------------------------------------------
    @property
    def cookie(self) -> str:
        return self._cookie

    @property
    def configured(self) -> bool:
        return bool(self._cookie)

    @property
    def source(self) -> str:
        return self._source

    @property
    def account(self) -> AccountInfo | None:
        return self._account

    def public_state(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "source": self._source,
            "account": self._account.public() if self._account else None,
            "checked_at": self._checked_at,
            "storage_path": str(self.path),
            "storage_exists": self.path.is_file(),
            "can_logout": self._source == "stored" and self.path.is_file(),
            "safe_mode": self.settings.safe_mode,
            "task_interval": self.settings.task_interval,
            "disclaimer": (
                "使用自己的账号 Cookie 可以获取账号有权访问的音轨（如 Hi-Res）。"
                "本工具不自动登录、不保存密码、不并发刷接口；但任何第三方客户端"
                "都无法绝对保证零账号风险，请低频使用并遵守 B 站用户协议。"
            ),
        }

    # -- client application -------------------------------------------------
    def apply_to_client(self, client: httpx.AsyncClient) -> None:
        apply_cookie_header(client, self._cookie)

    # -- login / validation -------------------------------------------------
    async def validate_cookie(self, raw_cookie: str) -> AccountInfo:
        cookie = normalize_cookie_input(raw_cookie)
        headers = base_headers(self.settings)
        headers["Cookie"] = cookie
        timeout = httpx.Timeout(connect=15.0, read=20.0, write=15.0, pool=15.0)
        try:
            async with httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
                proxy=self.settings.proxy,
            ) as client:
                response = await client.get(NAV_URL)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "无法验证账号 Cookie，请检查网络后重试",
                hint="如果当前网络需要代理，可在 .env 配置 BILI_PROXY。",
            ) from exc
        if response.status_code == 412:
            raise UpstreamError(
                "B 站风控校验失败（HTTP 412），请稍后再试",
                hint="不要频繁重复点击验证；几分钟后再试通常即可。",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("B 站登录校验返回了非 JSON 响应") from exc
        try:
            code = int(payload.get("code") or 0) if isinstance(payload, dict) else -1
        except (TypeError, ValueError):
            code = -1
        data = payload.get("data") if isinstance(payload, dict) else None
        if code != 0 or not isinstance(data, dict) or not data.get("isLogin"):
            if code == -101:
                raise InvalidCookieError(
                    "Cookie 无效或已过期（B 站返回未登录）",
                    hint="请重新登录 bilibili.com，复制最新的 SESSDATA 后再试。",
                )
            message = str(payload.get("message") or "")[:120] if isinstance(payload, dict) else ""
            raise InvalidCookieError(
                "B 站未确认这个 Cookie 的登录状态",
                hint=message or "请重新复制 SESSDATA 后再试。",
            )
        vip_status = int(data.get("vipStatus") or 0)
        level_info = data.get("level_info") if isinstance(data.get("level_info"), dict) else {}
        return AccountInfo(
            mid=int(data.get("mid") or 0),
            uname=str(data.get("uname") or "B站用户"),
            vip=vip_status == 1,
            vip_label=("大会员" if vip_status == 1 else ""),
            level=int(level_info.get("current_level") or 0),
        )

    async def login(self, client: httpx.AsyncClient, raw_cookie: str) -> dict[str, Any]:
        """Validate, persist and activate a cookie. Errors leave state untouched."""
        async with self._lock:
            account = await self.validate_cookie(raw_cookie)
            cookie = normalize_cookie_input(raw_cookie)
            self._checked_at = utc_now_iso()
            self._write_stored(cookie, account)
            self._cookie = cookie
            self._source = "stored"
            self._account = account
            self.apply_to_client(client)
            logger.info("Bilibili account connected: %s (mid=%s)", account.uname, account.mid)
            return self.public_state()

    async def check(self, client: httpx.AsyncClient) -> dict[str, Any]:
        if not self._cookie:
            raise AuthNotConfiguredError("尚未配置账号 Cookie")
        async with self._lock:
            account = await self.validate_cookie(self._cookie)
            self._account = account
            self._checked_at = utc_now_iso()
            if self._source == "stored":
                self._write_stored(self._cookie, account)
            self.apply_to_client(client)
            return self.public_state()

    async def logout(self, client: httpx.AsyncClient) -> dict[str, Any]:
        async with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise ConfigError(f"无法删除本地 Cookie 文件：{exc}") from exc
            self._account = None
            self._checked_at = None
            self._cookie, self._source = _cookie_from_settings(self.settings)
            self.apply_to_client(client)
            logger.info("Bilibili account cookie cleared from the UI store")
            return self.public_state()

    # -- storage ------------------------------------------------------------
    def _write_stored(self, cookie: str, account: AccountInfo | None) -> None:
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise ConfigError(f"无法创建配置目录 {directory}：{exc}") from exc
        payload = {
            "version": STORED_VERSION,
            "cookie": cookie,
            "account": account.public() if account else None,
            "checked_at": self._checked_at or utc_now_iso(),
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"无法保存 Cookie 到 {self.path}：{exc}") from exc
