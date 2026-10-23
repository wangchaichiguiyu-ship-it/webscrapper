"""FastAPI application: JSON API, Server-Sent Events and static web UI."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import mimetypes
import shutil
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import APP_NAME, __version__
from .config import FORMAT_META, Settings
from .converter import ffmpeg_version
from .errors import AppError, TaskNotFoundError
from .extractors import ExtractorChain
from .http_client import create_client
from .tasks import EventBus, TaskManager

logger = logging.getLogger(__name__)


class AddTaskPayload(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    format: Literal["flac", "wav", "m4a"] | None = None


class ClearPayload(BaseModel):
    scope: Literal["finished"] = "finished"


class RevealPayload(BaseModel):
    task_id: str | None = None


def _meta(settings: Settings, ffmpeg: str | None, started_at: float) -> dict[str, Any]:
    try:
        free = shutil.disk_usage(settings.output_dir).free
    except OSError:
        free = None
    return {
        "app": APP_NAME,
        "version": __version__,
        "output_dir": str(settings.output_dir),
        "ffmpeg_ok": bool(ffmpeg),
        "ffmpeg_version": ffmpeg or "",
        "cookie_configured": settings.has_cookie,
        "extractor_chain": ["bilibili_api", "yt_dlp"],
        "max_concurrent": settings.max_concurrent,
        "max_tasks": settings.max_tasks,
        "default_format": settings.default_format,
        "formats": list(FORMAT_META),
        "quality_policy": (
            "优先 Hi-Res 无损 FLAC > Dolby Atmos > 最高码率 AAC；"
            "只有候选音轨真实下载失败时才降级。"
        ),
        "source_note": (
            "B 站绝大多数视频最高为 192 kbps AAC（有损）；Hi-Res FLAC 仅部分视频、"
            "且通常需要登录 Cookie。转为 FLAC 不会提升有损源的音质。"
        ),
        "disk_free_bytes": free,
        "uptime_seconds": round(time.monotonic() - started_at, 1),
    }


async def _open_in_finder(path: Path, *, reveal_file: bool = False) -> None:
    if sys.platform == "darwin":
        args = ["open", "-R", str(path)] if reveal_file else ["open", str(path)]
    elif sys.platform.startswith("win"):
        args = ["explorer", f"/select,{path}"] if reveal_file else ["explorer", str(path)]
    else:
        args = ["xdg-open", str(path.parent if reveal_file else path)]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError as exc:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        raise AppError("打开 Finder 超时") from exc
    except OSError as exc:
        raise AppError(f"无法打开 Finder：{exc}") from exc
    if proc is not None and proc.returncode != 0:
        raise AppError("无法打开 Finder（可能在没有图形界面的环境中运行）")


def _normalize_host_header(value: str) -> str:
    """Strip port and IPv6 brackets from a Host header value."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _host_is_allowed(host_header: str, settings: Settings) -> bool:
    """Reject DNS-rebinding Host headers.

    Loopback names are always allowed. A specific configured bind host is
    allowed by name. When binding a wildcard address, only IP literals and
    loopback names are allowed: an attacker-controlled domain can no longer
    resolve to the local server and become same-origin.
    """
    host = _normalize_host_header(host_header)
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    configured = _normalize_host_header(settings.host)
    if settings.host not in {"0.0.0.0", "::", ""}:
        return host == configured or host in {"127.0.0.1", "localhost", "::1"}
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _origin_is_allowed(origin: str, host_header: str) -> bool:
    if not origin or origin == "null":
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    return _normalize_host_header(parsed.netloc) == _normalize_host_header(host_header)


