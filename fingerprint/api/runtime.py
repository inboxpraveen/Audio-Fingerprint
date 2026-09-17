"""The application runtime: one object that wires settings, storage, engine and jobs together.

The Flask app keeps a single :class:`Runtime` in ``app.extensions["audiofp"]``.
Route handlers stay thin: they parse the request, call a runtime method and
format the result. The CLI uses the same object without Flask.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .. import __version__, formats
from ..config import FINGERPRINT_ALGORITHM_VERSION, Settings
from ..core import Fingerprinter, Matcher, MatchOptions, TrackMatch, ffmpeg_info
from ..core.fingerprinter import Fingerprint
from ..indexing import Indexer, IndexSummary, find_media_files
from ..jobs import Job, JobManager
from ..storage import StorageBackend, create_storage
from ..utils.exceptions import ForbiddenError, JobError, ValidationError
from ..utils.files import ensure_dir, is_within

logger = logging.getLogger(__name__)

RUNTIME_SETTING_KEYS = ("top_k", "min_confidence", "min_aligned_hashes", "min_peak_ratio", "mode")


@dataclass
class SearchResult:
    query: Fingerprint
    matches: list[TrackMatch]
    options: MatchOptions
    processing_ms: float
    truncated: bool = False
    filename: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Runtime:
    def __init__(self, settings: Settings, storage: StorageBackend | None = None):
        self.settings = settings
        self.started_at = time.time()
        ensure_dir(settings.data_dir)
        ensure_dir(settings.upload_dir_resolved)
        self.storage = storage or create_storage(settings)
        self.fingerprinter = Fingerprinter(settings)
        self.matcher = Matcher(settings)
        self.indexer = Indexer(settings, self.storage, self.fingerprinter)
        self.jobs = JobManager(
            max_concurrent_jobs=settings.max_concurrent_jobs,
            history_limit=settings.job_history_limit,
            persist_dir=settings.jobs_dir if settings.persist_jobs else None,
            max_errors=settings.job_max_errors,
        )
        self._runtime_lock = threading.Lock()
        self._runtime_settings: dict[str, Any] = self._load_runtime_settings()
        self._closed = False
        logger.info(
            "AudioFP %s ready: storage=%s, workers=%d, ffmpeg=%s, fingerprint signature=%s",
            __version__,
            self.storage.backend_name,
            settings.effective_index_workers,
            "yes" if ffmpeg_info().available else "no",
            settings.fingerprint_signature(),
        )

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        """Stop background jobs at the next file boundary, flush buffered fingerprints and release storage.

        Safe to call more than once: ``audiofp serve`` calls it on shutdown (SIGINT/SIGTERM) and ``atexit`` calls it again.
        """
        if self._closed:
            return
        self._closed = True
        logger.info("Shutting down: stopping jobs and flushing storage")
        try:
            # running jobs see their cancel flag, finish the files already in flight (a bounded amount of
            # work) and write a 'cancelled' record. Wait for that before closing the storage under them.
            self.jobs.shutdown(wait=True)
        finally:
            self.indexer.close()
            self.storage.close()

    # ------------------------------------------------------------------ runtime-adjustable search defaults
    def _load_runtime_settings(self) -> dict[str, Any]:
        defaults = {
            "top_k": self.settings.top_k,
            "min_confidence": self.settings.min_confidence,
            "min_aligned_hashes": self.settings.min_aligned_hashes,
            "min_peak_ratio": self.settings.min_peak_ratio,
            "mode": "identify",
        }
        path = self.settings.runtime_settings_path
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    stored = json.load(fh)
                defaults.update({k: v for k, v in stored.items() if k in RUNTIME_SETTING_KEYS})
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable runtime settings file %s: %s", path, exc)
        return defaults

    def get_runtime_settings(self) -> dict[str, Any]:
        with self._runtime_lock:
            return dict(self._runtime_settings)

    def update_runtime_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        unknown = set(changes) - set(RUNTIME_SETTING_KEYS)
        if unknown:
            raise ValidationError(f"Unknown setting(s): {', '.join(sorted(unknown))}", details={"allowed": list(RUNTIME_SETTING_KEYS)})
        clean: dict[str, Any] = {}
        try:
            if "top_k" in changes:
                clean["top_k"] = max(1, min(int(changes["top_k"]), self.settings.max_top_k))
            if "min_confidence" in changes:
                clean["min_confidence"] = min(1.0, max(0.0, float(changes["min_confidence"])))
            if "min_aligned_hashes" in changes:
                clean["min_aligned_hashes"] = max(1, int(changes["min_aligned_hashes"]))
            if "min_peak_ratio" in changes:
                clean["min_peak_ratio"] = max(0.0, float(changes["min_peak_ratio"]))
            if "mode" in changes:
                mode = str(changes["mode"]).lower()
                if mode not in ("identify", "occurrences"):
                    raise ValidationError("mode must be 'identify' or 'occurrences'")
                clean["mode"] = mode
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"Invalid setting value: {exc}") from exc
        with self._runtime_lock:
            self._runtime_settings.update(clean)
            snapshot = dict(self._runtime_settings)
        try:
            with open(self.settings.runtime_settings_path, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
        except OSError as exc:
            logger.warning("Could not persist runtime settings: %s", exc)
        return snapshot

    def match_options(self, **overrides: Any) -> MatchOptions:
        base = self.get_runtime_settings()
        merged = {**base, **{k: v for k, v in overrides.items() if v is not None}}
        return MatchOptions.from_settings(self.settings, **merged)

    # ------------------------------------------------------------------ search
    def search_file(self, path: str, filename: str = "", **overrides: Any) -> SearchResult:
        started = time.perf_counter()
        options = self.match_options(**overrides)
        query = self.fingerprinter.fingerprint_file(path, max_seconds=self.settings.max_query_seconds, display_name=filename or None)
        truncated = query.duration_sec >= self.settings.max_query_seconds - 0.5
        matches = self.matcher.match(query, self.storage, options)
        diagnostics = self.matcher.last_diagnostics.to_dict()
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "Search %s: %.1fs audio, %d hashes, %d match(es) in %.0f ms [mode=%s]",
            filename or os.path.basename(path),
            query.duration_sec,
            query.num_hashes,
            len(matches),
            elapsed_ms,
            options.mode,
        )
        return SearchResult(
            query=query, matches=matches, options=options, processing_ms=elapsed_ms, truncated=truncated, filename=filename, extra={"diagnostics": diagnostics}
        )

    # ------------------------------------------------------------------ indexing jobs
    def start_upload_job(self, saved_path: str, original_name: str, *, title: str | None, artist: str | None, tags: Any, metadata: dict | None) -> Job:
        settings = self.settings

        def run(job: Job) -> dict[str, Any]:
            job.update(current_item=original_name, total=1)
            outcome = self.indexer.index_file(
                saved_path, title=title, artist=artist, tags=tags, metadata=metadata, source="upload", original_filename=original_name
            )
            job.update(completed=1, current_item=None)
            if outcome.status == "indexed":
                job.update(succeeded=1)
            elif outcome.status == "duplicate":
                job.update(skipped=1)
                self._discard_upload(saved_path, "duplicate")
            else:
                job.update(failed=1)
                job.add_error(original_name, outcome.error or "unknown error", outcome.error_code, settings.job_max_errors)
                if not settings.keep_failed_uploads:
                    self._discard_upload(saved_path, "failed")
            self.storage.flush()  # single uploads are small: make them durable right away
            result = outcome.to_dict()
            if outcome.track is not None:
                result["track"] = outcome.track.to_dict()
            return result

        return self.jobs.submit("upload", original_name, run, total=1, meta={"filename": original_name})

    def _discard_upload(self, path: str, reason: str) -> None:
        if is_within(path, self.settings.upload_dir_resolved):
            try:
                os.remove(path)
                logger.debug("Removed %s upload %s", reason, path)
            except OSError:  # pragma: no cover
                pass

    def check_directory_allowed(self, directory: str) -> None:
        settings = self.settings
        if not settings.allow_directory_indexing:
            raise ForbiddenError("Directory indexing is disabled on this server (AUDIOFP_ALLOW_DIRECTORY_INDEXING=false).")
        roots = settings.index_roots
        if roots:
            if not any(is_within(directory, root) for root in roots):
                raise ForbiddenError(
                    "That directory is outside the folders this server may index.",
                    details={"allowed_roots": roots},
                )
        elif settings.is_production:
            raise ForbiddenError(
                "In production, directory indexing requires AUDIOFP_INDEX_ROOTS to list the folders the server may read.",
            )

    def start_directory_job(self, directory: str, *, recursive: bool = True, tags: Any = None) -> tuple[Job, int]:
        self.check_directory_allowed(directory)
        paths = find_media_files(directory, recursive=recursive)
        if not paths:
            raise ValidationError(
                "No supported audio or video files were found in that directory.",
                details={"directory": directory, "supported": formats.describe_formats()},
            )
        if self.jobs.active_count() >= self.settings.max_concurrent_jobs * 4:
            raise JobError("Too many jobs queued; wait for running jobs to finish.", http_status=429)

        def run(job: Job) -> dict[str, Any]:
            job.update(total=len(paths))

            def on_progress(path: str, outcome, tracker) -> None:
                snap = tracker.snapshot()
                job.update(completed=snap.completed, succeeded=snap.succeeded, failed=snap.failed, skipped=snap.skipped, current_item=snap.current_item)
                if outcome is not None and outcome.status == "failed":
                    job.add_error(path, outcome.error or "unknown error", outcome.error_code, self.settings.job_max_errors)
                self.jobs.touch(job)

            summary: IndexSummary = self.indexer.index_paths(paths, tags=tags, source="directory", progress=on_progress, should_cancel=job.should_cancel)
            job.update(current_item=None)
            result = summary.to_dict()
            result["directory"] = directory
            return result

        job = self.jobs.submit("directory", directory, run, total=len(paths), meta={"directory": directory, "recursive": recursive})
        return job, len(paths)

    # ------------------------------------------------------------------ info / health
    def health(self) -> dict[str, Any]:
        storage_ok = self.storage.health_check()
        ff = ffmpeg_info()
        return {
            "status": "ok" if storage_ok else "degraded",
            "version": __version__,
            "uptime_sec": round(time.time() - self.started_at, 1),
            "storage": {"type": self.storage.backend_name, "ok": storage_ok},
            "ffmpeg": {"available": ff.available, "version": ff.version},
            "jobs": {"active": self.jobs.active_count()},
            "fingerprint": {"algorithm_version": FINGERPRINT_ALGORITHM_VERSION, "signature": self.settings.fingerprint_signature()},
            "timestamp": time.time(),
        }

    def info(self) -> dict[str, Any]:
        s = self.settings
        ff = ffmpeg_info()
        return {
            "version": __version__,
            "profile": s.profile,
            "storage_type": self.storage.backend_name,
            "ffmpeg": {"available": ff.available, "version": ff.version, "path": ff.path},
            "formats": formats.describe_formats(),
            "limits": {
                "max_upload_mb": s.max_upload_mb,
                "max_query_seconds": s.max_query_seconds,
                "max_top_k": s.max_top_k,
                "max_occurrences_per_track": s.max_occurrences_per_track,
            },
            "features": {
                "directory_indexing": s.allow_directory_indexing and (bool(s.index_roots) or not s.is_production),
                "index_roots": s.index_roots,
                "auth_required": bool(s.api_key),
                "dedupe": s.dedupe,
            },
            "fingerprint": s.fingerprint_params() | {"signature": s.fingerprint_signature()},
            "defaults": self.get_runtime_settings(),
            "frame_seconds": s.hop_length / s.sample_rate,
        }
