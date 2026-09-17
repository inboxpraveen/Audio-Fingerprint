"""Application exception hierarchy.

Every exception carries a stable machine-readable ``code``, which the API returns
verbatim, and the HTTP status the API layer maps it to. Clients can branch on the
code and never have to parse the message.
"""

from __future__ import annotations

from typing import Any


class AudioFPError(Exception):
    """Base class for all AudioFP errors."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return self.message


class ConfigurationError(AudioFPError):
    """Invalid or inconsistent configuration, such as a bad env var or an unknown storage type."""

    code = "configuration_error"
    http_status = 500


class ValidationError(AudioFPError):
    """The caller supplied invalid input."""

    code = "validation_error"
    http_status = 400


class NotFoundError(AudioFPError):
    """A requested resource does not exist."""

    code = "not_found"
    http_status = 404


class AuthenticationError(AudioFPError):
    """Missing or invalid API key."""

    code = "unauthorized"
    http_status = 401


class ForbiddenError(AudioFPError):
    """The action is not permitted by configuration (e.g. path outside INDEX_ROOTS)."""

    code = "forbidden"
    http_status = 403


class ConflictError(AudioFPError):
    """The request conflicts with existing state (e.g. duplicate track)."""

    code = "conflict"
    http_status = 409


class PayloadTooLargeError(ValidationError):
    """Upload exceeds the configured size limit."""

    code = "payload_too_large"
    http_status = 413


class AudioProcessingError(AudioFPError):
    """The audio could not be decoded or fingerprinted."""

    code = "audio_processing_error"
    http_status = 422


class AudioDecodeError(AudioProcessingError):
    """A decoder (libsndfile / ffmpeg) rejected the input."""

    code = "audio_decode_error"


class UnsupportedFormatError(AudioProcessingError):
    """The format is not supported at all."""

    code = "unsupported_format"
    http_status = 415


class FFmpegNotFoundError(AudioProcessingError):
    """ffmpeg is needed for this input but is not installed or not on PATH."""

    code = "ffmpeg_not_found"
    http_status = 422


class StorageError(AudioFPError):
    """The storage backend failed."""

    code = "storage_error"
    http_status = 503


class FingerprintCompatibilityError(StorageError):
    """The database was built with different fingerprint parameters."""

    code = "fingerprint_incompatible"
    http_status = 503


class MatchingError(AudioFPError):
    """Matching failed for a reason other than bad input."""

    code = "matching_error"
    http_status = 500


class JobError(AudioFPError):
    """A background job could not be created or controlled."""

    code = "job_error"
    http_status = 400