def create_app(settings: Settings) -> FastAPI:
    started_at = time.monotonic()
    ffmpeg_desc = ffmpeg_version(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.validate()
        settings.ensure_dirs()
        client = create_client(settings)
        chain = ExtractorChain(client, settings)
        manager = TaskManager(settings, chain, EventBus())
        app.state.settings = settings
        app.state.client = client
        app.state.chain = chain
        app.state.manager = manager
        app.state.started_at = started_at
        await manager.start()
        logger.info("BiliSonic ready on %s (output: %s)", settings.url, settings.output_dir)
        if not ffmpeg_desc:
            logger.warning("ffmpeg not found! Downloads will resolve, conversion will fail.")
        try:
            yield
        finally:
            await manager.stop()
            await client.aclose()
            logger.info("BiliSonic stopped")

    app = FastAPI(
        title=APP_NAME,
        version=__version__,
        description="Local Bilibili best-quality audio downloader",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not _host_is_allowed(host_header, settings):
            return JSONResponse(
                {"detail": "非法的 Host 请求头（服务仅允许本机访问）", "code": "invalid_host"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        # CSRF defence for a localhost service: a malicious website must not be
        # able to enqueue/cancel/reveal via a simple cross-origin POST. Non-browser
        # clients (curl/scripts) may omit Origin; browsers always send it on POST.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "")
            if origin:
                if not _origin_is_allowed(origin, host_header):
                    return JSONResponse(
                        {"detail": "跨站请求被拒绝", "code": "cross_origin_denied"},
                        status_code=403,
                        headers={"Cache-Control": "no-store"},
                    )
            elif request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse(
                    {"detail": "跨站请求被拒绝", "code": "cross_origin_denied"},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "connect-src 'self'; media-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        if exc.status >= 500:
            logger.warning("API error [%s]: %s", exc.code, exc.message)
        return JSONResponse(exc.to_payload(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "请求参数不合法",
                "code": "validation_error",
                "errors": exc.errors()[:5],
            },
            status_code=422,
        )

    @app.exception_handler(TaskNotFoundError)
    async def not_found_handler(_request: Request, exc: TaskNotFoundError) -> JSONResponse:
        return JSONResponse(exc.to_payload(), status_code=exc.status)

    # -- API ---------------------------------------------------------------
    @app.get("/api/meta")
    async def get_meta() -> dict[str, Any]:
        return _meta(settings, ffmpeg_desc, started_at)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        snapshot = manager.snapshot()
        return {
            "status": "ok",
            "version": __version__,
            "ffmpeg_ok": bool(ffmpeg_desc),
            "tasks": snapshot["stats"],
        }

    @app.get("/api/tasks")
    async def list_tasks() -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        return manager.snapshot()

    @app.post("/api/tasks", status_code=201)
    async def add_task(payload: AddTaskPayload) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        task = await manager.enqueue(payload.url, payload.format)
        return {"task": task.to_public()}

    @app.post("/api/tasks/clear")
    async def clear_tasks(payload: ClearPayload | None = None) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        removed = await manager.clear_finished()
        return {"removed": removed}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        return {"task": manager.get_public(task_id)}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        await manager.delete(task_id)
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        task = await manager.cancel(task_id)
        return {"task": task.to_public()}

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_task(task_id: str) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        task = await manager.retry(task_id)
        return {"task": task.to_public()}

    @app.get("/api/tasks/{task_id}/audio")
    async def get_audio(task_id: str, download: bool = Query(default=False)):
        manager: TaskManager = app.state.manager
        task = manager.get(task_id)
        path = manager.audio_path(task_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(
            path,
            media_type=media_type,
            filename=task.output_filename or path.name,
            content_disposition_type="attachment" if download else "inline",
        )

    @app.post("/api/reveal")
    async def reveal(payload: RevealPayload | None = None) -> dict[str, Any]:
        manager: TaskManager = app.state.manager
        path = settings.output_dir
        reveal_file = False
        if payload and payload.task_id:
            task = manager.get(payload.task_id)
            if task.output_path and task.output_path.is_file():
                path = task.output_path
                reveal_file = True
        await _open_in_finder(path, reveal_file=reveal_file)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        manager: TaskManager = app.state.manager
        queue = manager.bus.subscribe()

        async def generator() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                yield 'data: {"type":"snapshot"}\n\n'
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                manager.bus.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- static UI ---------------------------------------------------------
    web_dir = settings.web_dir
    if web_dir.is_dir():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")
    else:
        logger.warning("web/ directory not found; UI will be unavailable until it exists")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        path = web_dir / "favicon.svg"
        if path.is_file():
            return FileResponse(path, media_type="image/svg+xml")
        return JSONResponse({"detail": "not found"}, status_code=404)

    @app.get("/", include_in_schema=False)
    async def index():
        path = web_dir / "index.html"
        if not path.is_file():
            return JSONResponse(
                {"detail": "web/index.html 不存在，请检查安装是否完整"},
                status_code=503,
            )
        return FileResponse(path, media_type="text/html; charset=utf-8")

    return app


def run_server(settings: Settings) -> None:
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
        server_header=False,
        date_header=False,
    )
    server = uvicorn.Server(config)

    async def _main() -> None:
        server_task = asyncio.create_task(server.serve())
        while not server.started:
            if server_task.done():
                await server_task
                return
            await asyncio.sleep(0.05)
        url = settings.url
        logger.info("%s -> %s", APP_NAME, url)
        if settings.open_browser:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:  # pragma: no cover - headless
                logger.debug("Could not open browser", exc_info=True)
        await server_task

    asyncio.run(_main())
