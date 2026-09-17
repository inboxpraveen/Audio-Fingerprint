"""REST API blueprint (``/api/v1``).

Each module registers its endpoints on :data:`api_bp`. Route handlers are
thin: they parse the request, call :class:`~fingerprint.api.runtime.Runtime`
and format the result.
"""

from __future__ import annotations

from flask import Blueprint, current_app

from ..runtime import Runtime

api_bp = Blueprint("api", __name__)


def runtime() -> Runtime:
    return current_app.extensions["audiofp"]


from . import jobs, search, system, tracks  # noqa: E402,F401  (register routes)
