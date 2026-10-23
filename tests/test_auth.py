from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest
import respx

from bili_audio.auth import AuthStore, normalize_cookie_input
from bili_audio.config import Settings
from bili_audio.errors import InvalidCookieError

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


def _nav_payload(is_login: bool = True, uname: str = "测试用户") -> dict:
    return {
        "code": 0,
        "message": "0",
        "data": {
            "isLogin": is_login,
            "mid": 123456,
            "uname": uname,
            "vipStatus": 1,
            "level_info": {"current_level": 5},
        },
    }


def test_normalize_bare_sessdata() -> None:
    assert normalize_cookie_input("abc%2Cdef") == "SESSDATA=abc%2Cdef"
    restricted = normalize_cookie_input("SESSDATA=x; bili_jct=y; buvid3=z")
    assert restricted == "SESSDATA=x; buvid3=z"
    assert "bili_jct" not in restricted
    assert normalize_cookie_input("Cookie: SESSDATA=abc") == "SESSDATA=abc"
    assert normalize_cookie_input("SESSDATA: abc") == "SESSDATA=abc"


def test_normalize_rejects_missing_sessdata() -> None:
    with pytest.raises(InvalidCookieError):
        normalize_cookie_input("bili_jct=only")
    with pytest.raises(InvalidCookieError):
        normalize_cookie_input("")


async def test_login_persists_private_file(settings: Settings, tmp_path: Path) -> None:
    store = AuthStore(settings)
    assert store.configured is False
    assert store.source == "none"
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_payload()))
            state = await store.login(client, "SESSDATA=sess-token; bili_jct=csrf-should-not-persist")
    assert state["configured"] is True
    assert state["source"] == "stored"
    assert state["account"]["uname"] == "测试用户"
    path = Path(state["storage_path"])
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    raw = path.read_text(encoding="utf-8")
    assert "sess-token" in raw
    assert "csrf-should-not-persist" not in raw
    assert "bili_jct" not in raw

    reloaded = AuthStore(settings)
    assert reloaded.configured is True
    assert reloaded.source == "stored"
    assert reloaded.account is not None
    assert reloaded.account.uname == "测试用户"


async def test_invalid_cookie_is_not_saved(settings: Settings) -> None:
    store = AuthStore(settings)
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(NAV_URL).mock(
                return_value=httpx.Response(200, json={"code": -101, "data": {"isLogin": False}})
            )
            with pytest.raises(InvalidCookieError):
                await store.login(client, "expired-token")
    assert store.configured is False
    assert not store.path.exists()


async def test_stored_cookie_beats_env_and_clear_falls_back(settings: Settings, tmp_path: Path) -> None:
    configured = Settings(
        output_dir=settings.output_dir,
        config_dir=tmp_path / "config",
        ffmpeg=settings.ffmpeg,
        ffprobe=settings.ffprobe,
        cookie="SESSDATA=from-env",
        safe_mode=True,
    )
    configured.validate()
    store = AuthStore(configured)
    assert store.source == "env"
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(NAV_URL).mock(return_value=httpx.Response(200, json=_nav_payload()))
            await store.login(client, "from-ui")
    assert store.source == "stored"
    assert store.cookie == "SESSDATA=from-ui"

    reloaded = AuthStore(configured)
    assert reloaded.source == "stored"
    async with httpx.AsyncClient() as client:
        state = await reloaded.logout(client)
    assert state["configured"] is True
    assert state["source"] == "env"
    assert "from-env" in reloaded.cookie


async def test_check_updates_account(settings: Settings) -> None:
    store = AuthStore(settings)
    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(NAV_URL).mock(
                return_value=httpx.Response(200, json=_nav_payload(uname="新名字"))
            )
            await store.login(client, "token")
        with respx.mock(assert_all_called=False) as router:
            router.get(NAV_URL).mock(
                return_value=httpx.Response(200, json=_nav_payload(uname="更新名字"))
            )
            state = await store.check(client)
    assert state["account"]["uname"] == "更新名字"
    assert state["checked_at"]


def test_safe_mode_forces_sequential_and_interval() -> None:
    s = Settings(
        max_concurrent=4,
        safe_mode=True,
        task_interval=0,
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
    )
    s.validate()
    assert s.max_concurrent == 1
    assert s.task_interval >= 1.5


def test_stored_file_is_valid_json(settings: Settings) -> None:
    store = AuthStore(settings)
    assert not store.path.exists()
    assert store.public_state()["can_logout"] is False
    assert store.public_state()["storage_path"].endswith("cookie.json")
    # public state must never contain a cookie value
    assert "cookie" not in store.public_state()
