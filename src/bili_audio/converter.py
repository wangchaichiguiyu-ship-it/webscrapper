"""ffmpeg wrapper: probe, lossless extraction/encapsulation, progress.

Conversion policy:
* ``flac``: always encode FLAC. For a lossy AAC source this is a lossless
  *encapsulation* (it cannot restore discarded detail); for a Hi-Res FLAC source
  it remains bit-perfect PCM.
* ``wav``:  decode to PCM. 24-bit for lossless sources, 16-bit for AAC.
* ``m4a``:  keep the original codec with stream copy when possible. A Hi-Res
  source stays FLAC (``.flac``) because FLAC cannot live in an m4a container.

ffmpeg is driven with small arguments and its JSON/line progress protocol; no
shell is involved, so titles cannot inject commands.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .errors import ConversionError, FfmpegMissingError, PipelineCancelled
from .models import MediaInfo
from .naming import extension_for
from .quality import AudioStream

logger = logging.getLogger(__name__)

FFMPEG_TIMEOUT_FLOOR = 30.0
_TAIL_LIMIT = 12_000


@dataclass
class ProbeResult:
    duration: float = 0.0
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    bit_rate: int = 0
    has_audio: bool = False


def ffmpeg_version(settings: Settings) -> str | None:
    binary = shutil.which(settings.ffmpeg) or settings.ffmpeg
    if not binary:
        return None
    try:
        import subprocess

        proc = subprocess.run(
            [binary, "-version"], capture_output=True, text=True, timeout=5, check=False
        )
        if proc.returncode != 0:
            return None
        first = (proc.stdout or proc.stderr or "").splitlines()
        return first[0].strip() if first else "ffmpeg"
    except (OSError, subprocess.SubprocessError):
        return None


async def probe_audio(path: Path, settings: Settings) -> ProbeResult:
    """Best-effort ffprobe metadata; never fatal if ffprobe is unavailable."""
    ffprobe = shutil.which(settings.ffprobe) or settings.ffprobe
    if not ffprobe:
        return ProbeResult()
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bit_rate,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return ProbeResult()
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return ProbeResult()
    streams = payload.get("streams") or []
    if not isinstance(streams, list) or not streams:
        return ProbeResult()
    stream = streams[0] if isinstance(streams[0], dict) else {}
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _float(stream.get("duration")) or _float(fmt.get("duration"))
    return ProbeResult(
        duration=duration,
        codec=str(stream.get("codec_name") or ""),
        sample_rate=int(_float(stream.get("sample_rate"))),
        channels=int(_float(stream.get("channels"))),
        bit_rate=int(_float(stream.get("bit_rate"))),
        has_audio=True,
    )


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def output_extension(target_format: str, source: AudioStream) -> str:
    return extension_for(target_format, source.container, source.lossless)


def _build_args(
    input_path: Path,
    output_path: Path,
    target_format: str,
    source: AudioStream,
    probe: ProbeResult,
    media: MediaInfo,
) -> list[str]:
    args = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
    ]
    if target_format == "wav":
        codec = "pcm_s24le" if source.lossless or probe.codec == "flac" else "pcm_s16le"
        args += ["-c:a", codec]
    elif target_format == "m4a" and not source.lossless and source.container != "flac":
        args += ["-c:a", "copy", "-movflags", "+faststart"]
    else:
        # FLAC target, or an original-Hi-Res request that must stay FLAC.
        args += ["-c:a", "flac", "-compression_level", "5"]

    # Explicit metadata keeps files readable in Music/QuickTime/Finder preview.
    metadata = {
        "title": media.title,
        "artist": media.uploader or "Bilibili",
        "album": "Bilibili",
        "comment": media.canonical_url,
    }
    for key, value in metadata.items():
        if value:
            args += ["-metadata", f"{key}={_clean_metadata(value)}"]
    args.append(str(output_path))
    return args


def _clean_metadata(value: str) -> str:
    return " ".join(str(value).replace("\x00", "").splitlines()).strip()[:1000]


async def _read_stderr(proc: asyncio.subprocess.Process) -> str:
    assert proc.stderr is not None
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await proc.stderr.read(4096)
        if not chunk:
            break
        if size < _TAIL_LIMIT:
            chunks.append(chunk)
            size += len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


async def _read_progress(
    proc: asyncio.subprocess.Process,
    duration: float,
    on_progress: Callable[[float], None] | None,
) -> None:
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text or "=" not in text:
            continue
        key, _, value = text.partition("=")
        seconds = 0.0
        if key == "out_time_us":
            seconds = _float(value) / 1_000_000.0
        elif key == "out_time_ms":
            # ffmpeg historically emits microseconds here despite the name.
            seconds = _float(value) / 1_000_000.0
        elif key == "out_time":
            seconds = _parse_hms(value)
        elif key == "progress" and value == "end":
            if on_progress:
                on_progress(1.0)
            continue
        else:
            continue
        if duration > 0 and on_progress:
            on_progress(max(0.0, min(seconds / duration, 0.99)))


def _parse_hms(value: str) -> float:
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def convert(
    input_path: Path,
    output_path: Path,
    target_format: str,
    source: AudioStream,
    media: MediaInfo,
    settings: Settings,
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[int, ProbeResult]:
    """Convert ``input_path`` to ``output_path``; returns (bytes, probe)."""
    ffmpeg = shutil.which(settings.ffmpeg) or settings.ffmpeg
    if not ffmpeg:
        raise FfmpegMissingError(
            "没有找到 ffmpeg",
            hint="在 macOS 上运行 `brew install ffmpeg`，然后在 .env 中设置 BILI_FFMPEG。",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    cancel_event = cancel_event or asyncio.Event()
    if cancel_event.is_set():
        raise PipelineCancelled()

    probe = await probe_audio(input_path, settings)
    duration = probe.duration or media.duration or 0.0
    args = _build_args(input_path, output_path, target_format, source, probe, media)
    # Use the resolved binary instead of relying on PATH inside the subprocess.
    args[0] = ffmpeg

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise FfmpegMissingError(
            "无法启动 ffmpeg",
            hint="确认 ffmpeg 已安装，且 .env 中的 BILI_FFMPEG 路径正确。",
        ) from exc
    except OSError as exc:
        raise ConversionError(f"无法启动 ffmpeg：{exc}") from exc

    stderr_task = asyncio.create_task(_read_stderr(proc))
    progress_task = asyncio.create_task(_read_progress(proc, duration, on_progress))
    try:
        timeout = max(FFMPEG_TIMEOUT_FLOOR, settings.convert_timeout)
        started = time.monotonic()
        while True:
            if cancel_event.is_set():
                await _terminate(proc)
                raise PipelineCancelled()
            if time.monotonic() - started > timeout:
                await _terminate(proc)
                raise ConversionError(
                    "音频转换超时",
                    hint="视频可能过长或磁盘过慢，可调大 BILI_CONVERT_TIMEOUT。",
                )
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.25)
                break
            except asyncio.TimeoutError:
                continue
        stderr = await stderr_task
        await progress_task
        if proc.returncode != 0:
            output_path.unlink(missing_ok=True)
            tail = _tail(stderr)
            raise ConversionError(
                "音频转换失败",
                hint=_friendly_ffmpeg_error(tail),
                extra={"ffmpeg": tail[-1200:]},
            )
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ConversionError("ffmpeg 没有生成有效的输出文件")
        if on_progress:
            on_progress(1.0)
        return output_path.stat().st_size, probe
    except asyncio.CancelledError:
        await _terminate(proc)
        output_path.unlink(missing_ok=True)
        raise
    finally:
        for task in (stderr_task, progress_task):
            if not task.done():
                task.cancel()
        for task in (stderr_task, progress_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - progress/stderr readers are best-effort
                logger.debug("ffmpeg reader task ended with an error", exc_info=True)


def _tail(text: str) -> str:
    return (text or "").strip()[-1600:]


def _friendly_ffmpeg_error(tail: str) -> str:
    lower = tail.lower()
    if "invalid data found" in lower or "moov atom not found" in lower:
        return "输入音频损坏或不完整，请重试下载。"
    if "no such file" in lower:
        return "找不到输入文件，临时文件可能已被清理。"
    if "unknown encoder" in lower:
        return "当前 ffmpeg 不支持目标编码器，请更新 ffmpeg。"
    if "permission denied" in lower:
        return "没有写入输出目录的权限。"
    if "disk" in lower or "no space" in lower:
        return "磁盘空间不足。"
    return tail or "请查看服务日志获取 ffmpeg 详细输出。"
