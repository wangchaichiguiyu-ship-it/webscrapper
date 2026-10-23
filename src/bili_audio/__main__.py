"""Command line entry point.

    biliaudio                 # start local web UI (default)
    biliaudio doctor          # environment + upstream contract diagnostics
    biliaudio doctor --offline
    biliaudio doctor --url https://www.bilibili.com/video/BV...
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket

from . import APP_NAME, __version__
from .config import Settings, load_settings, redact
from .converter import ffmpeg_version
from .diagnostics import print_report, run_doctor
from .server import run_server

logger = logging.getLogger("bili_audio")


class _RedactFilter(logging.Filter):
    """Defence-in-depth: redact cookie values even if a future log leaks one."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(str(record.msg))
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        except Exception:  # noqa: BLE001 - logging must never break the app
            return True
        return True


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO; that is noisy for a GUI app.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    redactor = _RedactFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


def _port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _choose_port(settings: Settings) -> Settings:
    if _port_is_free(settings.host, settings.port):
        return settings
    for offset in range(1, 21):
        candidate = settings.port + offset
        if _port_is_free(settings.host, candidate):
            print(
                f"端口 {settings.port} 已被占用，自动改用 {candidate}。"
                f"（可用 --port 指定其他端口）"
            )
            return settings.with_overrides(port=candidate)
    raise SystemExit(
        f"端口 {settings.port}-{settings.port + 20} 都被占用，请用 --port 指定空闲端口。"
    )


def _print_banner(settings: Settings) -> None:
    ffmpeg = ffmpeg_version(settings)
    loopback = settings.host in {"127.0.0.1", "localhost", "::1"}
    print()
    print(f"  {APP_NAME} {__version__}")
    print(f"  Web UI   {settings.url}")
    print(f"  输出目录  {settings.output_dir}")
    print(f"  ffmpeg   {ffmpeg or '未找到（转换会失败，请安装 ffmpeg）'}")
    print(f"  格式/并发 默认 {settings.default_format.upper()} · {settings.max_concurrent} 个任务")
    print("  关闭服务  Ctrl+C")
    if not loopback:
        print("\n  ⚠️  警告：当前绑定到非本机地址，界面没有认证，请只在可信网络使用。")
    print()


def _build_settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    settings = settings.with_overrides(
        output_dir=getattr(args, "output_dir", None),
        host=getattr(args, "host", None),
        port=getattr(args, "port", None),
        open_browser=False if getattr(args, "no_browser", False) else None,
        max_concurrent=getattr(args, "concurrency", None),
        default_format=getattr(args, "format", None),
        log_level=getattr(args, "log_level", None),
    )
    settings.validate()
    return settings


def _add_serve_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765，占用时自动顺延）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不要自动打开浏览器")
    parser.add_argument("--output-dir", default=None, help="音频输出目录")
    parser.add_argument("--format", choices=("flac", "wav", "m4a"), default=None, help="默认输出格式")
    parser.add_argument("--concurrency", type=int, default=None, help="同时处理任务数（1-4）")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="日志级别",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biliaudio",
        description=f"{APP_NAME}: 下载 B 站视频当前可用的最高音质音频",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="启动本地 Web UI（默认命令）")
    _add_serve_args(serve)

    doctor = subparsers.add_parser("doctor", help="检查 ffmpeg、目录、Cookie 与 B 站接口契约")
    doctor.add_argument("--offline", action="store_true", help="只跑离线契约测试，不访问网络")
    doctor.add_argument("--url", default=None, help="用指定视频 URL 做在线检查")
    doctor.add_argument("--json", action="store_true", help="输出 JSON，便于 CI/脚本")
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="出现 warning 也以退出码 2 结束（适合 CI 监控上游契约）",
    )
    doctor.add_argument("--log-level", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `biliaudio` with no subcommand behaves like `biliaudio serve`.
    if args.command is None:
        args.command = "serve"

    if args.command == "doctor":
        settings = _build_settings(args)
        _configure_logging(settings.log_level)
        checks, code = asyncio.run(run_doctor(settings, url=args.url, offline=args.offline))
        print_report(checks, as_json=args.json)
        if args.strict and any(c.severity == "warning" and not c.ok for c in checks):
            return max(code, 2)
        return code

    settings = _build_settings(args)
    settings = _choose_port(settings)
    _configure_logging(settings.log_level)
    _print_banner(settings)
    run_server(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
