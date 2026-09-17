"""Progress bookkeeping shared by jobs and the CLI (rate, ETA, percent)."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field


@dataclass
class ProgressSnapshot:
    total: int
    completed: int
    succeeded: int
    failed: int
    skipped: int
    current_item: str | None
    elapsed_sec: float
    eta_sec: float | None
    rate_per_sec: float
    percent: float

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "completed": self.completed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "current_item": self.current_item,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "eta_sec": None if self.eta_sec is None else round(self.eta_sec, 1),
            "rate_per_sec": round(self.rate_per_sec, 3),
            "percent": round(self.percent, 1),
        }


@dataclass
class ProgressTracker:
    """Thread-safe counter with rate/ETA estimation."""

    total: int = 0
    completed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    current_item: str | None = None
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start_item(self, name: str | None) -> None:
        with self._lock:
            self.current_item = name

    def finish_item(self, outcome: str, name: str | None = None) -> None:
        with self._lock:
            self.completed += 1
            if outcome == "indexed":
                self.succeeded += 1
            elif outcome == "duplicate":
                self.skipped += 1
            else:
                self.failed += 1
            if name is not None:
                self.current_item = name

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            elapsed = max(time.time() - self.started_at, 1e-6)
            rate = self.completed / elapsed
            remaining = max(self.total - self.completed, 0)
            eta = (remaining / rate) if (rate > 0 and self.completed > 0) else None
            percent = (100.0 * self.completed / self.total) if self.total else 0.0
            return ProgressSnapshot(
                self.total,
                self.completed,
                self.succeeded,
                self.failed,
                self.skipped,
                self.current_item,
                elapsed,
                eta,
                rate,
                percent,
            )


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_bar(snapshot: ProgressSnapshot, width: int = 28) -> str:
    """One-line terminal progress bar."""
    filled = int(width * snapshot.percent / 100)
    bar = "#" * filled + "-" * (width - filled)
    item = (snapshot.current_item or "")[-40:]
    return (
        f"[{bar}] {snapshot.completed}/{snapshot.total} ({snapshot.percent:5.1f}%) "
        f"ok={snapshot.succeeded} dup={snapshot.skipped} fail={snapshot.failed} "
        f"eta {format_eta(snapshot.eta_sec)} {item}"
    )


def print_progress(snapshot: ProgressSnapshot, stream=None) -> None:
    stream = stream or sys.stderr
    stream.write("\r" + render_bar(snapshot).ljust(110)[:110])
    stream.flush()
    if snapshot.completed >= snapshot.total:
        stream.write("\n")
