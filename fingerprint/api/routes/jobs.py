"""Background job endpoints."""

from __future__ import annotations

from flask import jsonify, request

from ...jobs import ALL_STATUSES
from ...utils.exceptions import ValidationError
from ..responses import format_job
from ..validators import parse_bool, parse_int
from . import api_bp, runtime


@api_bp.route("/jobs", methods=["GET"])
def list_jobs():
    """List jobs, newest first. ``?status=active`` = pending+running; comma-separated statuses allowed."""
    rt = runtime()
    status = (request.args.get("status") or "").strip() or None
    if status and status != "active":
        unknown = set(status.split(",")) - set(ALL_STATUSES)
        if unknown:
            raise ValidationError(f"Unknown status: {', '.join(sorted(unknown))}", details={"allowed": list(ALL_STATUSES) + ["active"]})
    job_type = (request.args.get("type") or "").strip() or None
    limit = parse_int(request.args, "limit", 100, minimum=1, maximum=1000)
    include_errors = parse_bool(request.args, "include_errors", False)
    jobs = rt.jobs.list(status=status, job_type=job_type, limit=limit)
    return jsonify({"items": [format_job(j, include_errors=include_errors) for j in jobs], "active": rt.jobs.active_count()})


@api_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    rt = runtime()
    return jsonify(format_job(rt.jobs.get(job_id)))


@api_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
    rt = runtime()
    job = rt.jobs.cancel(job_id)
    return jsonify(format_job(job))


@api_bp.route("/jobs/<job_id>", methods=["DELETE"])
def remove_job(job_id: str):
    rt = runtime()
    rt.jobs.remove(job_id)
    return jsonify({"deleted": True, "job_id": job_id})
