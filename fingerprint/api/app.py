"""Flask application factory.

    from fingerprint.api import create_app
    app = create_app()                       # settings from env / .env
    app = create_app(profile="production")
    app = create_app(settings=Settings.load(storage_type="memory"))

WSGI servers: ``fingerprint.api.wsgi:app`` (see ``docs/DEPLOYMENT.md``).
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import time
import uuid

from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

from .. import __version__
from ..config import Settings
from ..storage import StorageBackend
from ..utils.logging import configure_logging, request_id_var
from .auth import check_request
from .errors import register_error_handlers
from .runtime import Runtime

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")

_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_SECRET_QUERY_KEYS = ("api_key", "token")


def _loggable_path(req) -> str:
    """Path + query string for the access log, with secrets redacted."""
    if not req.query_string:
        return req.path
    pairs = []
    for key, value in req.args.items(multi=True):
        pairs.append(f"{key}=***" if key in _SECRET_QUERY_KEYS else f"{key}={value}")
    return f"{req.path}?{'&'.join(pairs)}"


def create_app(
    settings: Settings | None = None,
    *,
    profile: str | None = None,
    storage: StorageBackend | None = None,
    configure_logs: bool = True,
) -> Flask:
    """Create the Flask app.

    Args:
        settings:       Pre-built settings (tests). Otherwise loaded from env/.env.
        profile:        Shortcut for ``Settings.load(profile=...)``.
        storage:        Inject a storage backend (tests).
        configure_logs: Set up the ``fingerprint`` logger hierarchy.
    """
    settings = settings or Settings.load(profile=profile)
    if configure_logs:
        configure_logging(
            settings.log_level,
            settings.log_format,
            settings.log_file_resolved or None,
            max_bytes=settings.log_max_mb * 1024 * 1024,
            backup_count=settings.log_backup_count,
        )

    app = Flask("audiofp", static_folder=STATIC_DIR, static_url_path="/static")
    app.config.update(
        MAX_CONTENT_LENGTH=settings.max_content_length,
        JSON_SORT_KEYS=False,
        DEBUG=settings.debug and not settings.is_production,
        PROPAGATE_EXCEPTIONS=False,
        SEND_FILE_MAX_AGE_DEFAULT=0 if settings.debug else 3600,
    )
    app.json.sort_keys = False  # type: ignore[attr-defined]

    if settings.cors_origins:
        CORS(app, origins="*" if "*" in settings.cors_origins else settings.cors_origins, expose_headers=["X-Request-ID"])

    if settings.trust_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    runtime = Runtime(settings, storage=storage)
    app.extensions["audiofp"] = runtime
    atexit.register(runtime.close)

    # ------------------------------------------------------------------ request lifecycle
    @app.before_request
    def _begin_request():
        supplied = request.headers.get("X-Request-ID", "").strip()
        rid = supplied[:64] if supplied and _SAFE_REQUEST_ID.fullmatch(supplied[:64]) else uuid.uuid4().hex[:12]
        g.request_id = rid
        g.request_started = time.perf_counter()
        g._rid_token = request_id_var.set(rid)
        if request.path.startswith("/api/"):
            check_request(request, settings.api_key)

    @app.after_request
    def _end_request(response):
        rid = getattr(g, "request_id", None)
        if rid:
            response.headers["X-Request-ID"] = rid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
            if settings.access_log:
                elapsed = (time.perf_counter() - getattr(g, "request_started", time.perf_counter())) * 1000
                logger.info("%s %s -> %d (%.0f ms)", request.method, _loggable_path(request), response.status_code, elapsed)
        return response

    @app.teardown_request
    def _teardown(_exc):
        token = getattr(g, "_rid_token", None)
        if token is not None:
            try:
                request_id_var.reset(token)
            except ValueError:  # pragma: no cover - different context
                pass

    # ------------------------------------------------------------------ blueprints & pages
    from .routes import api_bp

    app.register_blueprint(api_bp, url_prefix="/api/v1")
    register_error_handlers(app)

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html", max_age=0)

    @app.route("/docs")
    def docs_page():
        return send_from_directory(STATIC_DIR, "docs.html", max_age=0)

    @app.route("/api")
    @app.route("/api/v1")
    def api_root():
        return jsonify({"name": "AudioFP", "version": __version__, "openapi": "/api/v1/openapi.json", "docs": "/docs", "health": "/api/v1/health"})

    logger.info("Flask app created (profile=%s, debug=%s)", settings.profile, app.config["DEBUG"])
    return app
