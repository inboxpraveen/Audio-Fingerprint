"""Uniform JSON error responses.

Every error - ours, Flask's, or an unexpected exception - is rendered as::

    {"error": "<human message>", "code": "<machine code>", "details": {...}, "request_id": "..."}

``error`` stays a plain string for backwards compatibility with 1.x clients.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, g, jsonify
from werkzeug.exceptions import HTTPException

from ..utils.exceptions import AudioFPError

logger = logging.getLogger(__name__)

_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "too_many_requests",
    500: "internal_error",
    503: "service_unavailable",
}


def error_response(message: str, status: int = 400, code: str | None = None, details: dict[str, Any] | None = None):
    payload: dict[str, Any] = {"error": message, "code": code or _HTTP_CODES.get(status, "error"), "status": status}
    if details:
        payload["details"] = details
    request_id = getattr(g, "request_id", None)
    if request_id:
        payload["request_id"] = request_id
    response = jsonify(payload)
    response.status_code = status
    return response


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AudioFPError)
    def _handle_audiofp(exc: AudioFPError):
        if exc.http_status >= 500:
            logger.error("%s: %s", exc.code, exc.message, exc_info=exc.http_status >= 500 and exc.code == "internal_error")
        else:
            logger.info("%s: %s", exc.code, exc.message)
        return error_response(exc.message, exc.http_status, exc.code, exc.details)

    @app.errorhandler(HTTPException)
    def _handle_http(exc: HTTPException):
        status = exc.code or 500
        message = exc.description or exc.name
        if status == 413:
            limit_mb = app.config.get("MAX_CONTENT_LENGTH", 0) / (1024 * 1024)
            message = f"Upload is too large. The server accepts at most {limit_mb:.0f} MB per request (AUDIOFP_MAX_UPLOAD_MB)."
        elif status == 404:
            message = "The requested resource was not found."
        elif status == 405:
            message = "Method not allowed for this endpoint."
        return error_response(message, status, _HTTP_CODES.get(status))

    @app.errorhandler(Exception)
    def _handle_unexpected(exc: Exception):  # pragma: no cover - exercised via tests with a broken storage
        logger.exception("Unhandled error while processing request")
        if app.config.get("PROPAGATE_EXCEPTIONS"):
            raise exc
        return error_response(
            "An unexpected error occurred. The request id below identifies it in the server logs.",
            500,
            "internal_error",
        )
