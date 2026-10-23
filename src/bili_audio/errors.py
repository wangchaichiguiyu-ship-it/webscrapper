"""Typed, user-presentable errors.

Every error that can reach the UI carries a Chinese ``message`` plus a stable
machine-readable ``code``. Unexpected exceptions are never echoed verbatim to
the browser; they are logged and replaced with a generic message.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected failures."""

    code = "app_error"
    status = 400

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.extra = extra or {}

    @property
    def user_message(self) -> str:
        return f"{self.message} {self.hint}" if self.hint else self.message

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "detail": self.user_message,
            "code": self.code,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.extra)
        return payload


class ConfigError(AppError):
    code = "config_error"
    status = 500


class AuthError(AppError):
    code = "auth_error"
    status = 400


class AuthNotConfiguredError(AuthError):
    code = "auth_not_configured"
    status = 400


class InvalidCookieError(AuthError):
    code = "invalid_cookie"
    status = 400


class UnsupportedUrlError(AppError):
    code = "unsupported_url"
    status = 400


class InvalidUrlError(AppError):
    code = "invalid_url"
    status = 400


class DuplicateTaskError(AppError):
    code = "duplicate_task"
    status = 409


class TaskNotFoundError(AppError):
    code = "task_not_found"
    status = 404


class TaskStateError(AppError):
    code = "task_state"
    status = 409


class QueueFullError(AppError):
    code = "queue_full"
    status = 429


class UpstreamError(AppError):
    """Bilibili answered, but with an error or an unusable payload."""

    code = "upstream_error"
    status = 502

    def __init__(
        self,
        message: str,
        *,
        bili_code: int | None = None,
        http_status: int | None = None,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, hint=hint, extra=extra)
        self.bili_code = bili_code
        self.http_status = http_status


class SchemaChangedError(UpstreamError):
    """The JSON contract changed in a way this version cannot parse.

    The extractor chain reacts to this by trying the yt-dlp fallback and the
    doctor command surfaces it as a maintenance signal.
    """

    code = "upstream_schema_changed"


class MediaExpiredError(AppError):
    code = "media_expired"
    status = 502


class DownloadError(AppError):
    code = "download_error"
    status = 502


class ConversionError(AppError):
    code = "conversion_error"
    status = 500


class FfmpegMissingError(AppError):
    code = "ffmpeg_missing"
    status = 503


class PipelineCancelled(Exception):
    """Internal control-flow exception, never shown to the user."""
