"""In-memory storage backend.

Meant for tests, demos and throw-away experiments. Nothing survives a restart.
The inverted index is a list of sorted segments, one per added track, merged
into a single segment once there are more than ``MAX_SEGMENTS``. Adding a
track never re-sorts the whole library and lookups stay vectorised
(``searchsorted`` per segment).
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterable
from typing import Any

import numpy as np

from .base import StorageBackend, TrackRecord, normalize_tags, validate_track_changes

MAX_SEGMENTS = 64


class MemoryStore(StorageBackend):
    backend_name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._meta: dict[str, str] = {}
        self._tracks: dict[str, TrackRecord] = {}  # keyed by track_id
        self._by_ref: dict[int, TrackRecord] = {}
        self._next_ref = 1
        # Sorted index segments: (hashes sorted, refs, times). Deleted refs are masked out lazily.
        self._segments: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self._deleted_refs: set[int] = set()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        return self._meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    # ------------------------------------------------------------------ tracks
    def add_track(self, record: TrackRecord, hashes: np.ndarray, times: np.ndarray) -> TrackRecord:
        with self._lock:
            existing = self._tracks.get(record.track_id)
            if existing is not None:
                self._remove_ref(existing.ref)
            record = copy.deepcopy(record)
            record.tags = normalize_tags(record.tags)
            record.ref = self._next_ref
            record.num_hashes = int(np.asarray(hashes).size)
            self._next_ref += 1
            self._tracks[record.track_id] = record
            self._by_ref[record.ref] = record
            h = np.asarray(hashes, dtype=np.int64)
            t = np.asarray(times, dtype=np.int64)
            order = np.argsort(h, kind="stable")
            self._segments.append((h[order], np.full(h.size, record.ref, dtype=np.int64), t[order]))
            if len(self._segments) > MAX_SEGMENTS:
                self._compact()
            return copy.deepcopy(record)

    def _compact(self) -> None:
        """Merge all segments into one (drops rows of deleted tracks). Call with the lock held."""
        if not self._segments:
            return
        hashes = np.concatenate([s[0] for s in self._segments])
        refs = np.concatenate([s[1] for s in self._segments])
        times = np.concatenate([s[2] for s in self._segments])
        if self._deleted_refs:
            keep = ~np.isin(refs, np.fromiter(self._deleted_refs, dtype=np.int64))
            hashes, refs, times = hashes[keep], refs[keep], times[keep]
            self._deleted_refs.clear()
        order = np.argsort(hashes, kind="stable")
        self._segments = [(hashes[order], refs[order], times[order])] if hashes.size else []

    def _remove_ref(self, ref: int | None) -> None:
        if ref is None:
            return
        rec = self._by_ref.pop(ref, None)
        if rec is not None:
            self._tracks.pop(rec.track_id, None)
        self._deleted_refs.add(int(ref))
        if len(self._deleted_refs) > MAX_SEGMENTS:
            self._compact()

    def get_track(self, track_id: str) -> TrackRecord | None:
        with self._lock:
            rec = self._tracks.get(track_id)
            return copy.deepcopy(rec) if rec else None

    def get_tracks_by_ref(self, refs: Iterable[int]) -> dict[int, TrackRecord]:
        with self._lock:
            return {int(r): copy.deepcopy(self._by_ref[int(r)]) for r in refs if int(r) in self._by_ref}

    def find_by_content_hash(self, content_hash: str) -> TrackRecord | None:
        if not content_hash:
            return None
        with self._lock:
            for rec in self._tracks.values():
                if rec.content_hash == content_hash:
                    return copy.deepcopy(rec)
        return None

    def find_by_filepath(self, filepath: str) -> TrackRecord | None:
        if not filepath:
            return None
        with self._lock:
            for rec in self._tracks.values():
                if rec.filepath == filepath:
                    return copy.deepcopy(rec)
        return None

    def list_tracks(self, *, query=None, sort="indexed_at", order="desc", offset=0, limit=50, source_type=None, tag=None):
        sort_field, desc = self._sort_key(sort, order)
        with self._lock:
            items = list(self._tracks.values())
        if query:
            q = query.lower()
            items = [r for r in items if q in r.title.lower() or q in r.artist.lower() or q in r.filename.lower() or q in " ".join(r.tags)]
        if source_type:
            items = [r for r in items if r.source_type == source_type]
        if tag:
            t = tag.lower()
            items = [r for r in items if t in r.tags]
        items.sort(
            key=lambda r: (getattr(r, sort_field) or 0) if sort_field in ("indexed_at", "duration", "num_hashes") else str(getattr(r, sort_field)).lower(),
            reverse=desc,
        )
        total = len(items)
        page = items[offset : offset + limit] if limit else items[offset:]
        return [copy.deepcopy(r) for r in page], total

    def update_track(self, track_id: str, changes: dict[str, Any]) -> TrackRecord:
        clean = validate_track_changes(changes)
        with self._lock:
            rec = self.require_track(track_id)
            live = self._tracks[track_id]
            for key, value in clean.items():
                setattr(live, key, value)
            return copy.deepcopy(live) if live else rec

    def delete_track(self, track_id: str) -> bool:
        with self._lock:
            rec = self._tracks.get(track_id)
            if rec is None:
                return False
            self._remove_ref(rec.ref)
            return True

    def count_tracks(self) -> int:
        with self._lock:
            return len(self._tracks)

    # ------------------------------------------------------------------ hashes
    def query_hashes(self, hash_values: np.ndarray, max_rows_per_hash: int | None = None, stats: dict | None = None):
        wanted = np.unique(np.asarray(hash_values, dtype=np.int64))
        with self._lock:
            segments = list(self._segments)
            deleted = np.fromiter(self._deleted_refs, dtype=np.int64) if self._deleted_refs else None
        if not segments or wanted.size == 0:
            if stats is not None:
                stats["skipped_hashes"] = 0
            return self._empty_hash_result()
        parts = []
        for hashes, refs, times in segments:
            left = np.searchsorted(hashes, wanted, side="left")
            right = np.searchsorted(hashes, wanted, side="right")
            counts = right - left
            hit = counts > 0
            if not hit.any():
                continue
            left, counts = left[hit], counts[hit]
            within = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
            idx = np.repeat(left, counts) + within
            parts.append((hashes[idx], refs[idx], times[idx]))
        if not parts:
            if stats is not None:
                stats["skipped_hashes"] = 0
            return self._empty_hash_result()
        h = np.concatenate([p[0] for p in parts])
        r = np.concatenate([p[1] for p in parts])
        t = np.concatenate([p[2] for p in parts])
        if deleted is not None:
            keep = ~np.isin(r, deleted)
            h, r, t = h[keep], r[keep], t[keep]
        skipped = 0
        if max_rows_per_hash and h.size:
            _u, inverse, counts = np.unique(h, return_inverse=True, return_counts=True)
            heavy = counts > max_rows_per_hash
            skipped = int(heavy.sum())
            if skipped:
                keep = ~heavy[inverse]
                h, r, t = h[keep], r[keep], t[keep]
        if stats is not None:
            stats["skipped_hashes"] = skipped
        return h, r, t

    # ------------------------------------------------------------------ stats
    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            tracks = list(self._tracks.values())
        return {
            "storage_type": self.backend_name,
            "total_tracks": len(tracks),
            "total_hashes": int(sum(r.num_hashes for r in tracks)),
            "total_duration_sec": round(float(sum(r.duration for r in tracks)), 2),
            "persistent": False,
        }

    def clear(self) -> None:
        with self._lock:
            self._tracks.clear()
            self._by_ref.clear()
            self._segments.clear()
            self._deleted_refs.clear()
