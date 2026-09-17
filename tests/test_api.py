"""REST API end-to-end tests through Flask's test client."""

from __future__ import annotations

import io
import json
import os
import re

import pytest

from fingerprint.api import create_app
from fingerprint.config import Settings

from .conftest import upload, wait_for_job

# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def test_health_info_stats_and_request_ids(client):
    r = client.get("/api/v1/health")
    body = r.get_json()
    assert r.status_code == 200 and body["status"] == "ok" and body["storage"]["ok"] is True
    assert body["fingerprint"]["algorithm_version"] >= 2 and "ffmpeg" in body
    assert re.fullmatch(r"[0-9a-f]{12}", r.headers["X-Request-ID"])
    r = client.get("/api/v1/health", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123" and r.headers["Cache-Control"] == "no-store"
    r = client.get("/api/v1/health", headers={"X-Request-ID": "bad id; with spaces & <tags>"})
    assert re.fullmatch(r"[0-9a-f]{12}", r.headers["X-Request-ID"])  # unsafe ids are replaced

    info = client.get("/api/v1/info").get_json()
    assert info["formats"]["native_audio"] and info["limits"]["max_upload_mb"] > 0
    assert info["features"]["auth_required"] is False and "signature" in info["fingerprint"]
    assert info["defaults"]["top_k"] >= 1

    stats = client.get("/api/v1/stats").get_json()
    assert stats["total_tracks"] == 0 and stats["total_songs"] == 0 and stats["storage_type"] == "sqlite"
    assert client.get("/api/v1").get_json()["openapi"] == "/api/v1/openapi.json"
    assert client.get("/").status_code == 200 and client.get("/docs").status_code == 200


def test_error_envelope_shapes(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404 and r.get_json()["code"] == "not_found" and "request_id" in r.get_json()
    r = client.delete("/api/v1/health")
    assert r.status_code == 405 and r.get_json()["code"] == "method_not_allowed"
    r = client.post("/api/v1/search")
    body = r.get_json()
    assert r.status_code == 400 and body["code"] == "validation_error" and isinstance(body["error"], str)
    r = client.post("/api/v1/search", data={"audio": (io.BytesIO(b"x"), "doc.txt")}, content_type="multipart/form-data")
    assert r.status_code == 415 and r.get_json()["code"] == "unsupported_format"
    r = client.post("/api/v1/search", data={"audio": (io.BytesIO(b"junk"), "My Clip.wav")}, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 422 and body["code"] == "audio_decode_error" and "My Clip.wav" in body["error"]
    assert "Temp" not in body["error"] and "tmp" not in body["error"].lower()
    r = client.post("/api/v1/tracks/index-directory", data="not json", content_type="application/json")
    assert r.status_code == 400
    r = client.post("/api/v1/tracks/index-directory", json={"directory_path": "/nope/never"})
    assert r.status_code == 400 and r.get_json()["details"]["field"] == "directory_path"
    huge = '{"title": "' + "A" * (1200 * 1024) + '"}'
    r = client.patch("/api/v1/tracks/x", data=huge, content_type="application/json")
    assert r.status_code == 413 and r.get_json()["code"] == "payload_too_large"
    r = client.put("/api/v1/settings", data=huge, content_type="application/json")
    assert r.status_code == 413
    # Chunked bodies have no Content-Length, so they are refused rather than read into memory.
    chunked = {"HTTP_TRANSFER_ENCODING": "chunked", "wsgi.input_terminated": True}
    for method, path in (("patch", "/api/v1/tracks/x"), ("put", "/api/v1/settings"), ("post", "/api/v1/tracks/bulk-delete")):
        r = getattr(client, method)(path, data=huge, content_type="application/json", environ_overrides=chunked)
        assert r.status_code == 411 and r.get_json()["code"] == "length_required", (method, path)
    r = client.patch("/api/v1/tracks/x")  # no body at all is still a plain validation error
    assert r.status_code == 400


def test_upload_size_limit(tmp_path):
    settings = Settings.load(dotenv=False, env={}, profile="testing", data_dir=str(tmp_path), max_upload_mb=1, log_level="WARNING")
    app = create_app(settings=settings, configure_logs=False)
    client = app.test_client()
    big = io.BytesIO(b"\0" * (2 * 1024 * 1024))
    r = client.post("/api/v1/tracks", data={"audio": (big, "big.wav")}, content_type="multipart/form-data")
    assert r.status_code == 413 and r.get_json()["code"] == "payload_too_large" and "1 MB" in r.get_json()["error"]
    app.extensions["audiofp"].close()


# ---------------------------------------------------------------------------
# tracks lifecycle
# ---------------------------------------------------------------------------


def test_upload_search_edit_delete_flow(client, audio_dir, app):
    job = upload(client, str(audio_dir / "Beta - Second Song.wav"), tags="Music, demo", title="")
    assert job["status"] == "completed" and job["succeeded"] == 1 and job["result"]["status"] == "indexed"
    track_id = job["result"]["track_id"]
    track = job["result"]["track"]
    assert track["title"] == "Second Song" and track["artist"] == "Beta" and track["tags"] == ["music", "demo"]
    assert track["metadata"]["source"] == "upload"

    # duplicate upload is reported, not re-indexed, and its file is discarded
    dup = upload(client, str(audio_dir / "Beta - Second Song.wav"), name="copy.wav")
    assert dup["skipped"] == 1 and dup["result"]["status"] == "duplicate" and dup["result"]["duplicate_of"] == track_id
    uploads = os.listdir(app.extensions["audiofp"].settings.upload_dir_resolved)
    assert len(uploads) == 1

    # failed upload is reported with a helpful error and the file is discarded
    bad = upload(client, str(audio_dir / "broken.wav"))
    assert bad["status"] == "completed" and bad["failed"] == 1 and bad["errors"][0]["error_code"] == "audio_decode_error"
    assert len(os.listdir(app.extensions["audiofp"].settings.upload_dir_resolved)) == 1

    # list / get / search
    page = client.get("/api/v1/tracks?q=second&sort=title&order=asc").get_json()
    assert page["total"] == 1 and page["items"][0]["track_id"] == track_id and page["songs"] == page["items"]
    assert client.get("/api/v1/tracks?tag=demo").get_json()["total"] == 1
    assert client.get("/api/v1/tracks?source_type=video").get_json()["total"] == 0
    assert client.get("/api/v1/tracks?sort=evil").status_code == 400
    assert client.get("/api/v1/tracks?per_page=0").status_code == 400
    detail = client.get(f"/api/v1/tracks/{track_id}").get_json()
    assert detail["file_exists"] is True and detail["filepath"].endswith(".wav")
    assert client.get(f"/api/v1/songs/{track_id}").status_code == 200  # alias
    assert client.get("/api/v1/tracks/missing").status_code == 404

    with open(audio_dir / "clip_second_song_at_12s.wav", "rb") as fh:
        r = client.post("/api/v1/search", data={"audio": (fh, "clip.wav"), "top_k": "3"}, content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200 and body["found"] is True and body["mode"] == "identify"
    match = body["matches"][0]
    assert match["track_id"] == track_id and match["quality"] in ("strong", "likely")
    assert match["track_offset_sec"] == pytest.approx(12.0, abs=0.15) and match["match_offset_sec"] == match["track_offset_sec"]
    assert match["query_offset_sec"] == 0 and match["occurrences"] and body["query"]["num_hashes"] > 0
    assert body["thresholds"]["top_k"] == 3 and body["processing_time_ms"] > 0

    with open(audio_dir / "clip_unrelated.wav", "rb") as fh:
        body = client.post("/api/v1/search", data={"audio": (fh, "clip.wav")}, content_type="multipart/form-data").get_json()
    assert body["found"] is False and body["matches"] == []

    with open(audio_dir / "clip_second_song_at_12s.wav", "rb") as fh:
        r = client.post("/api/v1/search", data={"audio": (fh, "clip.wav"), "mode": "nope"}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["details"]["field"] == "mode"
    with open(audio_dir / "clip_second_song_at_12s.wav", "rb") as fh:
        r = client.post("/api/v1/search", data={"audio": (fh, "clip.wav"), "min_confidence": "2"}, content_type="multipart/form-data")
    assert r.status_code == 400

    # edit
    r = client.patch(f"/api/v1/tracks/{track_id}", json={"title": "Renamed", "tags": ["QA"], "metadata": {"agent": "A1"}})
    assert r.status_code == 200 and r.get_json()["title"] == "Renamed" and r.get_json()["tags"] == ["qa"]
    assert client.patch(f"/api/v1/tracks/{track_id}", json={"filepath": "x"}).status_code == 400
    assert client.patch(f"/api/v1/tracks/{track_id}", json={}).status_code == 400
    assert client.get("/api/v1/tags").get_json()["tags"] == [{"tag": "qa", "count": 1}]

    # streaming with Range
    r = client.get(f"/api/v1/tracks/{track_id}/audio", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.headers["Content-Range"].startswith("bytes 0-99/") and len(r.data) == 100
    assert r.headers["Content-Type"].startswith("audio/")
    r.close()
    r = client.get(f"/api/v1/songs/{track_id}/play")
    assert r.status_code == 200
    r.close()

    # delete (and remove the uploaded file)
    r = client.delete(f"/api/v1/tracks/{track_id}?delete_file=true")
    assert r.status_code == 200 and r.get_json()["file_removed"] is True
    assert client.delete(f"/api/v1/tracks/{track_id}").status_code == 404
    assert client.get(f"/api/v1/tracks/{track_id}/audio").status_code == 404
    assert client.get("/api/v1/stats").get_json()["total_tracks"] == 0


def test_directory_indexing_jobs_and_bulk_delete(client, audio_dir):
    r = client.post("/api/v1/tracks/index-directory", json={"directory_path": str(audio_dir), "tags": ["batch"]})
    assert r.status_code == 202
    body = r.get_json()
    assert body["total_files"] == 7 and body["job"]["type"] == "directory"
    job = wait_for_job(client, body["job_id"])
    assert job["status"] == "completed" and job["succeeded"] == 6 and job["failed"] == 1 and job["percent"] == 100.0
    assert job["errors"][0]["error_code"] == "audio_decode_error" and job["result"]["directory"] == str(audio_dir)
    assert job["elapsed_sec"] >= 0 and job["error_count"] == 1

    listing = client.get("/api/v1/jobs?status=completed&include_errors=false").get_json()
    assert listing["items"][0]["job_id"] == job["job_id"] and "errors" not in listing["items"][0]
    assert client.get("/api/v1/jobs?status=bogus").status_code == 400
    assert client.get("/api/v1/jobs/nope").status_code == 404
    assert client.post(f"/api/v1/jobs/{job['job_id']}/cancel").status_code == 400  # already finished

    again = wait_for_job(client, client.post("/api/v1/index", json={"directory_path": str(audio_dir)}).get_json()["job_id"])
    assert again["skipped"] == 6 and again["succeeded"] == 0

    page = client.get("/api/v1/tracks?per_page=2&page=2&tag=batch").get_json()
    assert page["total"] == 6 and page["pages"] == 3 and len(page["items"]) == 2
    ids = [t["track_id"] for t in client.get("/api/v1/tracks?per_page=100").get_json()["items"]]
    r = client.post("/api/v1/tracks/bulk-delete", json={"track_ids": ids[:3] + ["missing"]})
    assert r.get_json()["deleted"] == 3
    assert client.post("/api/v1/tracks/bulk-delete", json={"track_ids": []}).status_code == 400
    assert client.delete(f"/api/v1/jobs/{job['job_id']}").status_code == 200
    assert client.get(f"/api/v1/jobs/{job['job_id']}").status_code == 404


def test_occurrences_mode_via_api(client, audio_dir, tmp_path):
    import numpy as np
    import soundfile as sf

    from .conftest import SRC_SR, synth_signal

    src, sr = sf.read(str(audio_dir / "Gamma Call.wav"), dtype="float32")
    pattern = src[int(5 * sr) : int(8 * sr)]
    sf.write(str(tmp_path / "jingle.wav"), pattern, sr)
    filler = synth_signal(seed=31, seconds=30)
    recording = np.concatenate([filler[: 6 * SRC_SR], pattern, filler[6 * SRC_SR : 18 * SRC_SR], pattern, filler[18 * SRC_SR :]])
    sf.write(str(tmp_path / "recording.wav"), recording, SRC_SR)

    upload(client, str(tmp_path / "jingle.wav"), title="Compliance jingle")
    with open(tmp_path / "recording.wav", "rb") as fh:
        body = client.post("/api/v1/search", data={"audio": (fh, "recording.wav"), "mode": "occurrences"}, content_type="multipart/form-data").get_json()
    assert body["mode"] == "occurrences" and body["found"]
    match = next(m for m in body["matches"] if m["title"] == "Compliance jingle")
    starts = sorted(o["query_offset_sec"] for o in match["occurrences"])
    assert len(starts) == 2 and starts[0] == pytest.approx(6.0, abs=0.15) and starts[1] == pytest.approx(21.0, abs=0.15)
    assert all(o["offset_sec"] < 0 and o["query_start_sec"] <= o["query_end_sec"] for o in match["occurrences"])


def test_runtime_settings_persist(client, app):
    assert client.get("/api/v1/settings").get_json()["mode"] == "identify"
    r = client.put("/api/v1/settings", json={"top_k": 7, "min_confidence": 0.1, "mode": "occurrences"})
    assert r.status_code == 200 and r.get_json()["top_k"] == 7
    assert client.put("/api/v1/settings", json={"foo": 1}).status_code == 400
    assert client.put("/api/v1/settings", json={"mode": "bad"}).status_code == 400
    assert client.put("/api/v1/settings", json={"top_k": "x"}).status_code == 400
    assert client.put("/api/v1/settings", data="[]", content_type="application/json").status_code == 400
    settings_path = app.extensions["audiofp"].settings.runtime_settings_path
    with open(settings_path) as fh:
        assert json.load(fh)["top_k"] == 7
    # persisted defaults are picked up by a fresh app on the same data dir
    other = create_app(settings=app.extensions["audiofp"].settings, configure_logs=False)
    assert other.test_client().get("/api/v1/settings").get_json()["mode"] == "occurrences"
    other.extensions["audiofp"].close()


def test_runtime_close_waits_for_jobs_and_flushes(sqlite_settings):
    """Shutdown cancels running jobs, lets them persist their state and flushes buffered rows; it is idempotent."""
    import threading

    app = create_app(settings=sqlite_settings, configure_logs=False)
    rt = app.extensions["audiofp"]
    started = threading.Event()

    def slow(job):
        started.set()
        while True:
            job.check_cancelled()
            threading.Event().wait(0.01)

    job = rt.jobs.submit("directory", "slow", slow)
    assert started.wait(5)
    rt.close()
    assert job.is_terminal and job.status == "cancelled"
    assert rt.storage.get_stats().get("pending_rows", 0) == 0
    rt.close()  # second call is a no-op


def test_api_key_auth(tmp_path, audio_dir):
    settings = Settings.load(dotenv=False, env={}, profile="testing", data_dir=str(tmp_path), api_key="s3cret", log_level="WARNING")
    app = create_app(settings=settings, configure_logs=False)
    client = app.test_client()
    assert client.get("/api/v1/health").status_code == 200  # public
    r = client.get("/api/v1/stats")
    assert r.status_code == 401 and r.get_json()["code"] == "unauthorized"
    assert client.get("/api/v1/stats", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/api/v1/stats", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/v1/stats", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.get("/").status_code == 200  # the UI shell itself is public
    assert client.get("/api/v1/openapi.json").status_code == 200  # spec is public
    # <audio src> cannot send headers: a short-lived, track-scoped token is used instead of the key.
    assert client.get("/api/v1/stats?api_key=s3cret").status_code == 401  # the key is never accepted in a URL
    job = upload(client, str(audio_dir / "Gamma Call.wav"), headers={"X-API-Key": "s3cret"})
    track_id = job["result"]["track_id"]
    assert client.get(f"/api/v1/tracks/{track_id}/stream-token").status_code == 401
    tok = client.get(f"/api/v1/tracks/{track_id}/stream-token", headers={"X-API-Key": "s3cret"}).get_json()
    assert tok["auth_required"] is True and tok["token"] and tok["url"].endswith(f"?token={tok['token']}")
    r = client.get(tok["url"], headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    r.close()
    assert client.get(f"/api/v1/tracks/{track_id}/audio?token=garbage").status_code == 401
    other = upload(client, str(audio_dir / "Alpha Band - First Song.wav"), headers={"X-API-Key": "s3cret"})["result"]["track_id"]
    assert client.get(f"/api/v1/tracks/{other}/audio?token={tok['token']}").status_code == 401  # scoped to one track
    from fingerprint.api.auth import make_stream_token, verify_stream_token

    expired, _ = make_stream_token("s3cret", track_id, ttl=-1)
    assert client.get(f"/api/v1/tracks/{track_id}/audio?token={expired}").status_code == 401
    assert not verify_stream_token("other-key", track_id, tok["token"])
    # the access log must never contain secrets
    from fingerprint.api.app import _loggable_path

    with app.test_request_context("/api/v1/tracks/x/audio?token=abc&download=true"):
        from flask import request as req

        assert _loggable_path(req) == "/api/v1/tracks/x/audio?token=***&download=true"
    assert client.get("/api/v1/info", headers={"X-API-Key": "s3cret"}).get_json()["features"]["auth_required"] is True
    app.extensions["audiofp"].close()


def test_production_directory_indexing_requires_roots(tmp_path, audio_dir):
    base = {"dotenv": False, "env": {}, "profile": "production", "storage_type": "memory", "persist_jobs": False, "log_level": "WARNING", "log_file": ""}
    app = create_app(settings=Settings.load(data_dir=str(tmp_path / "a"), **base), configure_logs=False)
    r = app.test_client().post("/api/v1/tracks/index-directory", json={"directory_path": str(audio_dir)})
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"
    app.extensions["audiofp"].close()

    app = create_app(settings=Settings.load(data_dir=str(tmp_path / "b"), index_roots=[str(tmp_path / "elsewhere")], **base), configure_logs=False)
    r = app.test_client().post("/api/v1/tracks/index-directory", json={"directory_path": str(audio_dir)})
    assert r.status_code == 403 and "allowed_roots" in r.get_json()["details"]
    # a non-existent path outside the roots is also 403: existence is never revealed before authorisation
    r = app.test_client().post("/api/v1/tracks/index-directory", json={"directory_path": str(tmp_path / "does-not-exist")})
    assert r.status_code == 403
    app.extensions["audiofp"].close()

    app = create_app(settings=Settings.load(data_dir=str(tmp_path / "c"), index_roots=[str(audio_dir)], **base), configure_logs=False)
    r = app.test_client().post("/api/v1/tracks/index-directory", json={"directory_path": str(audio_dir)})
    assert r.status_code == 202
    app.extensions["audiofp"].close()

    app = create_app(settings=Settings.load(data_dir=str(tmp_path / "d"), allow_directory_indexing=False, **base), configure_logs=False)
    r = app.test_client().post("/api/v1/tracks/index-directory", json={"directory_path": str(audio_dir)})
    assert r.status_code == 403 and "disabled" in r.get_json()["error"]
    app.extensions["audiofp"].close()


# ---------------------------------------------------------------------------
# OpenAPI stays in sync with the registered routes
# ---------------------------------------------------------------------------


def test_openapi_covers_every_route(app, client):
    spec = client.get("/api/v1/openapi.json").get_json()
    assert spec["openapi"].startswith("3.0") and spec["info"]["version"]
    documented = set()
    for path, methods in spec["paths"].items():
        for method in methods:
            documented.add((method.upper(), path))
    registered = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/v1/"):
            continue
        path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", lambda m: "{" + m.group(1) + "}", rule.rule[len("/api/v1") :])
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            registered.add((method, path))
    missing = registered - documented
    extra = documented - registered
    assert not missing, f"routes missing from OpenAPI: {sorted(missing)}"
    assert not extra, f"OpenAPI documents unknown routes: {sorted(extra)}"
