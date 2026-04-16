"""API endpoint definitions."""

import os
import time
import uuid
import threading
import tempfile

from flask import Blueprint, request, current_app, jsonify
from werkzeug.utils import secure_filename

from ..core import load_audio, preprocess_audio, Fingerprinter, generate_hashes, match_fingerprint
from ..training.indexer import Indexer
from .validators import validate_audio_file
from .responses import format_search_response, format_error_response

api_bp = Blueprint("api", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_indexer(app) -> Indexer:
    """Create an Indexer configured from the Flask app config."""
    return Indexer(
        storage=app.storage,
        sr=app.config.get("SAMPLE_RATE", 11025),
        n_fft=app.config.get("N_FFT", 2048),
        hop_length=app.config.get("HOP_LENGTH", 512),
        peak_neighborhood_size=app.config.get("PEAK_NEIGHBORHOOD_SIZE", 20),
        min_amplitude=app.config.get("MIN_AMPLITUDE", 10),
        fan_value=app.config.get("FAN_VALUE", 10),
    )


def _run_index_job(app, job_id: str, indexer: Indexer, target, is_directory: bool):
    """
    Background thread: index a file or directory and update app.jobs.

    No Flask application context is required here because we hold a direct
    reference to the app object (not the request-local proxy).
    """
    jobs = app.jobs
    jobs[job_id]["status"] = "running"
    jobs[job_id]["started_at"] = time.time()

    def progress_cb(current: int, total: int, filename: str):
        jobs[job_id]["completed"] = current
        jobs[job_id]["total"] = total
        jobs[job_id]["current_file"] = filename

    try:
        if is_directory:
            result = indexer.index_directory(target, progress_callback=progress_cb)
        else:
            song_id, success, error_msg = indexer.index_song(target)
            progress_cb(1, 1, os.path.basename(target))
            result = {
                "total": 1,
                "success": 1 if success else 0,
                "failed": 0 if success else 1,
                "errors": [] if success else [{"file": target, "error": error_msg}],
            }

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["result"] = result
        jobs[job_id]["completed_at"] = time.time()

    except Exception as exc:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(exc)
        jobs[job_id]["completed_at"] = time.time()
        app.logger.error(f"Job {job_id} failed: {exc}")


def _start_job(app, job_meta: dict, indexer: Indexer, target, is_directory: bool) -> str:
    """Register a job, start its background thread, return job_id."""
    job_id = str(uuid.uuid4())
    app.jobs[job_id] = {**job_meta, "status": "pending", "created_at": time.time()}
    thread = threading.Thread(
        target=_run_index_job,
        args=(app, job_id, indexer, target, is_directory),
        daemon=True,
    )
    thread.start()
    return job_id


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@api_bp.route("/search", methods=["POST"])
def search():
    """Search the knowledge base with an audio clip.

    Request: multipart/form-data — field 'audio'
    Response: JSON with ranked matches, confidence scores, and match timestamps
    """
    start_time = time.time()

    if "audio" not in request.files:
        return format_error_response("No audio file provided", 400)

    audio_file = request.files["audio"]

    is_valid, err = validate_audio_file(
        audio_file,
        current_app.config.get("ALLOWED_EXTENSIONS"),
        current_app.config.get("MAX_CONTENT_LENGTH"),
    )
    if not is_valid:
        return format_error_response(err, 400)

    ext = os.path.splitext(audio_file.filename)[1] or ".tmp"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            audio_file.save(tmp.name)
            tmp_path = tmp.name

        sr_cfg = current_app.config.get("SAMPLE_RATE", 11025)
        hop = current_app.config.get("HOP_LENGTH", 512)

        audio, sr = load_audio(tmp_path, sr=sr_cfg)
        audio = preprocess_audio(audio)

        fingerprinter = Fingerprinter(
            sr=sr,
            n_fft=current_app.config.get("N_FFT", 2048),
            hop_length=hop,
            peak_neighborhood_size=current_app.config.get("PEAK_NEIGHBORHOOD_SIZE", 20),
            min_amplitude=current_app.config.get("MIN_AMPLITUDE", 10),
        )
        peaks = fingerprinter.generate_fingerprint(audio)
        query_hashes = generate_hashes(
            peaks,
            song_id=None,
            fan_value=current_app.config.get("FAN_VALUE", 10),
        )

        matches = match_fingerprint(
            query_hashes,
            current_app.storage,
            top_k=5,
            hop_length=hop,
            sr=sr,
        )

        processing_ms = (time.time() - start_time) * 1000
        return format_search_response(matches, processing_ms, len(audio) / sr)

    except Exception as exc:
        current_app.logger.error(f"Search error: {exc}")
        return format_error_response(f"Search failed: {exc}", 500)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Upload a single file
# ---------------------------------------------------------------------------

@api_bp.route("/upload", methods=["POST"])
def upload_song():
    """Upload an audio file and add it to the knowledge base.

    Request: multipart/form-data — field 'audio'
    Response: 202 with job_id; poll /jobs/<job_id> for progress.
    """
    if "audio" not in request.files:
        return format_error_response("No audio file provided", 400)

    audio_file = request.files["audio"]

    is_valid, err = validate_audio_file(
        audio_file,
        current_app.config.get("ALLOWED_EXTENSIONS"),
        current_app.config.get("MAX_CONTENT_LENGTH"),
    )
    if not is_valid:
        return format_error_response(err, 400)

    upload_folder = current_app.config.get("UPLOAD_FOLDER", "data/uploads")
    os.makedirs(upload_folder, exist_ok=True)

    original_name = secure_filename(audio_file.filename)
    unique_name = f"{uuid.uuid4().hex[:8]}_{original_name}"
    save_path = os.path.join(upload_folder, unique_name)
    audio_file.save(save_path)

    app = current_app._get_current_object()
    indexer = _build_indexer(app)

    job_id = _start_job(
        app,
        {"type": "upload", "filename": original_name, "total": 1, "completed": 0, "current_file": original_name},
        indexer,
        save_path,
        is_directory=False,
    )

    return jsonify({"job_id": job_id, "message": "Indexing started", "filename": original_name}), 202


# ---------------------------------------------------------------------------
# Index a directory
# ---------------------------------------------------------------------------

@api_bp.route("/index", methods=["POST"])
def index_directory():
    """Start background indexing of all audio files in a directory.

    Body: {"directory_path": "/absolute/path/to/folder"}
    Response: 202 with job_id; poll /jobs/<job_id> for progress.
    """
    data = request.get_json(silent=True) or {}
    directory_path = data.get("directory_path", "").strip()

    if not directory_path:
        return format_error_response("directory_path is required", 400)

    if not os.path.isdir(directory_path):
        return format_error_response("Path does not exist or is not a directory", 400)

    from ..training.dataset_loader import DatasetLoader
    audio_files = DatasetLoader().find_audio_files(directory_path)

    if not audio_files:
        return format_error_response("No supported audio files found in that directory", 400)

    app = current_app._get_current_object()
    indexer = _build_indexer(app)

    job_id = _start_job(
        app,
        {
            "type": "directory",
            "directory": directory_path,
            "total": len(audio_files),
            "completed": 0,
            "current_file": None,
        },
        indexer,
        directory_path,
        is_directory=True,
    )

    return jsonify({
        "job_id": job_id,
        "message": f"Indexing started for {len(audio_files)} files",
        "total_files": len(audio_files),
    }), 202


# ---------------------------------------------------------------------------
# Job status
# ---------------------------------------------------------------------------

@api_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    """Get status and progress of an indexing job."""
    job = current_app.jobs.get(job_id)
    if job is None:
        return format_error_response("Job not found", 404)
    return jsonify(job)


@api_bp.route("/jobs", methods=["GET"])
def list_jobs():
    """List all known jobs (for debugging / admin)."""
    return jsonify({"jobs": current_app.jobs})


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------

@api_bp.route("/songs", methods=["GET"])
def get_songs():
    """Return all indexed songs."""
    try:
        songs = current_app.storage.get_all_songs()
        return jsonify({"songs": songs, "count": len(songs)})
    except Exception as exc:
        current_app.logger.error(f"get_songs error: {exc}")
        return format_error_response(str(exc), 500)


@api_bp.route("/songs/<song_id>", methods=["GET"])
def get_song(song_id: str):
    """Return metadata for a single song."""
    try:
        meta = current_app.storage.get_song_metadata(song_id)
        if meta is None:
            return format_error_response("Song not found", 404)
        return jsonify(meta)
    except Exception as exc:
        current_app.logger.error(f"get_song error: {exc}")
        return format_error_response(str(exc), 500)


@api_bp.route("/songs/<song_id>", methods=["DELETE"])
def delete_song(song_id: str):
    """Remove a song and all its fingerprints from the knowledge base."""
    try:
        if current_app.storage.get_song_metadata(song_id) is None:
            return format_error_response("Song not found", 404)
        current_app.storage.delete_song(song_id)
        return jsonify({"message": "Song deleted", "song_id": song_id})
    except Exception as exc:
        current_app.logger.error(f"delete_song error: {exc}")
        return format_error_response(str(exc), 500)


# ---------------------------------------------------------------------------
# Stats & health
# ---------------------------------------------------------------------------

@api_bp.route("/stats", methods=["GET"])
def get_stats():
    """Return database statistics."""
    try:
        return jsonify(current_app.storage.get_stats())
    except Exception as exc:
        current_app.logger.error(f"stats error: {exc}")
        return format_error_response(str(exc), 500)


@api_bp.route("/health", methods=["GET"])
def health_check():
    """Liveness probe."""
    return jsonify({"status": "healthy", "timestamp": time.time()})
