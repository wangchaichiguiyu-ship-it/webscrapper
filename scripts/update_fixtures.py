#!/usr/bin/env python3
"""Refresh the sanitized upstream-contract fixtures.

Usage:
    .venv/bin/python scripts/update_fixtures.py
    .venv/bin/python scripts/update_fixtures.py --url 'https://www.bilibili.com/video/BV...'
    .venv/bin/python scripts/update_fixtures.py --url ... --output tests/fixtures/playurl_flac.json

The script never downloads media. It fetches metadata + playurl JSON, replaces
all signed media URLs with unique example.invalid placeholders, and writes a
small fixture that can be committed. **Always review the git diff**: an
unexpected new/removed field is exactly the signal this project is designed to
surface.

Run with your normal .env cookie if you want to record restricted formats
(Hi-Res FLAC / Dolby). The fixture then contains their *shape* only, never URLs
or tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bili_audio.config import load_settings
from bili_audio.extractors.bilibili import PLAYURL_URL, VIEW_URL
from bili_audio.http_client import base_headers, create_client
from bili_audio.urls import parse_video_url

FIXTURES = ROOT / "tests" / "fixtures"


def _sanitize_url(placeholder: str) -> str:
    return f"https://example.invalid/{placeholder}"


def _sanitize_stream(entry: dict[str, Any], tag: str) -> None:
    for key in ("baseUrl", "base_url", "url"):
        if key in entry:
            entry[key] = _sanitize_url(f"{tag}.m4s")
    for key in ("backupUrl", "backup_url"):
        if isinstance(entry.get(key), list):
            entry[key] = [_sanitize_url(f"backup-{tag}-{index}.m4s") for index, _ in enumerate(entry[key])]
    for key in ("baseUrl", "base_url", "url", "backupUrl", "backup_url"):
        if isinstance(entry.get(key), str):
            entry[key] = _sanitize_url(f"{tag}.m4s")


def _trim_playurl(data: dict[str, Any]) -> dict[str, Any]:
    dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
    kept: dict[str, Any] = {
        key: data[key]
        for key in (
            "from",
            "quality",
            "format",
            "timelength",
            "accept_format",
            "accept_description",
            "accept_quality",
            "support_formats",
        )
        if key in data
    }
    if dash:
        kept_dash: dict[str, Any] = {
            key: dash[key]
            for key in ("duration", "minBufferTime", "min_buffer_time")
            if key in dash
        }
        audio = dash.get("audio")
        if isinstance(audio, list):
            for index, entry in enumerate(audio):
                if isinstance(entry, dict):
                    _sanitize_stream(entry, f"audio-{entry.get('id', index)}-{index}")
            kept_dash["audio"] = audio
        for group in ("dolby", "flac"):
            value = dash.get(group)
            if isinstance(value, dict):
                cloned = json.loads(json.dumps(value))
                stream = cloned.get("audio")
                if isinstance(stream, dict):
                    _sanitize_stream(stream, f"{group}-{stream.get('id', 'audio')}")
                elif isinstance(stream, list):
                    for index, entry in enumerate(stream):
                        if isinstance(entry, dict):
                            _sanitize_stream(entry, f"{group}-{entry.get('id', index)}")
                kept_dash[group] = cloned
            elif value is not None:
                kept_dash[group] = value
        kept["dash"] = kept_dash
    if isinstance(data.get("durl"), list):
        durl = json.loads(json.dumps(data["durl"]))
        for index, entry in enumerate(durl):
            if isinstance(entry, dict):
                _sanitize_stream(entry, f"durl-{index}")
        kept["durl"] = durl
    return kept


def _trim_view(data: dict[str, Any]) -> dict[str, Any]:
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    return {
        "bvid": data.get("bvid"),
        "aid": data.get("aid"),
        "title": data.get("title"),
        "pic": _sanitize_url("cover.jpg"),
        "duration": data.get("duration"),
        "videos": data.get("videos"),
        "owner": {"name": owner.get("name"), "mid": owner.get("mid")},
        "pages": [
            {
                "cid": page.get("cid"),
                "page": page.get("page"),
                "part": page.get("part"),
                "duration": page.get("duration"),
            }
            for page in pages
            if isinstance(page, dict)
        ],
    }


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    response = await client.get(url, params=params, headers=base_headers(load_settings()))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise SystemExit(f"Bilibili API error: {payload!r}"[:1000])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit("Bilibili API returned no data object")
    return data


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://www.bilibili.com/video/BV1GJ411x7h7")
    parser.add_argument(
        "--output",
        default=None,
        help="output fixture path (default auto: playurl_flac.json if Hi-Res is present)",
    )
    parser.add_argument("--no-view", action="store_true", help="do not update view.json")
    args = parser.parse_args()

    settings = load_settings()
    settings.validate()
    ref = parse_video_url(args.url)
    if not ref.bvid and not ref.aid:
        raise SystemExit("Please pass a direct /video/BV... URL (resolve b23.tv first)")
    client = create_client(settings)
    try:
        view = await _get(
            client,
            VIEW_URL,
            {"bvid": ref.bvid} if ref.bvid else {"aid": ref.aid},
        )
        pages = view.get("pages") or []
        page = pages[min(ref.page, len(pages)) - 1]
        play = await _get(
            client,
            PLAYURL_URL,
            {
                "bvid": ref.bvid,
                "cid": page["cid"],
                "qn": 127,
                "fnval": 4048,
                "fnver": 0,
                "fourk": 1,
                "platform": "pc",
                "high_quality": 1,
            },
        )
    finally:
        await client.aclose()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    if not args.no_view:
        view_out = _trim_view(view)
        (FIXTURES / "view.json").write_text(
            json.dumps(view_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"updated {FIXTURES / 'view.json'}")

    has_flac = bool(
        isinstance(play.get("dash"), dict)
        and isinstance(play["dash"].get("flac"), dict)
        and play["dash"]["flac"].get("audio")
    )
    output = Path(args.output) if args.output else FIXTURES / (
        "playurl_flac.json" if has_flac else "playurl_regular.json"
    )
    payload = _trim_playurl(play)
    payload["_fixture"] = {
        "recorded": date.today().isoformat(),
        "source": "scripts/update_fixtures.py",
        "has_flac": has_flac,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"updated {output} (has_flac={has_flac})")
    print("Review the git diff, then run: .venv/bin/python -m pytest -q")
    print("Tip: URLs are placeholders; no signed links or cookies are stored.")


if __name__ == "__main__":
    asyncio.run(main())
