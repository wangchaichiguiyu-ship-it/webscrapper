"""Safe, readable output file naming.

Files are named after the video title. Multi-part videos get `` - Pn`` appended
so two parts cannot overwrite each other. No user-controlled path segment is
ever used directly: separators and traversal characters are stripped and the
result is always joined to the configured output directory as a basename.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WS = re.compile(r"\s+")
_DOTS = re.compile(r"^[.\s]+|[.\s]+$")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Trim to a UTF-8 byte budget so APFS's 255-byte name limit is respected."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    raw = raw[:max_bytes]
    # Drop a partial multi-byte sequence at the cut point.
    while raw and (raw[-1] & 0xC0) == 0x80:
        raw = raw[:-1]
    if raw and raw[-1] & 0x80:
        raw = raw[:-1]
    return raw.decode("utf-8", errors="ignore").rstrip(" .")


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFKC", name or "")
    name = _UNSAFE.sub(" ", name)
    name = _WS.sub(" ", name).strip(" .")
    name = _DOTS.sub("", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    # macOS/Linux impose a bytes limit, not a code-point limit. Chinese titles
    # are 3 bytes/char, so a 120-character cap alone can still exceed 255 bytes.
    name = _truncate_utf8(name, 200)
    if not name:
        return "未命名音频"
    # macOS treats a leading dot as hidden; avoid surprising users.
    if name.startswith("."):
        name = "_" + name.lstrip(".")
    return name


def build_stem(title: str, page: int = 1, page_title: str | None = None) -> str:
    base = sanitize_filename(title)
    if page and page > 1:
        part = sanitize_filename(page_title, max_len=60) if page_title else ""
        if part and part.casefold() not in base.casefold():
            return sanitize_filename(f"{base} - P{page} {part}", max_len=150)
        return sanitize_filename(f"{base} - P{page}", max_len=150)
    return base


def extension_for(target_format: str, source_container: str, source_is_lossless: bool) -> str:
    if target_format == "flac":
        return "flac"
    if target_format == "wav":
        return "wav"
    if target_format in {"m4a", "source"}:
        # "original" means original codec: a Hi-Res source stays FLAC.
        return "flac" if source_is_lossless or source_container == "flac" else "m4a"
    return "flac"


def unique_path(directory: Path, stem: str, ext: str, reserved: set[str] | None = None) -> Path:
    """Return an unused ``stem.ext`` path, adding `` (2)``, `` (3)`` as needed."""
    reserved = reserved or set()
    candidate = directory / f"{stem}.{ext}"
    index = 2
    while candidate.exists() or str(candidate) in reserved:
        candidate = directory / f"{stem} ({index}).{ext}"
        index += 1
    return candidate


def is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False
