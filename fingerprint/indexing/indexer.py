"""Index one file or many: decode -> fingerprint -> store, with de-duplication.

The :class:`Indexer` is used by the REST API (through background jobs), by the
CLI (``audiofp index``) and by tests.  It never raises for a bad *file*: every
file produces an :class:`IndexOutcome` describing what happened, so a single
corrupted MP3 cannot abort a 10 000-file run.  Configuration and storage
failures do raise, because retrying would not help.
"""

from __future__ import annotations

import collections
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from .. import formats
from ..config import Settings
from ..core.fingerprinter import Fingerprinter
from ..storage.base import StorageBackend, TrackRecord, normalize_tags
from ..utils.exceptions import AudioFPError, StorageError
from ..utils.files import sha256_file
from .progress import ProgressTracker
from .scanner import find_media_files, metadata_from_filename

logger = logging.getLogger(__name__)

OUTCOME_INDEXED = "indexed"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"


@dataclass
class IndexOutcome:
    path: str
    status: str  # indexed | duplicate | failed
    track: TrackRecord | None = None
    duplicate_of: str | None = None  # track_id of the existing copy
    error: str | None = None
    error_code: str | None = None
    elapsed_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "file": self.path,
            "filename": os.path.basename(self.path),
            "status": self.status,
            "elapsed_sec": round(self.elapsed_sec, 3),
        }
        if self.track is not None:
            out["track_id"] = self.track.track_id
        if self.duplicate_of:
            out["duplicate_of"] = self.duplicate_of
        if self.error:
            out["error"] = self.error
            out["error_code"] = self.error_code
        return out


@dataclass
class IndexSummary:
    total: int = 0
    indexed: int = 0
    duplicates: int = 0
    failed: int = 0
    cancelled: bool = False
    elapsed_sec: float = 0.0
    track_ids: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    duplicates_of: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "indexed": self.indexed,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "track_ids": self.track_ids,
            "errors": self.errors,
            "duplicates_of": self.duplicates_of,
        }


ProgressCallback = Callable[[str, IndexOutcome | None, ProgressTracker], None]


