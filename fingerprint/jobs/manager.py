"""Background job manager.

Long-running work (indexing uploads and folders) runs on a small thread pool
with a fixed number of workers. Jobs are thread-safe and cancellable, keep a
capped error list, and can be written out as JSON files so the history survives
a restart. A job that was still running when the process died comes back as
``interrupted``.

The manager knows nothing about storage and pulls in nothing beyond the standard
library and the package's own utils, so other job types (re-indexing, exports,
transcription) can use it later on.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..utils.exceptions import JobError, NotFoundError
from ..utils.logging import job_id_var

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED, INTERRUPTED})
ALL_STATUSES = (PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, INTERRUPTED)


class JobCancelled(Exception):
    """Raised inside a runner to abort cooperatively."""


@dataclass
class Job:
    type: str
    label: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    total: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    current_item: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _future: Future | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ runner-facing API
    def should_cancel(self) -> bool:
        return self._cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise JobCancelled()

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if not hasattr(self, key) or key.startswith("_"):
                    raise AttributeError(key)
                setattr(self, key, value)

    def add_error(self, item: str, message: str, code: str | None = None, limit: int = 500) -> None:
        with self._lock:
            if len(self.errors) < limit:
                self.errors.append({"file": item, "error": message, "error_code": code})

    # ------------------------------------------------------------------ presentation
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self, include_errors: bool = True) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            started = self.started_at or self.created_at
            end = self.finished_at or now
            elapsed = max(end - started, 0.0)
            rate = (self.completed / elapsed) if (elapsed > 0 and self.completed) else 0.0
            remaining = max(self.total - self.completed, 0)
            eta = (remaining / rate) if (rate > 0 and self.status == RUNNING) else None
            percent = (100.0 * self.completed / self.total) if self.total else (100.0 if self.status == COMPLETED else 0.0)
            data: dict[str, Any] = {
                "job_id": self.id,
                "type": self.type,
                "label": self.label,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "total": self.total,
                "completed": self.completed,
                "succeeded": self.succeeded,
                "failed": self.failed,
                "skipped": self.skipped,
                "current_item": self.current_item,
                "percent": round(percent, 1),
                "elapsed_sec": round(elapsed, 2),
                "eta_sec": None if eta is None else round(eta, 1),
                "rate_per_sec": round(rate, 3),
                "error_count": len(self.errors),
                "cancel_requested": self.cancel_requested,
                "result": self.result,
                "error": self.error,
                "meta": self.meta,
            }
            if include_errors:
                data["errors"] = list(self.errors)
            return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        job = cls(type=data.get("type", "unknown"), label=data.get("label", ""), id=data.get("job_id") or uuid.uuid4().hex)
        for key in (
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "total",
            "completed",
            "succeeded",
            "failed",
            "skipped",
            "current_item",
            "result",
            "error",
            "meta",
            "cancel_requested",
        ):
            if key in data and data[key] is not None:
                setattr(job, key, data[key])
        job.errors = list(data.get("errors") or [])
        return job


Runner = Callable[[Job], dict[str, Any] | None]


class JobManager:
    def __init__(
        self,
        max_concurrent_jobs: int = 2,
        history_limit: int = 200,
        persist_dir: str | None = None,
        max_errors: int = 500,
    ):
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self.history_limit = max(1, int(history_limit))
        self.persist_dir = persist_dir
        self.max_errors = max_errors
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent_jobs, thread_name_prefix="audiofp-job")
        self._last_persist: dict[str, float] = {}
        self._closed = False
        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)
            self._load_history()

    # ------------------------------------------------------------------ submission
    def submit(self, job_type: str, label: str, runner: Runner, *, total: int = 0, meta: dict[str, Any] | None = None) -> Job:
        if self._closed:
            raise JobError("Job manager is shutting down")
        job = Job(type=job_type, label=label, total=total, meta=meta or {})
        with self._lock:
            self._jobs[job.id] = job
            self._trim_history()
        self._persist(job, force=True)
        job._future = self._executor.submit(self._run, job, runner)
        logger.info("Job %s queued: %s (%s)", job.id, label, job_type)
        return job

    def _run(self, job: Job, runner: Runner) -> None:
        token = job_id_var.set(job.id)
        try:
            if job.should_cancel():
                job.update(status=CANCELLED, finished_at=time.time())
                self._persist(job, force=True)
                return
            job.update(status=RUNNING, started_at=time.time())
            self._persist(job, force=True)
            result = runner(job)
            if job.should_cancel():
                job.update(status=CANCELLED, result=result, finished_at=time.time())
            else:
                job.update(status=COMPLETED, result=result, finished_at=time.time())
            logger.info("Job %s %s in %.1fs", job.id, job.status, (job.finished_at or 0) - (job.started_at or 0))
        except JobCancelled:
            job.update(status=CANCELLED, finished_at=time.time())
            logger.info("Job %s cancelled", job.id)
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            job.update(status=FAILED, error=message, finished_at=time.time())
        finally:
            self._persist(job, force=True)
            with self._lock:
                self._trim_history()
            job_id_var.reset(token)

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"Job '{job_id}' not found", details={"job_id": job_id})
        return job

    def list(self, status: str | None = None, job_type: str | None = None, limit: int | None = None) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            wanted = set(status.split(","))
            if wanted == {"active"}:
                wanted = {PENDING, RUNNING}
            jobs = [j for j in jobs if j.status in wanted]
        if job_type:
            jobs = [j for j in jobs if j.type == job_type]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit] if limit else jobs

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in (PENDING, RUNNING))

    # ------------------------------------------------------------------ control
    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.is_terminal:
            raise JobError(f"Job is already {job.status}", details={"status": job.status})
        job._cancel_event.set()
        job.update(cancel_requested=True)
        if job.status == PENDING and job._future is not None and job._future.cancel():
            job.update(status=CANCELLED, finished_at=time.time())
        self._persist(job, force=True)
        logger.info("Cancellation requested for job %s", job.id)
        return job

    def remove(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job.is_terminal:
            raise JobError("Cannot remove a job that is still running; cancel it first")
        with self._lock:
            self._jobs.pop(job_id, None)
        self._delete_file(job_id)

    def touch(self, job: Job) -> None:
        """Persist progress (throttled). Runners call this after updating counters."""
        self._persist(job, force=False)

    def shutdown(self, wait: bool = False) -> None:
        self._closed = True
        with self._lock:
            active = [j for j in self._jobs.values() if not j.is_terminal]
        for job in active:
            job._cancel_event.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        for job in active:
            if job.status == PENDING:
                job.update(status=CANCELLED, finished_at=time.time())
                self._persist(job, force=True)

    # ------------------------------------------------------------------ history / persistence
    def _trim_history(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.is_terminal), key=lambda j: j.finished_at or j.created_at)
        excess = len(finished) - self.history_limit
        for job in finished[: max(excess, 0)]:
            self._jobs.pop(job.id, None)
            self._delete_file(job.id)

    def _file_for(self, job_id: str) -> str | None:
        return os.path.join(self.persist_dir, f"{job_id}.json") if self.persist_dir else None

    def _persist(self, job: Job, force: bool) -> None:
        path = self._file_for(job.id)
        if not path:
            return
        now = time.time()
        if not force and now - self._last_persist.get(job.id, 0.0) < 2.0:
            return
        self._last_persist[job.id] = now
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(job.to_dict(), fh, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not persist job %s: %s", job.id, exc)

    def _delete_file(self, job_id: str) -> None:
        path = self._file_for(job_id)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:  # pragma: no cover
                pass
        self._last_persist.pop(job_id, None)

    def _load_history(self) -> None:
        assert self.persist_dir
        loaded = 0
        for name in os.listdir(self.persist_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.persist_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    job = Job.from_dict(json.load(fh))
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable job file %s: %s", name, exc)
                continue
            if job.status in (PENDING, RUNNING):
                job.status = INTERRUPTED
                job.error = "The server restarted before this job finished. Files already indexed were kept; re-run to index the rest."
                job.finished_at = job.finished_at or time.time()
                self._persist(job, force=True)
            self._jobs[job.id] = job
            loaded += 1
        with self._lock:
            self._trim_history()
        if loaded:
            logger.info("Loaded %d job records from %s", loaded, self.persist_dir)
