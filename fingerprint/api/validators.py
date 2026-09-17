"""Request parsing helpers that turn bad input into 400s with useful messages."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from werkzeug.datastructures import FileStorage

from .. import formats
from ..utils.exceptions import ValidationError

MAX_PER_PAGE = 200
MAX_JSON_BODY_BYTES = 1024 * 1024  # JSON endpoints never need more than this; uploads use multipart


def check_json_body_size(request: Any) -> None:
    """Refuse JSON bodies that are oversized or of unknown (chunked) length before reading them."""
    length = request.content_length  # None for chunked transfer encoding or when there is no body at all
    if length is None:
        if request.headers.get("Transfer-Encoding"):
            raise ValidationError("A Content-Length header is required for JSON requests", code="length_required", http_status=411)
        return
    if length > MAX_JSON_BODY_BYTES:
        raise ValidationError(f"JSON body too large (max {MAX_JSON_BODY_BYTES // 1024} KB)", code="payload_too_large", http_status=413)


def parse_int(source: Mapping[str, Any], key: str, default: int | None = None, *, minimum: int | None = None, maximum: int | None = None) -> int | None:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"'{key}' must be an integer", details={"field": key, "value": raw}) from None
    if minimum is not None and value < minimum:
        raise ValidationError(f"'{key}' must be >= {minimum}", details={"field": key, "value": value})
    if maximum is not None and value > maximum:
        raise ValidationError(f"'{key}' must be <= {maximum}", details={"field": key, "value": value})
    return value


def parse_float(
    source: Mapping[str, Any], key: str, default: float | None = None, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"'{key}' must be a number", details={"field": key, "value": raw}) from None
    if minimum is not None and value < minimum:
        raise ValidationError(f"'{key}' must be >= {minimum}", details={"field": key, "value": value})
    if maximum is not None and value > maximum:
        raise ValidationError(f"'{key}' must be <= {maximum}", details={"field": key, "value": value})
    return value


def parse_bool(source: Mapping[str, Any], key: str, default: bool = False) -> bool:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    lowered = str(raw).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValidationError(f"'{key}' must be true or false", details={"field": key, "value": raw})


def parse_choice(source: Mapping[str, Any], key: str, choices: tuple[str, ...], default: str | None = None) -> str | None:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    value = str(raw).strip().lower()
    if value not in choices:
        raise ValidationError(f"'{key}' must be one of: {', '.join(choices)}", details={"field": key, "value": raw})
    return value


def parse_pagination(args: Mapping[str, Any], default_per_page: int = 50) -> tuple[int, int]:
    page = parse_int(args, "page", 1, minimum=1) or 1
    per_page = parse_int(args, "per_page", default_per_page, minimum=1, maximum=MAX_PER_PAGE) or default_per_page
    return page, per_page


def require_upload(files: Mapping[str, FileStorage], field: str = "audio") -> FileStorage:
    """Return the uploaded file or raise a helpful validation error."""
    upload = files.get(field)
    if upload is None:
        # Be forgiving about the field name: accept the first file if exactly one was sent.
        if len(files) == 1:
            upload = next(iter(files.values()))
        else:
            raise ValidationError(
                f"No file uploaded. Send the audio as multipart/form-data in the '{field}' field.",
                details={"field": field},
            )
    if not upload or not upload.filename:
        raise ValidationError("The uploaded file has no name.", details={"field": field})
    ext = formats.extension_of(upload.filename)
    if not ext:
        raise ValidationError(
            "The uploaded file has no extension, so its format cannot be determined.",
            details={"filename": upload.filename},
        )
    if ext not in formats.SUPPORTED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(e.lstrip('.') for e in formats.SUPPORTED_EXTENSIONS))}",
            code="unsupported_format",
            http_status=415,
            details={"extension": ext},
        )
    return upload


def clean_directory_path(raw: Any) -> str:
    """Normalise a directory path without touching the filesystem (existence is checked after authorisation)."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("'directory_path' is required", details={"field": "directory_path"})
    return os.path.abspath(os.path.expanduser(raw.strip()))
