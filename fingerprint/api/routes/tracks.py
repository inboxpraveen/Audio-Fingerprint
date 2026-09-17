"""Track endpoints: upload, index a folder, list/get/update/delete, stream audio.

``/songs*`` paths from AudioFP 1.x are kept as deprecated aliases.
"""

from __future__ import annotations

import json
import mimetypes
import os
import uuid

from flask import jsonify, request, send_file

from ...storage import SORTABLE_FIELDS, normalize_tags, validate_track_changes
from ...utils.exceptions import NotFoundError, ValidationError
from ...utils.files import ensure_dir, safe_filename
from ..auth import STREAM_TOKEN_TTL, make_stream_token
from ..responses import format_job, format_page, format_track
from ..validators import check_json_body_size, clean_directory_path, parse_bool, parse_choice, parse_int, parse_pagination, require_upload
from . import api_bp, runtime


def _json_body() -> dict:
    check_json_body_size(request)
    data = request.get_json(silent=True)
    if data is None:
        if request.data:
            raise ValidationError("Request body must be valid JSON")
        return {}
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


def _parse_metadata(raw) -> dict | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("'metadata' must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValidationError("'metadata' must be a JSON object")
    return value


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@api_bp.route("/tracks", methods=["POST"])
@api_bp.route("/upload", methods=["POST"])  # deprecated alias
def upload_track():
    """Upload a file and index it in the background (202 + job)."""
    rt = runtime()
    upload = require_upload(request.files)
    form = request.form
    # Validate the metadata fields first so a bad request never leaves an orphaned file on disk.
    fields = validate_track_changes(
        {"title": form.get("title") or "", "artist": form.get("artist") or "", "tags": form.get("tags"), "metadata": _parse_metadata(form.get("metadata"))}
    )
    original_name = os.path.basename(upload.filename.replace("\\", "/"))
    stored_name = f"{uuid.uuid4().hex[:8]}_{safe_filename(original_name)}"
    upload_dir = ensure_dir(rt.settings.upload_dir_resolved)
    save_path = os.path.abspath(os.path.join(upload_dir, stored_name))
    upload.save(save_path)

    job = rt.start_upload_job(
        save_path,
        original_name,
        title=fields["title"],
        artist=fields["artist"],
        tags=fields["tags"],
        metadata=fields["metadata"],
    )
    return jsonify({"job_id": job.id, "job": format_job(job), "filename": original_name, "message": "Indexing started"}), 202


@api_bp.route("/tracks/index-directory", methods=["POST"])
@api_bp.route("/index", methods=["POST"])  # deprecated alias
def index_directory():
    """Index every supported file below a server-side directory (202 + job)."""
    rt = runtime()
    body = _json_body()
    directory = clean_directory_path(body.get("directory_path"))
    rt.check_directory_allowed(directory)  # authorisation first: never reveal whether a path exists
    if not os.path.isdir(directory):
        raise ValidationError(f"'{body.get('directory_path')}' is not a directory on the server", details={"field": "directory_path"})
    recursive = parse_bool(body, "recursive", True)
    tags = normalize_tags(body.get("tags"))
    job, total = rt.start_directory_job(directory, recursive=recursive, tags=tags)
    return jsonify(
        {
            "job_id": job.id,
            "job": format_job(job),
            "total_files": total,
            "message": f"Indexing started for {total} file{'s' if total != 1 else ''}",
        }
    ), 202


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@api_bp.route("/tracks", methods=["GET"])
@api_bp.route("/songs", methods=["GET"])  # deprecated alias
def list_tracks():
    """List tracks with search, sorting and pagination."""
    rt = runtime()
    args = request.args
    page, per_page = parse_pagination(args)
    sort = parse_choice(args, "sort", SORTABLE_FIELDS, "indexed_at")
    order = parse_choice(args, "order", ("asc", "desc"), "desc")
    source_type = parse_choice(args, "source_type", ("audio", "video"))
    query = (args.get("q") or "").strip() or None
    tag = (args.get("tag") or "").strip() or None
    items, total = rt.storage.list_tracks(query=query, sort=sort, order=order, offset=(page - 1) * per_page, limit=per_page, source_type=source_type, tag=tag)
    payload = format_page([format_track(t) for t in items], total, page, per_page)
    payload["songs"] = payload["items"]  # deprecated alias
    payload["count"] = total
    return jsonify(payload)


@api_bp.route("/tracks/<track_id>", methods=["GET"])
@api_bp.route("/songs/<track_id>", methods=["GET"])  # deprecated alias
def get_track(track_id: str):
    rt = runtime()
    record = rt.storage.require_track(track_id)
    data = format_track(record, include_path=True)
    data["file_exists"] = bool(record.filepath) and os.path.isfile(record.filepath)
    return jsonify(data)


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


@api_bp.route("/tracks/<track_id>", methods=["PATCH", "PUT"])
def update_track(track_id: str):
    rt = runtime()
    body = _json_body()
    if not body:
        raise ValidationError("Nothing to update. Send one or more of: title, artist, tags, metadata.")
    record = rt.storage.update_track(track_id, body)
    return jsonify(format_track(record))


@api_bp.route("/tracks/<track_id>", methods=["DELETE"])
@api_bp.route("/songs/<track_id>", methods=["DELETE"])  # deprecated alias
def delete_track(track_id: str):
    rt = runtime()
    record = rt.storage.require_track(track_id)
    rt.storage.delete_track(track_id)
    removed_file = _remove_upload_file(rt, record.filepath, parse_bool(request.args, "delete_file", False))
    return jsonify({"deleted": True, "track_id": track_id, "file_removed": removed_file, "message": "Track deleted"})


@api_bp.route("/tracks/bulk-delete", methods=["POST"])
def bulk_delete():
    rt = runtime()
    body = _json_body()
    ids = body.get("track_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise ValidationError("'track_ids' must be a non-empty list of track ids")
    if len(ids) > 1000:
        raise ValidationError("At most 1000 tracks can be deleted per request")
    delete_files = parse_bool(body, "delete_files", False)
    removed_files = 0
    if delete_files:
        for tid in ids:
            record = rt.storage.get_track(tid)
            if record and _remove_upload_file(rt, record.filepath, True):
                removed_files += 1
    deleted = rt.storage.delete_tracks(ids)
    return jsonify({"deleted": deleted, "requested": len(ids), "files_removed": removed_files})


def _remove_upload_file(rt, filepath: str, wanted: bool) -> bool:
    """Delete the file behind a track, but only if it lives in our upload folder."""
    if not wanted or not filepath:
        return False
    from ...utils.files import is_within

    if not is_within(filepath, rt.settings.upload_dir_resolved):
        return False
    try:
        os.remove(filepath)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Audio streaming
# ---------------------------------------------------------------------------


@api_bp.route("/tracks/<track_id>/audio", methods=["GET"])
@api_bp.route("/tracks/<track_id>/play", methods=["GET"])
@api_bp.route("/songs/<track_id>/play", methods=["GET"])  # deprecated alias
def stream_track(track_id: str):
    """Stream the original file (supports HTTP Range requests so players can seek)."""
    rt = runtime()
    record = rt.storage.require_track(track_id)
    path = record.filepath
    if not path or not os.path.isfile(path):
        raise NotFoundError(
            "The original file for this track is no longer on disk. The fingerprint still works for searching.",
            code="file_missing",
            details={"track_id": track_id},
        )
    mime, _ = mimetypes.guess_type(record.filename or path)
    return send_file(
        path,
        mimetype=mime or "application/octet-stream",
        as_attachment=parse_bool(request.args, "download", False),
        download_name=record.filename or os.path.basename(path),
        conditional=True,  # enables Range / 206 responses
        max_age=0,
    )


@api_bp.route("/tracks/<track_id>/stream-token", methods=["GET"])
def stream_token(track_id: str):
    """Mint a short-lived, track-scoped token for ``<audio src>`` when the server requires an API key."""
    rt = runtime()
    rt.storage.require_track(track_id)
    if not rt.settings.api_key:
        return jsonify({"token": None, "expires_at": None, "url": f"/api/v1/tracks/{track_id}/audio", "auth_required": False})
    token, expires = make_stream_token(rt.settings.api_key, track_id, STREAM_TOKEN_TTL)
    return jsonify({"token": token, "expires_at": expires, "url": f"/api/v1/tracks/{track_id}/audio?token={token}", "auth_required": True})


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@api_bp.route("/tags", methods=["GET"])
def list_tags():
    """Distinct tags across the library (bounded scan of the most recent tracks)."""
    rt = runtime()
    limit = parse_int(request.args, "scan_limit", 2000, minimum=1, maximum=20000) or 2000
    items, _ = rt.storage.list_tracks(limit=limit)
    counts: dict[str, int] = {}
    for item in items:
        for tag in item.tags:
            counts[tag] = counts.get(tag, 0) + 1
    tags = sorted(({"tag": t, "count": c} for t, c in counts.items()), key=lambda x: (-x["count"], x["tag"]))
    return jsonify({"tags": tags})
