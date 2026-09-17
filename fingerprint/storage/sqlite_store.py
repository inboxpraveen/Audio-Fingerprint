"""SQLite storage backend, the default for single-node deployments.

Design notes
------------
* One connection per thread (``threading.local``; connections of threads that
  have exited are closed lazily) and WAL journal mode. A process-wide write
  lock makes concurrent indexing workers queue and keeps them from fighting
  over ``SQLITE_BUSY``.
* ``fingerprints`` is a ``WITHOUT ROWID`` table whose primary key is
  ``(hash_value, track_ref, time_offset)``: a single clustered B-tree that is
  both the storage and the lookup index. There is no secondary index to keep,
  and it takes half the disk of the v1 layout.
* Writes are batched. Inserting one track's hashes touches a random leaf page
  per hash, so on a large library every track would rewrite hundreds of MB of
  B-tree. Fingerprints are therefore buffered in memory (``write_batch_rows``)
  and written in one sorted transaction, which visits each leaf once per
  batch. The buffer is searched too, so a track is matchable the moment it is
  added. ``flush()`` runs when the buffer fills, at the end of an indexing run
  and on ``close()``. A track row carries ``flushed=0`` until its hashes are
  on disk. After an unclean shutdown such tracks are removed at start-up and
  logged, so they get re-indexed next time. The library never holds a track
  that can't be matched.
* Batch lookups load the query hashes into a temporary table and join, which
  is one statement whatever the query size. Hashes that occur more than
  ``max_rows_per_hash`` times in the library ("stop words": hold-music loops,
  test tones) are skipped. They carry almost no information but would
  multiply the vote count.
* ``get_stats`` only touches the small ``tracks`` table and never scans the
  fingerprint table, so the UI can poll it freely.
* The schema is versioned with ``PRAGMA user_version``. A v1 database (from
  AudioFP 1.x) is detected and rejected with a clear message because the hash
  layout changed.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

import numpy as np

from ..utils.exceptions import FingerprintCompatibilityError, StorageError
from .base import StorageBackend, TrackRecord, normalize_tags, validate_track_changes

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
INSERT_SLICE = 200_000  # rows converted to Python tuples at a time (bounds transient memory)

_TRACK_COLUMNS = (
    "ref",
    "track_id",
    "title",
    "artist",
    "filename",
    "filepath",
    "content_hash",
    "duration",
    "num_peaks",
    "num_hashes",
    "source_type",
    "file_size",
    "indexed_at",
    "tags",
    "metadata",
)
_SELECT_TRACK = f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks"


class SQLiteStore(StorageBackend):
    backend_name = "sqlite"

    _SCHEMA_V2 = (
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tracks (
            ref          INTEGER PRIMARY KEY,
            track_id     TEXT NOT NULL UNIQUE,
            title        TEXT NOT NULL DEFAULT '',
            artist       TEXT NOT NULL DEFAULT '',
            filename     TEXT NOT NULL DEFAULT '',
            filepath     TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            duration     REAL NOT NULL DEFAULT 0,
            num_peaks    INTEGER NOT NULL DEFAULT 0,
            num_hashes   INTEGER NOT NULL DEFAULT 0,
            source_type  TEXT NOT NULL DEFAULT 'audio',
            file_size    INTEGER NOT NULL DEFAULT 0,
            indexed_at   REAL NOT NULL,
            tags         TEXT NOT NULL DEFAULT '[]',
            metadata     TEXT NOT NULL DEFAULT '{}'
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_tracks_content_hash ON tracks (content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_filepath ON tracks (filepath)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_indexed_at ON tracks (indexed_at)",
        """
        CREATE TABLE IF NOT EXISTS fingerprints (
            hash_value  INTEGER NOT NULL,
            track_ref   INTEGER NOT NULL,
            time_offset INTEGER NOT NULL,
            PRIMARY KEY (hash_value, track_ref, time_offset)
        ) WITHOUT ROWID
        """,
    )
    # v3: a per-track flag telling whether its fingerprints have reached the disk (batched writes).
    _SCHEMA_V3 = ("ALTER TABLE tracks ADD COLUMN flushed INTEGER NOT NULL DEFAULT 1",)

    def __init__(
        self,
        db_path: str,
        cache_mb: int = 64,
        mmap_mb: int = 256,
        timeout: float = 30.0,
        write_batch_rows: int = 2_000_000,
        track_index: bool = False,
    ):
        if db_path == ":memory:":
            raise StorageError("SQLite ':memory:' databases are per-connection and cannot be shared between threads; use AUDIOFP_STORAGE_TYPE=memory instead.")
        self.db_path = db_path
        self.cache_mb = int(cache_mb)
        self.mmap_mb = int(mmap_mb)
        self.timeout = float(timeout)
        self.write_batch_rows = max(0, int(write_batch_rows))
        self.track_index = bool(track_index)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        # (owning thread, connection) pairs; connections of threads that have exited are closed lazily
        self._connections: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._connections_lock = threading.Lock()
        # Write buffer: (track ref, hashes, times) per track not yet on disk.
        self._pending: list[tuple[int, np.ndarray, np.ndarray]] = []
        self._pending_rows = 0
        self._pending_lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ connections
    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=self.timeout, isolation_level=None)
        except sqlite3.Error as exc:
            raise StorageError(f"Could not open SQLite database '{self.db_path}': {exc}") from exc
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA cache_size=-{max(self.cache_mb, 1) * 1024}")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(f"PRAGMA mmap_size={max(self.mmap_mb, 0) * 1024 * 1024}")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys=ON")
        with self._connections_lock:
            self._prune_dead_connections()
            self._connections.append((threading.current_thread(), conn))
        return conn

    def _prune_dead_connections(self) -> None:
        """Close connections whose owning thread has exited (call with _connections_lock held)."""
        alive: list[tuple[threading.Thread, sqlite3.Connection]] = []
        for thread, conn in self._connections:
            if thread.is_alive():
                alive.append((thread, conn))
            else:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover (defensive)
                    pass
        self._connections = alive

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        try:
            self.flush()
        except StorageError as exc:  # pragma: no cover (disk problems at shutdown)
            logger.error("Could not flush pending fingerprints on close: %s", exc)
        with self._connections_lock:
            conns, self._connections = self._connections, []
        for _thread, conn in conns:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover (defensive)
                pass
        self._local = threading.local()

    @property
    def open_connections(self) -> int:
        with self._connections_lock:
            self._prune_dead_connections()
            return len(self._connections)

    @property
    def pending_rows(self) -> int:
        with self._pending_lock:
            return self._pending_rows

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        conn = self._conn()
        with self._write_lock:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and "songs" in tables and "meta" not in tables:
                raise FingerprintCompatibilityError(
                    f"'{self.db_path}' was created by AudioFP 1.x. Its fingerprints use an incompatible hash "
                    "layout and cannot be upgraded in place. Point AUDIOFP_SQLITE_PATH at a new file (or delete "
                    "the old one) and re-index your library.",
                    details={"db_path": self.db_path, "schema_version": int(version)},
                )
            if version > SCHEMA_VERSION:
                raise StorageError(f"'{self.db_path}' uses schema version {version}, newer than this AudioFP supports ({SCHEMA_VERSION}). Upgrade AudioFP.")
            if version < SCHEMA_VERSION:
                self._migrate(conn, int(version))
            if self.track_index:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints (track_ref)")
            self._repair_unflushed(conn)

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """Apply schema steps from *from_version* up to :data:`SCHEMA_VERSION` in one transaction."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            if from_version < 2:
                for statement in self._SCHEMA_V2:
                    conn.execute(statement)
            if from_version < 3:  # also runs for fresh databases: v2 created `tracks` without the column
                for statement in self._SCHEMA_V3:
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover (already rolled back)
                pass
            raise StorageError(f"Schema migration failed: {exc}") from exc
        logger.info("SQLite schema initialised (version %d) at %s", SCHEMA_VERSION, self.db_path)

    def _repair_unflushed(self, conn: sqlite3.Connection) -> None:
        """Remove tracks whose fingerprints never reached the disk (process died before a flush)."""
        rows = conn.execute("SELECT ref, filename FROM tracks WHERE flushed=0").fetchall()
        if not rows:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            for ref, _name in rows:
                conn.execute("DELETE FROM fingerprints WHERE track_ref=?", (ref,))
            conn.execute("DELETE FROM tracks WHERE flushed=0")
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise StorageError(f"Could not repair unflushed tracks: {exc}") from exc
        names = ", ".join(name for _ref, name in rows[:5])
        logger.warning(
            "Removed %d track(s) whose fingerprints were lost in an unclean shutdown (%s%s); re-index them.",
            len(rows),
            names,
            " ..." if len(rows) > 5 else "",
        )

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._write_lock:
            self._conn().execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------ tracks
    @staticmethod
    def _row_to_record(row: sqlite3.Row | tuple) -> TrackRecord:
        data = dict(zip(_TRACK_COLUMNS, row))
        data["tags"] = json.loads(data.get("tags") or "[]")
        data["metadata"] = json.loads(data.get("metadata") or "{}")
        return TrackRecord(**data)

    def add_track(self, record: TrackRecord, hashes: np.ndarray, times: np.ndarray) -> TrackRecord:
        hashes = np.ascontiguousarray(hashes, dtype=np.int64)
        times = np.ascontiguousarray(times, dtype=np.int64)
        if hashes.shape != times.shape:
            raise StorageError("hashes and times must have the same length")
        record.tags = normalize_tags(record.tags)

        conn = self._conn()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT ref FROM tracks WHERE track_id=?", (record.track_id,)).fetchone()
                if existing:
                    self._drop_pending(int(existing[0]))
                    conn.execute("DELETE FROM fingerprints WHERE track_ref=?", (existing[0],))
                    conn.execute("DELETE FROM tracks WHERE ref=?", (existing[0],))
                cur = conn.execute(
                    """
                    INSERT INTO tracks (track_id, title, artist, filename, filepath, content_hash, duration,
                                        num_peaks, num_hashes, source_type, file_size, indexed_at, tags, metadata, flushed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.track_id,
                        record.title,
                        record.artist,
                        record.filename,
                        record.filepath,
                        record.content_hash,
                        float(record.duration),
                        int(record.num_peaks),
                        int(hashes.size),
                        record.source_type,
                        int(record.file_size),
                        float(record.indexed_at),
                        json.dumps(record.tags),
                        json.dumps(record.metadata, default=str),
                        0 if (self.write_batch_rows and hashes.size) else 1,
                    ),
                )
                ref = int(cur.lastrowid)
                if hashes.size and not self.write_batch_rows:
                    self._insert_rows(conn, hashes, np.full(hashes.size, ref, dtype=np.int64), times)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(f"Failed to store track '{record.filename}': {exc}") from exc

        record.ref = ref
        record.num_hashes = int(hashes.size)
        if hashes.size and self.write_batch_rows:
            with self._pending_lock:
                self._pending.append((ref, hashes, times))
                self._pending_rows += int(hashes.size)
                full = self._pending_rows >= self.write_batch_rows
            if full:
                self.flush()
        return record

    # ------------------------------------------------------------------ batched writes
    def _drop_pending(self, ref: int) -> None:
        with self._pending_lock:
            kept = [item for item in self._pending if item[0] != ref]
            if len(kept) != len(self._pending):
                self._pending = kept
                self._pending_rows = int(sum(h.size for _r, h, _t in kept))

    @staticmethod
    def _insert_rows(conn: sqlite3.Connection, hashes: np.ndarray, refs: np.ndarray, times: np.ndarray) -> None:
        """Insert rows in key order, converting to Python objects one slice at a time."""
        order = np.lexsort((times, refs, hashes))
        hashes, refs, times = hashes[order], refs[order], times[order]
        for start in range(0, hashes.size, INSERT_SLICE):
            stop = start + INSERT_SLICE
            conn.executemany(
                "INSERT OR IGNORE INTO fingerprints (hash_value, track_ref, time_offset) VALUES (?, ?, ?)",
                zip(hashes[start:stop].tolist(), refs[start:stop].tolist(), times[start:stop].tolist()),
            )

    def flush(self) -> int:
        """Write buffered fingerprints to disk in one sorted transaction. Returns rows written."""
        with self._write_lock:
            with self._pending_lock:
                pending, self._pending, self._pending_rows = self._pending, [], 0
            if not pending:
                return 0
            hashes = np.concatenate([h for _r, h, _t in pending])
            refs = np.concatenate([np.full(h.size, r, dtype=np.int64) for r, h, _t in pending])
            times = np.concatenate([t for _r, _h, t in pending])
            conn = self._conn()
            started = time.time()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._insert_rows(conn, hashes, refs, times)
                for start in range(0, len(pending), 500):
                    chunk = [r for r, _h, _t in pending[start : start + 500]]
                    conn.execute(f"UPDATE tracks SET flushed=1 WHERE ref IN ({','.join('?' * len(chunk))})", chunk)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                with self._pending_lock:  # keep the rows for a retry
                    self._pending = pending + self._pending
                    self._pending_rows += int(hashes.size)
                raise StorageError(f"Failed to write {hashes.size} fingerprints: {exc}") from exc
            logger.info("Flushed %d fingerprints for %d track(s) in %.1fs", hashes.size, len(pending), time.time() - started)
            return int(hashes.size)

    def get_track(self, track_id: str) -> TrackRecord | None:
        row = self._conn().execute(f"{_SELECT_TRACK} WHERE track_id=?", (track_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_tracks_by_ref(self, refs: Iterable[int]) -> dict[int, TrackRecord]:
        refs = [int(r) for r in refs]
        if not refs:
            return {}
        out: dict[int, TrackRecord] = {}
        conn = self._conn()
        for i in range(0, len(refs), 500):
            chunk = refs[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(f"{_SELECT_TRACK} WHERE ref IN ({placeholders})", chunk):
                rec = self._row_to_record(row)
                out[int(rec.ref)] = rec
        return out

    def find_by_content_hash(self, content_hash: str) -> TrackRecord | None:
        if not content_hash:
            return None
        row = self._conn().execute(f"{_SELECT_TRACK} WHERE content_hash=? ORDER BY indexed_at LIMIT 1", (content_hash,)).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_filepath(self, filepath: str) -> TrackRecord | None:
        if not filepath:
            return None
        row = self._conn().execute(f"{_SELECT_TRACK} WHERE filepath=? ORDER BY indexed_at LIMIT 1", (filepath,)).fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _like(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{escaped}%"

    def list_tracks(self, *, query=None, sort="indexed_at", order="desc", offset=0, limit=50, source_type=None, tag=None):
        sort_field, desc = self._sort_key(sort, order)
        where: list[str] = []
        params: list[Any] = []
        if query:
            like = self._like(query.strip())
            where.append("(title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')")
            params += [like, like, like, like]
        if source_type:
            where.append("source_type=?")
            params.append(source_type)
        if tag:
            where.append("tags LIKE ? ESCAPE '\\'")
            params.append(self._like(json.dumps(tag.strip().lower())))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        collate = " COLLATE NOCASE" if sort_field in ("title", "artist", "filename") else ""
        direction = "DESC" if desc else "ASC"
        conn = self._conn()
        total = conn.execute(f"SELECT COUNT(*) FROM tracks{clause}", params).fetchone()[0]
        sql = f"{_SELECT_TRACK}{clause} ORDER BY {sort_field}{collate} {direction}, ref {direction}"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params = params + [int(limit), int(offset)]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params = params + [int(offset)]
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows], int(total)

    def update_track(self, track_id: str, changes: dict[str, Any]) -> TrackRecord:
        clean = validate_track_changes(changes)
        with self._write_lock:
            self.require_track(track_id)
            if clean:
                sets, params = [], []
                for key, value in clean.items():
                    sets.append(f"{key}=?")
                    params.append(json.dumps(value) if key in ("tags", "metadata") else value)
                params.append(track_id)
                self._conn().execute(f"UPDATE tracks SET {', '.join(sets)} WHERE track_id=?", params)
        return self.require_track(track_id)

    def delete_track(self, track_id: str) -> bool:
        return self.delete_tracks([track_id]) == 1

    def delete_tracks(self, track_ids: Iterable[str]) -> int:
        ids = list(track_ids)
        if not ids:
            return 0
        conn = self._conn()
        with self._write_lock:
            refs: list[int] = []
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                refs += [int(r[0]) for r in conn.execute(f"SELECT ref FROM tracks WHERE track_id IN ({','.join('?' * len(chunk))})", chunk)]
            if not refs:
                return 0
            for ref in refs:
                self._drop_pending(ref)
            try:
                conn.execute("BEGIN IMMEDIATE")
                # delete in batches so the fingerprint table isn't scanned once per track
                for i in range(0, len(refs), 500):
                    chunk = refs[i : i + 500]
                    placeholders = ",".join("?" * len(chunk))
                    conn.execute(f"DELETE FROM fingerprints WHERE track_ref IN ({placeholders})", chunk)
                    conn.execute(f"DELETE FROM tracks WHERE ref IN ({placeholders})", chunk)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(f"Delete failed: {exc}") from exc
        return len(refs)

    def count_tracks(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM tracks").fetchone()[0])

    # ------------------------------------------------------------------ hashes
    def query_hashes(self, hash_values: np.ndarray, max_rows_per_hash: int | None = None, stats: dict | None = None):
        wanted = np.unique(np.asarray(hash_values, dtype=np.int64))
        if wanted.size == 0:
            return self._empty_hash_result()
        conn = self._conn()
        skipped = 0
        try:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS query_hashes (hash_value INTEGER PRIMARY KEY)")
            # One transaction for the fill: in autocommit mode every row would be its own commit.
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM query_hashes")
                conn.executemany("INSERT OR IGNORE INTO query_hashes (hash_value) VALUES (?)", ((int(h),) for h in wanted))
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            if max_rows_per_hash:
                heavy = conn.execute(
                    """
                    SELECT q.hash_value FROM query_hashes AS q CROSS JOIN fingerprints AS f ON f.hash_value = q.hash_value
                    GROUP BY q.hash_value HAVING COUNT(*) > ?
                    """,
                    (int(max_rows_per_hash),),
                ).fetchall()
                if heavy:
                    skipped = len(heavy)
                    conn.executemany("DELETE FROM query_hashes WHERE hash_value=?", heavy)
            rows = conn.execute(
                """
                SELECT f.hash_value, f.track_ref, f.time_offset
                FROM query_hashes AS q CROSS JOIN fingerprints AS f ON f.hash_value = q.hash_value
                """
            ).fetchall()
            conn.execute("DELETE FROM query_hashes")
        except sqlite3.Error as exc:
            raise StorageError(f"Hash lookup failed: {exc}") from exc

        parts = [np.array(rows, dtype=np.int64).reshape(-1, 3)] if rows else []
        parts += self._pending_matches(wanted, max_rows_per_hash)
        if stats is not None:
            stats["skipped_hashes"] = skipped
        if not parts:
            return self._empty_hash_result()
        arr = np.concatenate(parts) if len(parts) > 1 else parts[0]
        return arr[:, 0], arr[:, 1], arr[:, 2]

    def _pending_matches(self, wanted: np.ndarray, max_rows_per_hash: int | None) -> list[np.ndarray]:
        """Rows from the not-yet-flushed buffer that match *wanted* (so fresh tracks are searchable)."""
        with self._pending_lock:
            snapshot = list(self._pending)
        parts: list[np.ndarray] = []
        for ref, hashes, times in snapshot:
            mask = np.isin(hashes, wanted, assume_unique=False)
            if mask.any():
                h = hashes[mask]
                parts.append(np.column_stack([h, np.full(h.size, ref, dtype=np.int64), times[mask]]))
        if parts and max_rows_per_hash:
            merged = np.concatenate(parts)
            _u, inverse, counts = np.unique(merged[:, 0], return_inverse=True, return_counts=True)
            merged = merged[counts[inverse] <= max_rows_per_hash]
            return [merged] if merged.size else []
        return parts

    # ------------------------------------------------------------------ stats
    def get_stats(self) -> dict[str, Any]:
        row = self._conn().execute("SELECT COUNT(*), COALESCE(SUM(num_hashes), 0), COALESCE(SUM(duration), 0) FROM tracks").fetchone()
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(self.db_path + suffix)
            except OSError:
                pass
        return {
            "storage_type": self.backend_name,
            "total_tracks": int(row[0]),
            "total_hashes": int(row[1]),
            "total_duration_sec": round(float(row[2]), 2),
            "db_path": self.db_path,
            "db_size_bytes": size,
            "pending_rows": self.pending_rows,
            "persistent": True,
        }

    def unique_hash_count(self) -> int:
        """Expensive (full index scan). Only ``audiofp stats --full`` uses it."""
        self.flush()
        return int(self._conn().execute("SELECT COUNT(DISTINCT hash_value) FROM fingerprints").fetchone()[0])

    def clear(self) -> None:
        conn = self._conn()
        with self._write_lock:
            with self._pending_lock:
                self._pending, self._pending_rows = [], 0
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM fingerprints")
                conn.execute("DELETE FROM tracks")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(f"Clear failed: {exc}") from exc

    def vacuum(self) -> None:
        self.flush()
        with self._write_lock:
            self._conn().execute("VACUUM")

    def checkpoint(self) -> None:
        """Fold the WAL back into the main database file."""
        self.flush()
        with self._write_lock:
            self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def __repr__(self) -> str:  # pragma: no cover
        return f"SQLiteStore({self.db_path!r})"


def sqlite_runtime_info() -> dict[str, Any]:
    return {"sqlite_version": sqlite3.sqlite_version, "threadsafety": sqlite3.threadsafety, "time": time.time()}
