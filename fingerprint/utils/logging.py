"""Logging setup: console plus rotating file, text or JSON, with request ids.

Modules get their loggers with ``logging.getLogger(__name__)``, so everything sits
under the ``fingerprint`` namespace and :func:`configure_logging` sets it up once.
The current request id, set by the API layer, is added to every record through a
:class:`contextvars.ContextVar`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
import time
from typing import Any

ROOT_LOGGER_NAME = "fingerprint"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("audiofp_request_id", default="-")
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("audiofp_job_id", default="-")


class ContextFilter(logging.Filter):
    """Attach request/job ids to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.job_id = job_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, easy to ship to Loki, Datadog or CloudWatch."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "job_id"):
            value = getattr(record, key, "-")
            if value and value != "-":
                payload[key] = value
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    default_fmt = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"

    def __init__(self) -> None:
        super().__init__(self.default_fmt, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        extra = getattr(record, "extra_fields", None)
        base = super().format(record)
        if isinstance(extra, dict) and extra:
            base += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        return base


def configure_logging(
    level: str = "INFO",
    fmt: str = "text",
    log_file: str | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 5,
    stream=None,
) -> logging.Logger:
    """(Re)configure the ``fingerprint`` logger hierarchy. Safe to call repeatedly."""
    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover, nothing useful to do if a handler won't close
            pass

    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else TextFormatter()
    context_filter = ContextFilter()

    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    root.addHandler(console)

    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        except OSError as exc:
            root.warning("Cannot write the log file %s (%s); logging to the console only", log_file, exc)
        else:
            file_handler.setFormatter(formatter)
            file_handler.addFilter(context_filter)
            root.addHandler(file_handler)

    # Quieten chatty third-party loggers unless we are debugging.
    noisy_level = logging.DEBUG if root.level <= logging.DEBUG else logging.WARNING
    for name in ("werkzeug", "waitress", "urllib3"):
        logging.getLogger(name).setLevel(noisy_level)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``fingerprint`` namespace."""
    if name.startswith(ROOT_LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def log_extra(**fields: Any) -> dict[str, Any]:
    """Attach structured fields to a log call: ``logger.info("msg", extra=log_extra(a=1))``."""
    return {"extra_fields": fields}
