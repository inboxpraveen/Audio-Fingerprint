"""PostgreSQL storage backend for multi-process / multi-node deployments.

Requires the optional dependency ``pip install "audiofp[postgres]"``
(``psycopg[binary,pool]`` - psycopg 3).

* Connections come from a :class:`psycopg_pool.ConnectionPool`.
* Fingerprints are bulk-loaded with ``COPY`` and looked up with
  ``WHERE hash_value = ANY(%s)`` - one round trip per query.
* The covering index ``(hash_value) INCLUDE (track_ref, time_offset)`` serves
  lookups straight from the index; ``ON DELETE CASCADE`` keeps deletes cheap.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

import numpy as np

from ..utils.exceptions import StorageError
from .base import StorageBackend, TrackRecord, normalize_tags, validate_track_changes

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    ref          SERIAL PRIMARY KEY,
    track_id     TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    artist       TEXT NOT NULL DEFAULT '',
    filename     TEXT NOT NULL DEFAULT '',
    filepath     TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    duration     DOUBLE PRECISION NOT NULL DEFAULT 0,
    num_peaks    INTEGER NOT NULL DEFAULT 0,
    num_hashes   INTEGER NOT NULL DEFAULT 0,
    source_type  TEXT NOT NULL DEFAULT 'audio',
    file_size    BIGINT NOT NULL DEFAULT 0,
    indexed_at   DOUBLE PRECISION NOT NULL,
    tags         JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_tracks_content_hash ON tracks (content_hash);
CREATE INDEX IF NOT EXISTS idx_tracks_filepath ON tracks (filepath);
CREATE INDEX IF NOT EXISTS idx_tracks_indexed_at ON tracks (indexed_at);
CREATE TABLE IF NOT EXISTS fingerprints (
    hash_value  BIGINT  NOT NULL,
    track_ref   INTEGER NOT NULL REFERENCES tracks(ref) ON DELETE CASCADE,
    time_offset INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fp_hash ON fingerprints (hash_value) INCLUDE (track_ref, time_offset);
CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints (track_ref);
"""


def _redact(dsn: str) -> str:
    """Hide the password in a connection string for log/error messages."""
    import re

    return re.sub(r"(://[^:/@]+:)[^@]*@", lambda m: m.group(1) + "***@", dsn)