class Indexer:
    """Fingerprint files and persist them."""

    def __init__(
        self,
        settings: Settings,
        storage: StorageBackend,
        fingerprinter: Fingerprinter | None = None,
        executor: Executor | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.fingerprinter = fingerprinter or Fingerprinter(settings)
        self._executor = executor
        self._owns_executor = executor is None
        # Serialises the "is it already there? -> store" step so two identical files indexed
        # concurrently (same batch, several workers) cannot both slip past the duplicate check.
        self._store_lock = threading.Lock()

    # ------------------------------------------------------------------ executor
    @property
    def executor(self) -> Executor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.settings.effective_index_workers, thread_name_prefix="audiofp-index")
        return self._executor

    def close(self) -> None:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # ------------------------------------------------------------------ single file
    def index_file(
        self,
        path: str,
        *,
        title: str | None = None,
        artist: str | None = None,
        tags: Iterable[str] | str | None = None,
        metadata: dict[str, Any] | None = None,
        source: str = "file",
        original_filename: str | None = None,
        track_id: str | None = None,
    ) -> IndexOutcome:
        """Index a single file. Never raises for problems with the file itself."""
        started = time.time()
        path = os.path.abspath(path)
        display_name = original_filename or os.path.basename(path)
        try:
            if not os.path.isfile(path):
                return IndexOutcome(path, OUTCOME_FAILED, error="File not found", error_code="file_not_found")
            if not formats.is_supported(display_name) and not formats.is_supported(path):
                return IndexOutcome(
                    path,
                    OUTCOME_FAILED,
                    error=f"Unsupported file type '{formats.extension_of(display_name) or 'none'}'",
                    error_code="unsupported_format",
                )

            file_size = os.path.getsize(path)
            content_hash = ""
            if self.settings.dedupe == "content":
                content_hash = sha256_file(path)
                existing = self.storage.find_by_content_hash(content_hash)
                if existing is not None:
                    logger.info("Skipping %s: identical content already indexed as %s", display_name, existing.track_id)
                    return IndexOutcome(path, OUTCOME_DUPLICATE, track=existing, duplicate_of=existing.track_id, elapsed_sec=time.time() - started)
            elif self.settings.dedupe == "path":
                existing = self.storage.find_by_filepath(path)
                if existing is not None:
                    return IndexOutcome(path, OUTCOME_DUPLICATE, track=existing, duplicate_of=existing.track_id, elapsed_sec=time.time() - started)

            fp = self.fingerprinter.fingerprint_file(path, display_name=display_name)

            parsed = metadata_from_filename(display_name)
            record = TrackRecord(
                title=(title or "").strip() or parsed["title"],
                artist=(artist or "").strip() or parsed["artist"],
                filename=display_name,
                filepath=path,
                content_hash=content_hash,
                duration=round(fp.duration_sec, 3),
                num_peaks=fp.num_peaks,
                num_hashes=fp.num_hashes,
                source_type=formats.source_type_of(display_name),
                file_size=file_size,
                tags=normalize_tags(tags),
                metadata={"source": source, **(metadata or {})},
            )
            if track_id:
                record.track_id = track_id
            if fp.num_hashes == 0:
                return IndexOutcome(
                    path,
                    OUTCOME_FAILED,
                    error="No fingerprint could be extracted (silent, extremely short or noise-only audio)",
                    error_code="empty_fingerprint",
                    elapsed_sec=time.time() - started,
                )

            with self._store_lock:
                if self.settings.dedupe == "content":
                    existing = self.storage.find_by_content_hash(content_hash)
                    if existing is not None:
                        return IndexOutcome(path, OUTCOME_DUPLICATE, track=existing, duplicate_of=existing.track_id, elapsed_sec=time.time() - started)
                elif self.settings.dedupe == "path":
                    existing = self.storage.find_by_filepath(path)
                    if existing is not None:
                        return IndexOutcome(path, OUTCOME_DUPLICATE, track=existing, duplicate_of=existing.track_id, elapsed_sec=time.time() - started)
                stored = self.storage.add_track(record, fp.hashes, fp.hash_times)
            elapsed = time.time() - started
            logger.info(
                "Indexed %s: %.1fs audio, %d peaks, %d hashes in %.2fs (%s)",
                display_name,
                fp.duration_sec,
                fp.num_peaks,
                fp.num_hashes,
                elapsed,
                stored.track_id,
            )
            return IndexOutcome(path, OUTCOME_INDEXED, track=stored, elapsed_sec=elapsed)

        except StorageError:
            raise
        except AudioFPError as exc:
            logger.warning("Failed to index %s: %s", display_name, exc.message)
            return IndexOutcome(path, OUTCOME_FAILED, error=exc.message, error_code=exc.code, elapsed_sec=time.time() - started)
        except MemoryError:
            logger.error("Out of memory while indexing %s", display_name)
            return IndexOutcome(
                path, OUTCOME_FAILED, error="Out of memory while processing this file", error_code="out_of_memory", elapsed_sec=time.time() - started
            )
        except Exception as exc:
            logger.exception("Unexpected error while indexing %s", display_name)
            return IndexOutcome(path, OUTCOME_FAILED, error=f"{type(exc).__name__}: {exc}", error_code="internal_error", elapsed_sec=time.time() - started)

    # ------------------------------------------------------------------ many files
    def index_paths(
        self,
        paths: list[str],
        *,
        tags: Iterable[str] | str | None = None,
        source: str = "directory",
        progress: ProgressCallback | None = None,
        should_cancel: Callable[[], bool] | None = None,
        max_errors: int | None = None,
        tracker: ProgressTracker | None = None,
    ) -> IndexSummary:
        """Index many files concurrently on the shared executor.

        Files are submitted in a sliding window (2 x workers) so cancellation is
        prompt and memory stays bounded even for 100 000-file runs.
        """
        started = time.time()
        summary = IndexSummary(total=len(paths))
        tracker = tracker or ProgressTracker(total=len(paths))
        tracker.total = len(paths)
        max_errors = self.settings.job_max_errors if max_errors is None else max_errors
        if not paths:
            summary.elapsed_sec = time.time() - started
            return summary

        window = max(2, self.settings.effective_index_workers * 2)
        pending: dict[Future, str] = {}
        queue = collections.deque(paths)
        storage_failure: StorageError | None = None

        def _submit_next() -> None:
            while queue and len(pending) < window:
                if should_cancel and should_cancel():
                    queue.clear()
                    return
                path = queue.popleft()
                tracker.start_item(os.path.basename(path))
                fut = self.executor.submit(self.index_file, path, tags=tags, source=source)
                pending[fut] = path

        _submit_next()
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                path = pending.pop(fut)
                try:
                    outcome = fut.result()
                except StorageError as exc:
                    # Storage is down: stop the whole run rather than fail every file individually.
                    logger.error("Storage failure during indexing: %s", exc)
                    storage_failure = storage_failure or exc
                    summary.errors.append({"file": path, "error": exc.message, "error_code": exc.code})
                    queue.clear()
                    outcome = None
                except Exception as exc:
                    logger.exception("Worker crashed on %s", path)
                    outcome = IndexOutcome(path, OUTCOME_FAILED, error=str(exc), error_code="internal_error")
                if outcome is not None:
                    self._record(summary, outcome, max_errors)
                    tracker.finish_item(outcome.status, os.path.basename(path))
                if progress:
                    try:
                        progress(path, outcome, tracker)
                    except Exception:
                        logger.exception("progress callback failed")
            _submit_next()
            if should_cancel and should_cancel() and queue:
                queue.clear()

        if should_cancel and should_cancel():
            summary.cancelled = True
        try:
            self.storage.flush()  # batched backends: make everything from this run durable
        except StorageError as exc:
            storage_failure = storage_failure or exc
        summary.elapsed_sec = time.time() - started
        if storage_failure is not None:
            # Surface the root cause to the caller (job -> failed, CLI -> error) instead of a quiet "completed".
            raise StorageError(
                f"Indexing stopped after {summary.indexed} file(s): {storage_failure.message}",
                code=storage_failure.code,
                details={"indexed": summary.indexed, "failed": summary.failed, "duplicates": summary.duplicates},
            ) from storage_failure
        logger.info(
            "Indexing run finished: %d indexed, %d duplicates, %d failed of %d in %.1fs%s",
            summary.indexed,
            summary.duplicates,
            summary.failed,
            summary.total,
            summary.elapsed_sec,
            " (cancelled)" if summary.cancelled else "",
        )
        return summary

    @staticmethod
    def _record(summary: IndexSummary, outcome: IndexOutcome, max_errors: int) -> None:
        if outcome.status == OUTCOME_INDEXED:
            summary.indexed += 1
            if outcome.track is not None:
                summary.track_ids.append(outcome.track.track_id)
        elif outcome.status == OUTCOME_DUPLICATE:
            summary.duplicates += 1
            if len(summary.duplicates_of) < max_errors:
                summary.duplicates_of.append({"file": outcome.path, "duplicate_of": outcome.duplicate_of})
        else:
            summary.failed += 1
            if len(summary.errors) < max_errors:
                summary.errors.append({"file": outcome.path, "error": outcome.error, "error_code": outcome.error_code})

    def index_directory(self, directory: str, *, recursive: bool = True, **kwargs) -> IndexSummary:
        paths = find_media_files(directory, recursive=recursive)
        return self.index_paths(paths, **kwargs)
