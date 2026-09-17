"""Storage backend contract.

A backend stores *tracks* (a song, a call recording, a jingle - any indexed
piece of audio) and the inverted index ``hash -> (track, frame)`` used for
matching.  Internally each track has a small integer ``ref`` that keeps the
fingerprint table compact; externally tracks are addressed by their opaque
``track_id`` (a UUID string).

All backends must pass the shared contract tests in ``tests/test_storage.py``.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from ..utils.exceptions import FingerprintCompatibilityError, NotFoundError, ValidationError

SORTABLE_FIELDS = ("indexed_at", "title", "artist", "duration", "filename", "num_hashes")
META_SIGNATURE = "fingerprint_signature"
META_PARAMS = "fingerprint_params"


@dataclass
class TrackRecord:
    """Metadata for one indexed track."""

    track_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ref: int | None = None
    title: str = ""
    artist: str = ""
    filename: str = ""
    filepath: str = ""
    content_hash: str = ""
    duration: float = 0.0
    num_peaks: int = 0
    num_hashes: int = 0
    source_type: str = "audio"
    file_size: int = 0
    indexed_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_path: bool = True) -> dict[str, Any]:
        data = asdict(self)
        data.pop("ref", None)
        if not include_path:
            data.pop("filepath", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackRecord:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("tags", [])
        kwargs.setdefault("metadata", {})
        return cls(**kwargs)


def normalize_tags(tags: Iterable[str] | str | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = tags.split(",")
    seen: list[str] = []
    for tag in tags:
        tag = str(tag).strip().lower()
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:50]


def validate_track_changes(changes: dict[str, Any]) -> dict[str, Any]:
    """Whitelist and sanitise fields a client may edit."""
    allowed = {"title", "artist", "tags", "metadata"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"Cannot edit field(s): {', '.join(sorted(unknown))}", details={"allowed": sorted(allowed)})
    clean: dict[str, Any] = {}
    for key in ("title", "artist"):
        if key in changes:
            value = changes[key]
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValidationError(f"{key} must be a string")
            clean[key] = value.strip()[:500]
    if "tags" in changes:
        clean["tags"] = normalize_tags(changes["tags"])
    if "metadata" in changes:
        meta = changes["metadata"]
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise ValidationError("metadata must be an object")
        try:
            if len(json.dumps(meta)) > 64 * 1024:
                raise ValidationError("metadata is too large (max 64 KB)")
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"metadata must be JSON-serialisable: {exc}") from exc
        clean["metadata"] = meta
    return clean


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    backend_name: str = "abstract"

    # ------------------------------------------------------------------ lifecycle
    def initialize(self, signature: str, params: dict[str, Any], compat: str = "strict") -> None:
        """Verify (or stamp) the fingerprint parameters this store was built with.

        ``compat`` is ``strict`` (raise on mismatch), ``warn`` or ``ignore``.
        """
        stored = self.get_meta(META_SIGNATURE)
        if stored is None:
            if self.count_tracks() == 0:
                self.set_meta(META_SIGNATURE, signature)
                self.set_meta(META_PARAMS, json.dumps(params, sort_keys=True))
                self.set_meta("created_at", str(time.time()))
            return
        if stored == signature or compat == "ignore":
            return
        message = (
            "The fingerprint database was built with different fingerprint parameters "
            f"(stored signature {stored}, current {signature}). Matching would silently degrade. "
            "Either restore the original AUDIOFP_* fingerprint settings, point AUDIOFP_SQLITE_PATH / "
            "AUDIOFP_POSTGRES_DSN at a new database, or re-index everything with `audiofp db reset`."
        )
        if compat == "warn":
            import logging

            logging.getLogger(__name__).warning(message)
            return
        raise FingerprintCompatibilityError(
            message,
            details={"stored_signature": stored, "current_signature": signature, "stored_params": self.get_meta(META_PARAMS)},
        )

    def close(self) -> None:  # noqa: B027 - optional hook, backends without connections need not override
        """Release connections. Safe to call multiple times."""

    def flush(self) -> int:
        """Write any buffered fingerprints; returns the number of rows written."""
        return 0

    def health_check(self) -> bool:
        try:
            self.count_tracks()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ meta
    @abstractmethod
    def get_meta(self, key: str) -> str | None: ...

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None: ...

    # ------------------------------------------------------------------ tracks
    @abstractmethod
    def add_track(self, record: TrackRecord, hashes: np.ndarray, times: np.ndarray) -> TrackRecord:
        """Persist a track and its fingerprints; returns the record with ``ref`` set.

        Adding a track whose ``track_id`` already exists replaces it atomically.
        """

    @abstractmethod
    def get_track(self, track_id: str) -> TrackRecord | None: ...

    @abstractmethod
    def get_tracks_by_ref(self, refs: Iterable[int]) -> dict[int, TrackRecord]: ...

    @abstractmethod
    def find_by_content_hash(self, content_hash: str) -> TrackRecord | None: ...

    @abstractmethod
    def find_by_filepath(self, filepath: str) -> TrackRecord | None: ...

    @abstractmethod
    def list_tracks(
        self,
        *,
        query: str | None = None,
        sort: str = "indexed_at",
        order: str = "desc",
        offset: int = 0,
        limit: int = 50,
        source_type: str | None = None,
        tag: str | None = None,
    ) -> tuple[list[TrackRecord], int]:
        """Return ``(page, total_matching)``."""

    @abstractmethod
    def update_track(self, track_id: str, changes: dict[str, Any]) -> TrackRecord: ...

    @abstractmethod
    def delete_track(self, track_id: str) -> bool: ...

    def delete_tracks(self, track_ids: Iterable[str]) -> int:
        return sum(1 for tid in track_ids if self.delete_track(tid))

    @abstractmethod
    def count_tracks(self) -> int: ...

    # ------------------------------------------------------------------ hashes
    @abstractmethod
    def query_hashes(
        self, hash_values: np.ndarray, max_rows_per_hash: int | None = None, stats: dict | None = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Look up many hashes at once.

        Returns three aligned arrays ``(hash_value int64, track_ref int64, time_offset int64)``,
        one entry per stored fingerprint whose hash is in *hash_values*.  Hashes that occur
        more than *max_rows_per_hash* times in the library are skipped entirely (they are
        "stop words" that would only inflate the vote count); when *stats* is given,
        ``stats["skipped_hashes"]`` reports how many were skipped.
        """

    # ------------------------------------------------------------------ stats
    @abstractmethod
    def get_stats(self) -> dict[str, Any]:
        """Cheap statistics (never scans the fingerprint table)."""

    @abstractmethod
    def clear(self) -> None:
        """Delete every track and fingerprint (keeps meta)."""

    # ------------------------------------------------------------------ helpers
    def require_track(self, track_id: str) -> TrackRecord:
        record = self.get_track(track_id)
        if record is None:
            raise NotFoundError(f"Track '{track_id}' not found", details={"track_id": track_id})
        return record

    @staticmethod
    def _sort_key(sort: str, order: str) -> tuple[str, bool]:
        if sort not in SORTABLE_FIELDS:
            raise ValidationError(f"sort must be one of {', '.join(SORTABLE_FIELDS)}")
        if order not in ("asc", "desc"):
            raise ValidationError("order must be 'asc' or 'desc'")
        return sort, order == "desc"

    @staticmethod
    def _empty_hash_result() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
