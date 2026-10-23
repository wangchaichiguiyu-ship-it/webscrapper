"""Runtime configuration.

Precedence: CLI arguments > environment variables > ``.env`` > defaults.
Secrets (Bilibili cookies) are only ever held in memory and are never included
in API responses or logs.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = PROJECT_ROOT / "web"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
SUPPORTED_FORMATS = ("flac", "wav", "m4a")
FORMAT_META = (
    {
        "id": "flac",
        "label": "FLAC 无损",
        "description": "无损封装，兼容性最好；源为 AAC 时不会凭空提升音质",
        "ext": "flac",
    },
    {
        "id": "wav",
        "label": "WAV 无损",
        "description": "未压缩 PCM，体积最大；适合再编辑",
        "ext": "wav",
    },
    {
        "id": "m4a",
        "label": "原始音轨",
        "description": "不二次转码：AAC 保持 m4a，Hi-Res 保持 flac",
        "ext": "m4a",
    },
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值：{raw!r}") from exc


def load_dotenv(path: Path | None = None) -> None:
    """Minimal, dependency-free .env loader (does not override real env vars)."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_netscape_cookies(path: Path) -> dict[str, str]:
    """Extract Bilibili cookies from a Netscape cookies.txt file."""
    cookies: dict[str, str] = {}
    try:
        lines = path.expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ConfigError(f"无法读取 Cookie 文件：{path}") from exc
    for line in lines:
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _expires, name, value = parts[:7]
        if "bilibili.com" in domain:
            cookies[name] = value
    return cookies


def cookie_header_from_env() -> str:
    raw = os.environ.get("BILI_COOKIE", "").strip()
    if raw:
        if "=" not in raw:
            return f"SESSDATA={raw}"
        return raw
    sess = os.environ.get("BILI_SESSDATA", "").strip()
    if sess:
        return f"SESSDATA={sess}"
    return ""


def _find_binary(name: str) -> str:
    if os.path.sep in name:
        return name
    return shutil.which(name) or name


@dataclass(frozen=True)
class Settings:
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "output")
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    max_concurrent: int = 1
    max_tasks: int = 300
    default_format: str = "flac"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    cookie: str = ""
    cookie_file: Path | None = None
    user_agent: str = DEFAULT_UA
    proxy: str | None = None
    log_level: str = "INFO"
    download_retries: int = 3
    convert_timeout: float = 900.0

    @property
    def has_cookie(self) -> bool:
        return bool(self.cookie) or bool(self.cookie_file and self.cookie_file.is_file())

    @property
    def web_dir(self) -> Path:
        return WEB_DIR

    @property
    def url(self) -> str:
        host = self.host
        if host in {"0.0.0.0", ""}:
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @property
    def work_dir(self) -> Path:
        # Hidden, on the same volume as the output so os.replace() is atomic.
        return self.output_dir / ".work"

    @property
    def cookie_jar(self) -> dict[str, str]:
        cookies: dict[str, str] = {}
        if self.cookie_file:
            cookies.update(parse_netscape_cookies(self.cookie_file))
        if self.cookie:
            for chunk in self.cookie.split(";"):
                name, sep, value = chunk.strip().partition("=")
                if sep and name.strip():
                    cookies[name.strip()] = value.strip()
        return cookies

    def ensure_dirs(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"无法创建输出目录 {self.output_dir}：{exc}") from exc

    def validate(self) -> None:
        if self.default_format not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"不支持的输出格式 {self.default_format!r}，可选：{', '.join(SUPPORTED_FORMATS)}"
            )
        if not 1 <= self.max_concurrent <= 4:
            raise ConfigError("BILI_MAX_CONCURRENT 必须在 1 到 4 之间")
        if not 1 <= self.max_tasks <= 2000:
            raise ConfigError("BILI_MAX_TASKS 必须在 1 到 2000 之间")
        if not 1 <= self.port <= 65535:
            raise ConfigError("端口号必须在 1 到 65535 之间")
        if self.cookie_file and not self.cookie_file.is_file():
            raise ConfigError(f"Cookie 文件不存在：{self.cookie_file}")
        object.__setattr__(self, "ffmpeg", _find_binary(self.ffmpeg))
        object.__setattr__(self, "ffprobe", _find_binary(self.ffprobe))

    def with_overrides(
        self,
        *,
        output_dir: str | Path | None = None,
        host: str | None = None,
        port: int | None = None,
        open_browser: bool | None = None,
        max_concurrent: int | None = None,
        default_format: str | None = None,
        log_level: str | None = None,
    ) -> Settings:
        changes: dict[str, object] = {}
        if output_dir is not None:
            changes["output_dir"] = Path(output_dir).expanduser().resolve()
        if host is not None:
            changes["host"] = host
        if port is not None:
            changes["port"] = port
        if open_browser is not None:
            changes["open_browser"] = open_browser
        if max_concurrent is not None:
            changes["max_concurrent"] = max_concurrent
        if default_format is not None:
            changes["default_format"] = default_format
        if log_level is not None:
            changes["log_level"] = log_level.upper()
        return replace(self, **changes)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from .env + environment variables."""
    load_dotenv()
    env = env if env is not None else os.environ

    output_raw = env.get("BILI_OUTPUT_DIR", "").strip()
    output_dir = (
        Path(output_raw).expanduser().resolve()
        if output_raw
        else (PROJECT_ROOT / "output").resolve()
    )

    cookie_file_raw = env.get("BILI_COOKIE_FILE", "").strip()
    cookie_file = Path(cookie_file_raw).expanduser() if cookie_file_raw else None

    settings = Settings(
        output_dir=output_dir,
        host=env.get("BILI_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int("BILI_PORT", 8765),
        max_concurrent=_env_int("BILI_MAX_CONCURRENT", 1),
        max_tasks=_env_int("BILI_MAX_TASKS", 300),
        default_format=env.get("BILI_AUDIO_FORMAT", "flac").strip().lower() or "flac",
        ffmpeg=env.get("BILI_FFMPEG", "ffmpeg").strip() or "ffmpeg",
        ffprobe=env.get("BILI_FFPROBE", "ffprobe").strip() or "ffprobe",
        cookie=cookie_header_from_env(),
        cookie_file=cookie_file,
        user_agent=env.get("BILI_USER_AGENT", DEFAULT_UA).strip() or DEFAULT_UA,
        proxy=(env.get("BILI_PROXY", "").strip() or None),
        log_level=(env.get("BILI_LOG_LEVEL", "INFO").strip() or "INFO").upper(),
        download_retries=_env_int("BILI_DOWNLOAD_RETRIES", 3),
        convert_timeout=float(_env_int("BILI_CONVERT_TIMEOUT", 900)),
    )
    settings.validate()
    return settings


_SAFE_LOG_RE = re.compile(r"(SESSDATA|bili_jct|buvid3|buvid4)=([^;\s]+)", re.IGNORECASE)


def redact(text: str) -> str:
    """Redact cookie-like values from log strings."""
    return _SAFE_LOG_RE.sub(lambda m: f"{m.group(1)}=***", text)