class PostgresStore(StorageBackend):
    backend_name = "postgres"

    def __init__(self, dsn: str, pool_size: int = 4, connect_timeout: float = 15.0):
        try:
            import psycopg  # noqa: F401
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise StorageError(
                "PostgreSQL support needs the optional dependency: pip install 'psycopg[binary,pool]' (or pip install 'audiofp[postgres]')"
            ) from exc
        self.dsn = dsn
        try:
            self._pool = ConnectionPool(dsn, min_size=1, max_size=max(1, int(pool_size)), open=True, timeout=connect_timeout)
            # Fail fast with a readable error instead of a pool timeout deep inside the first query.
            self._pool.wait(timeout=connect_timeout)
        except Exception as exc:
            try:
                self._pool.close(timeout=1)
            except Exception:  # pragma: no cover - best effort
                pass
            raise StorageError(
                f"Could not connect to PostgreSQL ({_redact(dsn)}): {exc}. Check AUDIOFP_POSTGRES_DSN, that the server is reachable, and that the database exists."
            ) from exc
        try:
            self._init_schema()
        except Exception as exc:
            self.close()
            raise StorageError(f"Could not initialise the PostgreSQL schema: {exc}") from exc

    # ------------------------------------------------------------------ helpers
    def _init_schema(self) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)
                cur.execute("INSERT INTO meta (key, value) VALUES ('schema_version', %s) ON CONFLICT (key) DO NOTHING", (str(SCHEMA_VERSION),))
            conn.commit()
        logger.info("PostgreSQL schema ready (version %d)", SCHEMA_VERSION)

    @staticmethod
    def _row_to_record(row: tuple) -> TrackRecord:
        data = dict(zip(_TRACK_COLUMNS, row))
        if isinstance(data.get("tags"), str):
            data["tags"] = json.loads(data["tags"])
        if isinstance(data.get("metadata"), str):
            data["metadata"] = json.loads(data["metadata"])
        data["tags"] = data.get("tags") or []
        data["metadata"] = data.get("metadata") or {}
        return TrackRecord(**data)

    def close(self) -> None:
        try:
            self._pool.close(timeout=5)
        except Exception:  # pragma: no cover - defensive
            pass

    def health_check(self) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))
            conn.commit()

    # ------------------------------------------------------------------ tracks
    def add_track(self, record: TrackRecord, hashes: np.ndarray, times: np.ndarray) -> TrackRecord:
        from psycopg.types.json import Jsonb

        hashes = np.asarray(hashes, dtype=np.int64)
        times = np.asarray(times, dtype=np.int64)
        record.tags = normalize_tags(record.tags)
        try:
            with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM tracks WHERE track_id=%s", (record.track_id,))
                cur.execute(
                    """
                    INSERT INTO tracks (track_id, title, artist, filename, filepath, content_hash, duration,
                                        num_peaks, num_hashes, source_type, file_size, indexed_at, tags, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING ref
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
                        Jsonb(record.tags),
                        Jsonb(record.metadata),
                    ),
                )
                ref = int(cur.fetchone()[0])
                if hashes.size:
                    with cur.copy("COPY fingerprints (hash_value, track_ref, time_offset) FROM STDIN") as copy:
                        # Text COPY in large chunks instead of one write_row() call per row.
                        for start in range(0, hashes.size, 200_000):
                            block = "\n".join(
                                f"{h}\t{ref}\t{t}" for h, t in zip(hashes[start : start + 200_000].tolist(), times[start : start + 200_000].tolist())
                            )
                            copy.write(block + "\n")
        except Exception as exc:
            raise StorageError(f"Failed to store track '{record.filename}': {exc}") from exc
        record.ref = ref
        record.num_hashes = int(hashes.size)
        return record

    def get_track(self, track_id: str) -> TrackRecord | None:
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks WHERE track_id=%s", (track_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_tracks_by_ref(self, refs: Iterable[int]) -> dict[int, TrackRecord]:
        refs = [int(r) for r in refs]
        if not refs:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks WHERE ref = ANY(%s)", (refs,)).fetchall()
        out = {}
        for row in rows:
            rec = self._row_to_record(row)
            out[int(rec.ref)] = rec
        return out

    def find_by_content_hash(self, content_hash: str) -> TrackRecord | None:
        if not content_hash:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks WHERE content_hash=%s ORDER BY indexed_at LIMIT 1", (content_hash,)).fetchone()
        return self._row_to_record(row) if row else None

    def find_by_filepath(self, filepath: str) -> TrackRecord | None:
        if not filepath:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks WHERE filepath=%s ORDER BY indexed_at LIMIT 1", (filepath,)).fetchone()
        return self._row_to_record(row) if row else None

    def list_tracks(self, *, query=None, sort="indexed_at", order="desc", offset=0, limit=50, source_type=None, tag=None):
        sort_field, desc = self._sort_key(sort, order)
        where: list[str] = []
        params: list[Any] = []
        if query:
            escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            where.append("(title ILIKE %s ESCAPE '\\' OR artist ILIKE %s ESCAPE '\\' OR filename ILIKE %s ESCAPE '\\' OR tags::text ILIKE %s ESCAPE '\\')")
            params += [like, like, like, like]
        if source_type:
            where.append("source_type=%s")
            params.append(source_type)
        if tag:
            where.append("tags ? %s")
            params.append(tag.strip().lower())
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        direction = "DESC" if desc else "ASC"
        order_expr = f"LOWER({sort_field})" if sort_field in ("title", "artist", "filename") else sort_field
        with self._pool.connection() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM tracks{clause}", params).fetchone()[0]
            sql = f"SELECT {', '.join(_TRACK_COLUMNS)} FROM tracks{clause} ORDER BY {order_expr} {direction}, ref {direction}"
            if limit:
                sql += " LIMIT %s OFFSET %s"
                params = params + [int(limit), int(offset)]
            elif offset:
                sql += " OFFSET %s"
                params = params + [int(offset)]
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows], int(total)

    def update_track(self, track_id: str, changes: dict[str, Any]) -> TrackRecord:
        from psycopg.types.json import Jsonb

        clean = validate_track_changes(changes)
        self.require_track(track_id)
        if clean:
            sets, params = [], []
            for key, value in clean.items():
                sets.append(f"{key}=%s")
                params.append(Jsonb(value) if key in ("tags", "metadata") else value)
            params.append(track_id)
            with self._pool.connection() as conn:
                conn.execute(f"UPDATE tracks SET {', '.join(sets)} WHERE track_id=%s", params)
                conn.commit()
        return self.require_track(track_id)

    def delete_track(self, track_id: str) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM tracks WHERE track_id=%s", (track_id,))
            conn.commit()
            return cur.rowcount > 0

    def delete_tracks(self, track_ids: Iterable[str]) -> int:
        ids = list(track_ids)
        if not ids:
            return 0
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM tracks WHERE track_id = ANY(%s)", (ids,))
            conn.commit()
            return int(cur.rowcount)

    def count_tracks(self) -> int:
        with self._pool.connection() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])

    # ------------------------------------------------------------------ hashes
    def query_hashes(self, hash_values: np.ndarray, max_rows_per_hash: int | None = None, stats: dict | None = None):
        wanted = np.unique(np.asarray(hash_values, dtype=np.int64)).tolist()
        if stats is not None:
            stats["skipped_hashes"] = 0
        if not wanted:
            return self._empty_hash_result()
        try:
            with self._pool.connection() as conn:
                if max_rows_per_hash:
                    heavy = conn.execute(
                        "SELECT hash_value FROM fingerprints WHERE hash_value = ANY(%s) GROUP BY hash_value HAVING COUNT(*) > %s",
                        (wanted, int(max_rows_per_hash)),
                    ).fetchall()
                    if heavy:
                        heavy_set = {row[0] for row in heavy}
                        wanted = [h for h in wanted if h not in heavy_set]
                        if stats is not None:
                            stats["skipped_hashes"] = len(heavy_set)
                        if not wanted:
                            return self._empty_hash_result()
                rows = conn.execute("SELECT hash_value, track_ref, time_offset FROM fingerprints WHERE hash_value = ANY(%s)", (wanted,)).fetchall()
        except Exception as exc:
            raise StorageError(f"Hash lookup failed: {exc}") from exc
        if not rows:
            return self._empty_hash_result()
        arr = np.array(rows, dtype=np.int64)
        return arr[:, 0], arr[:, 1], arr[:, 2]

    # ------------------------------------------------------------------ stats
    def get_stats(self) -> dict[str, Any]:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(SUM(num_hashes), 0), COALESCE(SUM(duration), 0) FROM tracks").fetchone()
            size = conn.execute("SELECT pg_total_relation_size('fingerprints') + pg_total_relation_size('tracks')").fetchone()[0]
        return {
            "storage_type": self.backend_name,
            "total_tracks": int(row[0]),
            "total_hashes": int(row[1]),
            "total_duration_sec": round(float(row[2]), 2),
            "db_size_bytes": int(size or 0),
            "persistent": True,
        }

    def clear(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("TRUNCATE fingerprints, tracks RESTART IDENTITY")
            conn.commit()
