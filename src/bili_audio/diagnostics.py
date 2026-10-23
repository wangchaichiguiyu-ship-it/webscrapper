"""`biliaudio doctor` — environment and upstream-contract health checks.

This is the first tool to run when Bilibili stops working. It deliberately
separates local problems (ffmpeg, disk, permissions) from upstream problems
(schema changes, risk control, missing login) so maintenance is a five-minute
triage instead of guesswork.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from . import APP_NAME, __version__
from .auth import AuthStore
from .config import PROJECT_ROOT, Settings
from .converter import ffmpeg_version
from .errors import AppError
from .extractors import ExtractorChain
from .extractors.bilibili import parse_playurl
from .http_client import create_client
from .quality import best_stream

DEFAULT_SAMPLE = "https://www.bilibili.com/video/BV1GJ411x7h7"
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    severity: str = "info"  # info | warning | error


def _fixture_playurl(name: str) -> dict[str, Any]:
    path = FIXTURE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def _offline_contract_check(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    try:
        regular = parse_playurl(
            _fixture_playurl("playurl_regular.json"),
            settings=settings,
            has_cookie=False,
        )
        best = best_stream(regular)
        ok = bool(best and best.quality_id == 30280)
        checks.append(
            Check(
                "离线契约（常规音轨）",
                ok,
                f"解析 {len(regular)} 条音轨，最佳：{best.label if best else '无'}",
                "info" if ok else "error",
            )
        )
    except Exception as exc:  # noqa: BLE001 - doctor must never crash
        checks.append(Check("离线契约（常规音轨）", False, str(exc), "error"))
    try:
        flac = parse_playurl(
            _fixture_playurl("playurl_flac.json"),
            settings=settings,
            has_cookie=True,
        )
        best = best_stream(flac)
        ok = bool(best and best.lossless and best.quality_id == 30251)
        checks.append(
            Check(
                "离线契约（Hi-Res 无损）",
                ok,
                f"解析 {len(flac)} 条音轨，最佳：{best.label if best else '无'}",
                "info" if ok else "error",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("离线契约（Hi-Res 无损）", False, str(exc), "error"))
    return checks


async def run_doctor(settings: Settings, *, url: str | None = None, offline: bool = False) -> tuple[list[Check], int]:
    checks: list[Check] = []

    checks.append(
        Check(
            "运行环境",
            True,
            f"{APP_NAME} {__version__} · Python {sys.version.split()[0]} · 输出目录 {settings.output_dir}",
        )
    )
    index = settings.web_dir / "index.html"
    checks.append(
        Check(
            "Web UI 文件",
            index.is_file(),
            str(index) if index.is_file() else f"缺少 {index}（安装不完整）",
            "info" if index.is_file() else "error",
        )
    )

    ffmpeg = ffmpeg_version(settings)
    checks.append(
        Check(
            "ffmpeg",
            bool(ffmpeg),
            ffmpeg or "未找到 ffmpeg；macOS 可运行 brew install ffmpeg",
            "info" if ffmpeg else "error",
        )
    )
    ffprobe = shutil.which(settings.ffprobe)
    checks.append(
        Check(
            "ffprobe",
            bool(ffprobe),
            str(ffprobe or "未找到（不影响主流程，仅少一些元数据）"),
            "info" if ffprobe else "warning",
        )
    )

    try:
        settings.ensure_dirs()
        probe = settings.output_dir / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = shutil.disk_usage(settings.output_dir)
        checks.append(
            Check("输出目录", True, f"可写 · 剩余 {usage.free / 1024**3:.1f} GiB", "info")
        )
    except OSError as exc:
        checks.append(Check("输出目录", False, f"不可写：{exc}", "error"))

    auth = AuthStore(settings)
    source_labels = {
        "stored": "界面保存的本地配置",
        "env": "环境变量/.env",
        "file": "浏览器导出的 cookies.txt",
        "none": "未配置",
    }
    account = auth.account
    account_text = f"账号 {account.uname} (mid={account.mid})" if account else "尚未验证账号"
    checks.append(
        Check(
            "账号 Cookie",
            True,
            (
                f"已配置（{source_labels.get(auth.source, auth.source)}）· {account_text}"
                if auth.configured
                else "未配置；Hi-Res FLAC / Dolby 可能不可见（不一定是故障）"
            ),
            "info" if auth.configured else "warning",
        )
    )

    try:
        import yt_dlp

        checks.append(
            Check(
                "yt-dlp 兼容解析器",
                True,
                str(getattr(yt_dlp, "__version__", "已安装")),
                "info",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("yt-dlp 兼容解析器", False, f"不可用：{exc}", "error"))

    checks.extend(_offline_contract_check(settings))

    if offline:
        checks.append(Check("在线接口", True, "已跳过（--offline）", "info"))
    else:
        client = create_client(settings, with_cookies=False)
        auth.apply_to_client(client)
        chain = ExtractorChain(client, settings, auth)
        try:
            if auth.configured:
                try:
                    state = await auth.check(client)
                    name = (state.get("account") or {}).get("uname") or "未知账号"
                    checks.append(
                        Check("账号登录校验", True, f"Cookie 有效 · 当前账号 {name}", "info")
                    )
                except AppError as exc:
                    checks.append(
                        Check("账号登录校验", False, exc.user_message, "warning")
                    )
            await _live_check(url or DEFAULT_SAMPLE, checks, client, chain)
        finally:
            await client.aclose()

    critical = [c for c in checks if c.severity == "error" and not c.ok]
    return checks, (0 if not critical else 1)


async def _live_check(
    url: str,
    checks: list[Check],
    client: httpx.AsyncClient,
    chain: ExtractorChain,
) -> None:
    try:
        ref = await chain.resolve_url(url)
        result = await chain.extract(ref)
        best = best_stream(result.media.streams)
        if best:
            checks.append(
                Check(
                    "在线接口",
                    True,
                    (
                        f"《{result.media.title}》 · {len(result.media.streams)} 条音轨 · "
                        f"最佳 {best.label} · 解析器 {result.media.extractor}"
                        + ("（已降级到兼容解析）" if result.used_fallback else "")
                    ),
                    "warning" if result.used_fallback else "info",
                )
            )
            if result.media.warnings:
                checks.append(
                    Check("解析提示", True, " / ".join(result.media.warnings[:3]), "info")
                )
            keys = result.media.extra.get("playurl_keys")
            if keys:
                checks.append(
                    Check("playurl 顶层字段", True, ", ".join(str(k) for k in keys), "info")
                )
            quality_ids = ", ".join(
                f"{stream.quality_id}:{stream.label}" for stream in result.media.streams
            )
            checks.append(Check("候选音轨", True, quality_ids, "info"))
        else:
            checks.append(Check("在线接口", False, "没有解析到音轨", "error"))
        if result.media.extractor != "bilibili_api":
            checks.append(
                Check(
                    "官方 API 契约",
                    False,
                    "官方解析失败，已使用 yt-dlp。请检查接口变化，或先更新 yt-dlp。",
                    "warning",
                )
            )
        else:
            checks.append(Check("官方 API 契约", True, "view / playurl 字段可正常解析", "info"))
    except Exception as exc:  # noqa: BLE001 - doctor must survive any upstream weirdness
        message = getattr(exc, "user_message", str(exc))
        severity = "warning" if isinstance(exc, (AppError, httpx.HTTPError)) else "error"
        checks.append(
            Check(
                "在线接口",
                False,
                f"{message}（网络受限或被风控时也会出现；可加 --offline 跳过）",
                severity,
            )
        )

def print_report(checks: list[Check], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2))
        return
    icons = {"info": "✓", "warning": "!", "error": "✗"}
    print(f"\n{APP_NAME} doctor\n" + "=" * 60)
    for check in checks:
        icon = icons.get(check.severity, "·") if check.ok else "✗"
        print(f" {icon} {check.name}: {check.detail}")
    print("=" * 60)
    errors = [c for c in checks if not c.ok and c.severity == "error"]
    warnings = [c for c in checks if not c.ok and c.severity == "warning"]
    if errors:
        print(f"发现 {len(errors)} 个需要处理的问题。维护手册：MAINTENANCE.md")
    elif warnings:
        print(f"基本可用，{len(warnings)} 条提示可忽略或在需要时处理。")
    else:
        print("全部检查通过，可以开始下载。")
    print()


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / name
