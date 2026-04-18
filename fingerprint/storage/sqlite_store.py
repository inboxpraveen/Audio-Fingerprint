"""SQLite storage backend - WAL mode, thread-local connections, batch queries."""

import sqlite3
import json
import threading
from .base import StorageBackend


class SQLiteStore(StorageBackend):
    """
    SQLite storage backend with:
    - WAL journal mode for concurrent reads during writes
    - Thread-local connections (one connection per thread, reused)
    - Batch hash lookup (single SQL query vs N queries - huge speedup)
    - Covering index on (hash_value, song_id, time_offset)
    - Large in-memory page cache and mmap for fast reads
    """

    # SQLite variable limit is 999; keep chunks well below that
    _CHUNK_SIZE = 900

    def __init__(self, db_path: str = "fingerprint.db"):
        self.db_path = db_path
        self._local = threading.local()
        self._init_database()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return (or create) a thread-local SQLite connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30,
            )
            # Performance tuning
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-65536")   # 64 MB page cache
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456") # 256 MB memory-mapped I/O
            conn.execute("PRAGMA page_size=4096")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Schema setup
    # ------------------------------------------------------------------

    def _init_database(self):
        """Create tables and indices if they do not exist."""
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                song_id   TEXT PRIMARY KEY,
                title     TEXT,
                artist    TEXT,
                filepath  TEXT,
                duration  REAL,
                metadata  TEXT,
                indexed_at REAL DEFAULT (strftime('%s', 'now'))
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fingerprints (
                hash_value  INTEGER NOT NULL,
                song_id     TEXT    NOT NULL,
                time_offset INTEGER NOT NULL
            )
        """)

        # Primary lookup index
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hash
            ON fingerprints (hash_value)
        """)

        # Covering index so lookups never touch the table heap
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_hash_cover
            ON fingerprints (hash_value, song_id, time_offset)
        """)

        conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def store_fingerprint(self, song_id: str, song_metadata: dict, hashes: list):
        """
        Persist a song and all of its fingerprint hashes.

        hashes: list of (hash_value, time_offset, song_id) tuples
        """
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT OR REPLACE INTO songs
                (song_id, title, artist, filepath, duration, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                song_id,
                song_metadata.get("title", ""),
                song_metadata.get("artist", ""),
                song_metadata.get("filepath", ""),
                song_metadata.get("duration", 0.0),
                json.dumps(song_metadata),
            ),
        )

        cur.executemany(
            "INSERT INTO fingerprints (hash_value, song_id, time_offset) VALUES (?, ?, ?)",
            [(hv, song_id, t_off) for hv, t_off, _ in hashes],
        )

        conn.commit()

    # ------------------------------------------------------------------
    # Read - single hash (kept for compatibility with base interface)
    # ------------------------------------------------------------------

    def query_hash(self, hash_value: int) -> list:
        cur = self._get_conn().cursor()
        cur.execute(
            "SELECT song_id, time_offset FROM fingerprints WHERE hash_value = ?",
            (hash_value,),
        )
        return cur.fetchall()

    # ------------------------------------------------------------------
    # Read - batch hash lookup (primary search path)
    # ------------------------------------------------------------------

    def query_hashes_batch(self, hash_values: list) -> list:
        """
        Look up all hashes in a single round-trip (chunked to respect
        SQLite's per-query variable limit of 999).

        Returns: list of (hash_value, song_id, time_offset)
        """
        if not hash_values:
            return []

        conn = self._get_conn()
        cur = conn.cursor()
        results = []

        for i in range(0, len(hash_values), self._CHUNK_SIZE):
            chunk = hash_values[i : i + self._CHUNK_SIZE]
            placeholders = ",".join("?" * len(chunk))
            cur.execute(
                f"SELECT hash_value, song_id, time_offset "
                f"FROM fingerprints WHERE hash_value IN ({placeholders})",
                chunk,
            )
            results.extend(cur.fetchall())

        return results

    # ------------------------------------------------------------------
    # Song metadata
    # ------------------------------------------------------------------

    def get_song_metadata(self, song_id: str):
        cur = self._get_conn().cursor()
        cur.execute("SELECT metadata FROM songs WHERE song_id = ?", (song_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    def get_all_songs(self) -> list:
        cur = self._get_conn().cursor()
        cur.execute("SELECT metadata FROM songs ORDER BY indexed_at DESC")
        return [json.loads(row[0]) for row in cur.fetchall()]

    def song_exists(self, filepath: str) -> bool:
        """Return True if a song with this filepath is already indexed."""
        cur = self._get_conn().cursor()
        cur.execute("SELECT 1 FROM songs WHERE filepath = ? LIMIT 1", (filepath,))
        return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_song(self, song_id: str):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM fingerprints WHERE song_id = ?", (song_id,))
        cur.execute("DELETE FROM songs WHERE song_id = ?", (song_id,))
        conn.commit()

    # ------------------------------------------------------------------
    # Stats & maintenance
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        cur = self._get_conn().cursor()

        cur.execute("SELECT COUNT(*) FROM songs")
        total_songs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM fingerprints")
        total_hashes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT hash_value) FROM fingerprints")
        unique_hashes = cur.fetchone()[0]

        return {
            "total_songs": total_songs,
            "total_hashes": total_hashes,
            "unique_hashes": unique_hashes,
            "storage_type": "sqlite",
            "db_path": self.db_path,
        }

    def clear(self):
        conn = self._get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM fingerprints")
        cur.execute("DELETE FROM songs")
        conn.commit()

    def close(self):
        """Close the current thread's connection (useful in tests / shutdown)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
