"""Indexer (scanning, de-duplication, error isolation, cancellation) and JobManager."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time

import pytest

from fingerprint.indexing import Indexer, find_media_files, metadata_from_filename
from fingerprint.indexing.progress import ProgressTracker, format_eta, render_bar
from fingerprint.jobs import CANCELLED, COMPLETED, FAILED, INTERRUPTED, RUNNING, JobCancelled, JobManager
from fingerprint.storage import MemoryStore
from fingerprint.utils.exceptions import JobError, NotFoundError

# ---------------------------------------------------------------------------
# scanner
# ---------------------------------------------------------------------------


def test_scanner_and_filename_metadata(audio_dir, tmp_path):
    files = find_media_files(str(audio_dir))
    assert all(f.endswith(".wav") for f in files) and not any("notes.txt" in f for f in files)
    assert files == sorted(files)
    nested = tmp_path / "root"
    (nested / "sub" / ".hidden").mkdir(parents=True)
    (nested / "node_modules").mkdir()
    shutil.copy(files[0], nested / "a.mp3")
    shutil.copy(files[0], nested / "sub" / "b.flac")
    shutil.copy(files[0], nested / "sub" / ".hidden" / "c.wav")
    shutil.copy(files[0], nested / "node_modules" / "d.wav")
    (nested / "sub" / "e.txt").write_text("x")
    found = find_media_files(str(nested))
    assert [os.path.basename(f) for f in found] == ["a.mp3", "b.flac"]
    assert [os.path.basename(f) for f in find_media_files(str(nested), recursive=False)] == ["a.mp3"]
    assert find_media_files(str(nested), limit=1) == found[:1]
    assert find_media_files(str(tmp_path / "missing")) == []

    assert metadata_from_filename("/x/Artist Name - Song Title.mp3") == {"artist": "Artist Name", "title": "Song Title"}
    assert metadata_from_filename("call-2024-01-01.wav") == {"artist": "", "title": "call-2024-01-01"}
    assert metadata_from_filename(" - only dash.wav") == {"artist": "", "title": "- only dash"}


def test_progress_tracker():
    tracker = ProgressTracker(total=4)
    tracker.start_item("a")
    tracker.finish_item("indexed", "a")
    tracker.finish_item("duplicate", "b")
    tracker.finish_item("failed", "c")
    snap = tracker.snapshot()
    assert (snap.completed, snap.succeeded, snap.skipped, snap.failed) == (3, 1, 1, 1)
    assert snap.percent == 75.0 and snap.eta_sec is not None
    assert "3/4" in render_bar(snap) and format_eta(None) == "--:--" and format_eta(3661) == "1h 01m"
    assert snap.to_dict()["current_item"] == "c"


# ---------------------------------------------------------------------------
# indexer
# ---------------------------------------------------------------------------


def test_index_directory_isolates_bad_files_and_dedupes(settings, audio_dir):
    store = MemoryStore()
    indexer = Indexer(settings, store)
    events = []
    summary = indexer.index_directory(str(audio_dir), progress=lambda path, outcome, tracker: events.append((os.path.basename(path), outcome.status)))
    indexer.close()
    assert summary.total == 7  # 3 tracks + 3 clips + broken.wav (notes.txt is skipped by the scanner)
    assert summary.failed == 1 and any(e["error_code"] == "audio_decode_error" for e in summary.errors)
    assert summary.indexed == summary.total - summary.failed
    assert len(summary.track_ids) == summary.indexed and len(events) == summary.total
    assert store.count_tracks() == summary.indexed
    track = store.list_tracks(query="First Song")[0][0]
    assert track.artist == "Alpha Band" and track.title == "First Song" and track.source_type == "audio"
    assert track.content_hash and track.file_size > 0 and track.duration == pytest.approx(25.0, abs=0.05)
    assert track.metadata["source"] == "directory"

    # second run: everything is a duplicate, nothing re-indexed
    again = Indexer(settings, store).index_directory(str(audio_dir))
    assert again.indexed == 0 and again.duplicates == summary.indexed and again.failed == 1
    assert store.count_tracks() == summary.indexed
    assert again.duplicates_of and all(d["duplicate_of"] for d in again.duplicates_of)


def test_index_file_options_and_dedupe_modes(settings, audio_dir, tmp_path):
    store = MemoryStore()
    indexer = Indexer(settings, store)
    path = str(audio_dir / "Gamma Call.wav")
    out = indexer.index_file(
        path, title="Custom", artist="Someone", tags="QA, Compliance", metadata={"agent": "A7"}, source="upload", original_filename="Original Name.wav"
    )
    assert out.status == "indexed" and out.track.title == "Custom" and out.track.artist == "Someone"
    assert out.track.tags == ["qa", "compliance"] and out.track.metadata == {"source": "upload", "agent": "A7"}
    assert out.track.filename == "Original Name.wav" and out.track.filepath == os.path.abspath(path)
    assert out.to_dict()["track_id"] == out.track.track_id

    copy = tmp_path / "copy.wav"
    shutil.copy(path, copy)
    dup = indexer.index_file(str(copy))
    assert dup.status == "duplicate" and dup.duplicate_of == out.track.track_id

    path_settings = settings.__class__.load(dotenv=False, env={}, profile="testing", dedupe="path", data_dir=settings.data_dir)
    indexer_path = Indexer(path_settings, store, fingerprinter=indexer.fingerprinter)
    assert indexer_path.index_file(str(copy)).status == "indexed"  # different path -> new track
    assert indexer_path.index_file(str(copy)).status == "duplicate"  # same path -> duplicate

    none_settings = settings.__class__.load(dotenv=False, env={}, profile="testing", dedupe="none", data_dir=settings.data_dir)
    assert Indexer(none_settings, store, fingerprinter=indexer.fingerprinter).index_file(str(copy)).status == "indexed"

    assert indexer.index_file(str(tmp_path / "missing.wav")).error_code == "file_not_found"
    assert indexer.index_file(str(audio_dir / "notes.txt")).error_code == "unsupported_format"
    broken = indexer.index_file(str(audio_dir / "broken.wav"))
    assert broken.status == "failed" and broken.error_code == "audio_decode_error" and "broken.wav" in broken.error
    indexer.close()


def test_identical_files_in_one_batch_are_deduplicated(settings, audio_dir, tmp_path):
    """Two byte-identical files indexed concurrently must yield exactly one track."""
    store = MemoryStore()
    indexer = Indexer(settings, store)
    src = audio_dir / "Gamma Call.wav"
    paths = []
    for i in range(6):
        p = tmp_path / f"copy{i}.wav"
        shutil.copy(src, p)
        paths.append(str(p))
    summary = indexer.index_paths(paths)
    indexer.close()
    assert summary.indexed == 1 and summary.duplicates == 5 and store.count_tracks() == 1


def test_storage_failure_aborts_run_with_error(settings, audio_dir):
    from fingerprint.utils.exceptions import StorageError

    class BrokenStore(MemoryStore):
        def add_track(self, record, hashes, times):
            raise StorageError("disk full")

    indexer = Indexer(settings, BrokenStore())
    with pytest.raises(StorageError, match="disk full"):
        indexer.index_directory(str(audio_dir))
    indexer.close()


def test_index_paths_cancellation(settings, audio_dir):
    store = MemoryStore()
    indexer = Indexer(settings, store)
    paths = find_media_files(str(audio_dir)) * 3
    calls = {"n": 0}

    def should_cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    summary = indexer.index_paths(paths, should_cancel=should_cancel)
    indexer.close()
    assert summary.cancelled is True
    assert summary.indexed + summary.duplicates + summary.failed < len(paths)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


def _wait(job, timeout=10):
    deadline = time.time() + timeout
    while not job.is_terminal and time.time() < deadline:
        time.sleep(0.01)
    assert job.is_terminal, job.status
    return job


def test_job_lifecycle_and_persistence(tmp_path):
    persist = tmp_path / "jobs"
    manager = JobManager(max_concurrent_jobs=2, history_limit=3, persist_dir=str(persist))

    def runner(job):
        job.update(total=2)
        job.update(completed=1, succeeded=1, current_item="a")
        manager.touch(job)
        job.update(completed=2, succeeded=2, current_item=None)
        return {"ok": True}

    job = manager.submit("upload", "file.wav", runner, total=2, meta={"filename": "file.wav"})
    _wait(job)
    assert job.status == COMPLETED and job.result == {"ok": True} and job.to_dict()["percent"] == 100.0
    assert (persist / f"{job.id}.json").is_file()
    assert manager.get(job.id) is job
    with pytest.raises(NotFoundError):
        manager.get("nope")

    def failing(job):
        raise RuntimeError("boom")

    failed = _wait(manager.submit("directory", "/x", failing))
    assert failed.status == FAILED and "boom" in failed.error

    assert [j.id for j in manager.list(status="failed")] == [failed.id]
    assert len(manager.list()) == 2 and len(manager.list(limit=1)) == 1
    with pytest.raises(JobError):
        manager.cancel(failed.id)
    manager.remove(failed.id)
    assert not (persist / f"{failed.id}.json").exists()

    # history is bounded
    for _ in range(5):
        _wait(manager.submit("upload", "x", lambda j: None))
    assert len(manager.list()) <= 3
    manager.shutdown(wait=True)

    # a job that was "running" when the process died is reported as interrupted after reload
    stale = {"job_id": "stale123", "type": "directory", "label": "/old", "status": RUNNING, "created_at": time.time() - 100, "total": 10, "completed": 4}
    (persist / "stale123.json").write_text(json.dumps(stale))
    (persist / "garbage.json").write_text("{not json")
    reloaded = JobManager(persist_dir=str(persist))
    assert reloaded.get("stale123").status == INTERRUPTED and "restarted" in reloaded.get("stale123").error
    reloaded.shutdown()


def test_job_cancellation_running_and_pending():
    manager = JobManager(max_concurrent_jobs=1, history_limit=10)
    started = threading.Event()
    release = threading.Event()

    def slow(job):
        started.set()
        while not release.is_set():
            job.check_cancelled()
            time.sleep(0.01)
        return {"finished": True}

    running = manager.submit("directory", "slow", slow)
    assert started.wait(5)
    pending = manager.submit("directory", "pending", lambda j: {"ran": True})
    assert pending.status == "pending"
    manager.cancel(pending.id)
    assert pending.status == CANCELLED
    cancelled = manager.cancel(running.id)
    assert cancelled.cancel_requested and running.should_cancel()
    _wait(running)
    assert running.status == CANCELLED and running.to_dict()["cancel_requested"] is True
    release.set()
    manager.shutdown(wait=True)
    with pytest.raises(JobError):
        manager.submit("upload", "late", lambda j: None)


def test_job_add_error_cap_and_dict_shape():
    manager = JobManager(history_limit=5)

    def runner(job):
        for i in range(20):
            job.add_error(f"f{i}", "bad", "code", limit=5)
        raise JobCancelled()

    job = _wait(manager.submit("directory", "d", runner))
    assert job.status == CANCELLED and len(job.errors) == 5
    data = job.to_dict(include_errors=False)
    assert "errors" not in data and data["error_count"] == 5 and data["eta_sec"] is None
    manager.shutdown()
