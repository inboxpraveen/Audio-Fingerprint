"""Health, info, stats, runtime settings and the OpenAPI document."""

from __future__ import annotations

from flask import jsonify, request

from ...utils.exceptions import ValidationError
from ..openapi import build_openapi
from ..validators import check_json_body_size
from . import api_bp, runtime


@api_bp.route("/health", methods=["GET"])
def health():
    """Liveness + readiness probe (no auth required)."""
    rt = runtime()
    payload = rt.health()
    return jsonify(payload), (200 if payload["status"] == "ok" else 503)


@api_bp.route("/info", methods=["GET"])
def info():
    """Server capabilities: formats, limits, feature flags, fingerprint parameters, defaults."""
    return jsonify(runtime().info())


@api_bp.route("/stats", methods=["GET"])
def stats():
    """Library statistics. Cheap, so polling it is fine."""
    rt = runtime()
    data = rt.storage.get_stats()
    data["total_songs"] = data.get("total_tracks", 0)  # deprecated alias
    data["jobs_active"] = rt.jobs.active_count()
    return jsonify(data)


@api_bp.route("/settings", methods=["GET"])
def get_settings():
    """Runtime-adjustable search defaults."""
    rt = runtime()
    return jsonify(rt.get_runtime_settings())


@api_bp.route("/settings", methods=["PUT", "PATCH"])
def update_settings():
    rt = runtime()
    check_json_body_size(request)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body:
        raise ValidationError("Send a JSON object with one or more of: top_k, min_confidence, min_aligned_hashes, min_peak_ratio, mode")
    updated = rt.update_runtime_settings(body)
    return jsonify({"message": "Settings updated", **updated})


@api_bp.route("/openapi.json", methods=["GET"])
def openapi():
    rt = runtime()
    return jsonify(build_openapi(rt.settings))
