"""Contract tests every storage backend must pass: memory and SQLite, plus PostgreSQL when a DSN is set."""

from __future__ import annotations

import json
import os
import sqlite3
import threading

import numpy as np
import pytest

from fingerprint.config import Settings
from fingerprint.storage import MemoryStore, SQLiteStore, TrackRecord, create_storage, normalize_tags, validate_track_changes
from fingerprint.storage.base import META_SIGNATURE
from fingerprint.utils.exceptions import FingerprintCompatibilityError, NotFoundError, StorageError, ValidationError

POSTGRES_DSN = os.environ.get("AUDIOFP_TEST_POSTGRES_DSN")
BACKENDS = ["memory", "sqlite"] + (["postgres"] if POSTGRES_DSN else [])


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    if request.param == "memory":
        s = MemoryStore()
    elif request.param == "sqlite":
        s = SQLiteStore(str(tmp_path / "t.db"))
    else:  # pragma: no cover (needs a live server)
        from fingerprint.storage.postgres_store import PostgresStore

        s = PostgresStore(POSTGRES_DSN)
        s.clear()
    yield s
    if request.param == "postgres":  # pragma: no cover
        s.clear()
    s.close()


def _track(title="T", **kw) -> TrackRecord:
    return TrackRecord(title=title, artist=kw.pop("artist", "A"), filename=kw.pop("filename", f"{title}.wav"), **kw)


def _hashes(seed: int, n: int = 200):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 2**36, n, dtype=np.int64), rng.integers(0, 5000, n, dtype=np.int64)


def test_add_get_list_delete_roundtrip(store):
    h1, t1 = _hashes(1)
    rec = store.add_track(_track("One", tags=["Rock", "demo"], metadata={"k": 1}, duration=10.5, content_hash="abc", filepath="/x/one.wav"), h1, t1)
    assert rec.ref is not None and rec.num_hashes == 200
    got = store.get_track(rec.track_id)
    assert got is not None and got.title == "One" and got.tags == ["rock", "demo"] and got.metadata == {"k": 1}
    assert got.duration == 10.5 and got.content_hash == "abc"
    assert store.count_tracks() == 1
    assert store.find_by_content_hash("abc").track_id == rec.track_id
    assert store.find_by_filepath("/x/one.wav").track_id == rec.track_id
    assert store.find_by_content_hash("nope") is None and store.find_by_filepath("") is None
    assert store.get_tracks_by_ref([rec.ref, 999999]) == {rec.ref: store.get_track(rec.track_id)}

    h2, t2 = _hashes(2)
    rec2 = store.add_track(_track("Two", artist="Zed", duration=3), h2, t2)
    page, total = store.list_tracks(sort="title", order="asc")
    assert total == 2 and [p.title for p in page] == ["One", "Two"]
    page, total = store.list_tracks(query="zed")
    assert total == 1 and page[0].track_id == rec2.track_id
    page, total = store.list_tracks(query="ONE.WAV")
    assert total == 1 and page[0].track_id == rec.track_id
    page, total = store.list_tracks(tag="rock")
    assert total == 1 and page[0].track_id == rec.track_id
    page, total = store.list_tracks(sort="duration", order="desc", limit=1)
    assert total == 2 and page[0].title == "One"
    page, total = store.list_tracks(offset=1, limit=1, sort="title", order="asc")
    assert [p.title for p in page] == ["Two"]
    with pytest.raises(ValidationError):
        store.list_tracks(sort="hacker")

    assert store.delete_track(rec.track_id) is True
    assert store.delete_track(rec.track_id) is False
    assert store.get_track(rec.track_id) is None
    assert store.count_tracks() == 1
    stats = store.get_stats()
    assert stats["total_tracks"] == 1 and stats["total_hashes"] == 200


def test_query_hashes_returns_all_rows(store):
    h1, t1 = _hashes(10, 50)
    h2, t2 = _hashes(11, 50)
    shared = np.array([12345678, 12345678, 99], dtype=np.int64)
    h1 = np.concatenate([h1, shared])
    t1 = np.concatenate([t1, np.array([1, 2, 3], dtype=np.int64)])
    h2 = np.concatenate([h2, shared[:1]])
    t2 = np.concatenate([t2, np.array([7], dtype=np.int64)])
    r1 = store.add_track(_track("A"), h1, t1)
    r2 = store.add_track(_track("B"), h2, t2)

    hashes, refs, times = store.query_hashes(np.array([12345678, 99, 424242], dtype=np.int64))
    rows = sorted(zip(hashes.tolist(), refs.tolist(), times.tolist()))
    assert rows == [(99, r1.ref, 3), (12345678, r1.ref, 1), (12345678, r1.ref, 2), (12345678, r2.ref, 7)]
    assert all(a.dtype == np.int64 for a in (hashes, refs, times))
    empty = store.query_hashes(np.array([], dtype=np.int64))
    assert all(a.size == 0 for a in empty)
    empty = store.query_hashes(np.array([5], dtype=np.int64))
    assert all(a.size == 0 for a in empty)

    store.delete_track(r1.track_id)
    hashes, refs, times = store.query_hashes(np.array([12345678, 99], dtype=np.int64))
    assert refs.tolist() == [r2.ref]

    # large batches work (beyond SQLite's variable limit)
    big_h, big_t = _hashes(12, 5000)
    store.add_track(_track("Big"), big_h, big_t)
    hashes, refs, times = store.query_hashes(np.unique(big_h))
    assert hashes.size == np.unique(big_h).size


def test_replace_same_track_id_is_atomic(store):
    h1, t1 = _hashes(20, 30)
    rec = store.add_track(_track("Old"), h1, t1)
    h2, t2 = _hashes(21, 10)
    replaced = store.add_track(TrackRecord(track_id=rec.track_id, title="New"), h2, t2)
    assert replaced.track_id == rec.track_id and store.count_tracks() == 1
    assert store.get_track(rec.track_id).title == "New"
    hashes, _, _ = store.query_hashes(np.unique(np.concatenate([h1, h2])))
    assert hashes.size == np.unique(h2).size


def test_update_and_validation(store):
    rec = store.add_track(_track("U"), *_hashes(30, 5))
    updated = store.update_track(rec.track_id, {"title": "  New Title ", "tags": "A, b ,a", "metadata": {"agent": "x"}})
    assert updated.title == "New Title" and updated.tags == ["a", "b"] and updated.metadata == {"agent": "x"}
    with pytest.raises(ValidationError):
        store.update_track(rec.track_id, {"filepath": "/etc/passwd"})
    with pytest.raises(ValidationError):
        store.update_track(rec.track_id, {"metadata": "not a dict"})
    with pytest.raises(NotFoundError):
        store.update_track("missing", {"title": "x"})
    with pytest.raises(NotFoundError):
        store.require_track("missing")


def test_bulk_delete_and_clear(store):
    ids = [store.add_track(_track(f"T{i}"), *_hashes(40 + i, 5)).track_id for i in range(4)]
    assert store.delete_tracks(ids[:2] + ["missing"]) == 2
    assert store.count_tracks() == 2
    store.set_meta("custom", "value")
    store.clear()
    assert store.count_tracks() == 0 and store.get_meta("custom") == "value"
    assert store.health_check()


def test_meta_and_fingerprint_compat(store):
    assert store.get_meta(META_SIGNATURE) is None
    store.initialize("sig-a", {"fan_value": 10}, "strict")
    assert store.get_meta(META_SIGNATURE) == "sig-a"
    store.initialize("sig-a", {"fan_value": 10}, "strict")  # the same signature again is fine
    store.add_track(_track("X"), *_hashes(50, 5))
    with pytest.raises(FingerprintCompatibilityError) as exc:
        store.initialize("sig-b", {"fan_value": 11}, "strict")
    assert exc.value.details["stored_signature"] == "sig-a"
    store.initialize("sig-b", {"fan_value": 11}, "warn")  # only logs
    store.initialize("sig-b", {"fan_value": 11}, "ignore")
    assert store.get_meta(META_SIGNATURE) == "sig-a"  # initialize() never re-stamps by itself, that takes an explicit db reset


def test_helpers():
    assert normalize_tags(" A, b,,A ") == ["a", "b"]
    assert normalize_tags(["X", "x", " y "]) == ["x", "y"]
    assert normalize_tags(None) == []
    clean = validate_track_changes({"title": None, "artist": "Z"})
    assert clean == {"title": "", "artist": "Z"}
    with pytest.raises(ValidationError):
        validate_track_changes({"metadata": {"big": "x" * 70000}})
    rec = TrackRecord.from_dict({"track_id": "id", "title": "t", "unknown": 1})
    assert rec.track_id == "id" and rec.tags == [] and "unknown" not in rec.to_dict()
    assert "filepath" not in rec.to_dict(include_path=False)


# ---------------------------------------------------------------------------
# SQLite specifics
# ---------------------------------------------------------------------------


def test_sqlite_rejects_legacy_v1_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE songs (song_id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE fingerprints (hash_value INTEGER, song_id TEXT, time_offset INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(FingerprintCompatibilityError, match="1.x"):
        SQLiteStore(str(path))


def test_sqlite_rejects_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    with pytest.raises(StorageError, match="newer"):
        SQLiteStore(str(path))


def test_sqlite_persists_and_reopens(tmp_path):
    path = str(tmp_path / "p.db")
    s1 = SQLiteStore(path)
    rec = s1.add_track(_track("Persist", tags=["t"]), *_hashes(60, 20))
    s1.set_meta("k", "v")
    s1.close()
    s2 = SQLiteStore(path)
    assert s2.get_track(rec.track_id).tags == ["t"] and s2.get_meta("k") == "v"
    stats = s2.get_stats()
    assert stats["db_size_bytes"] > 0 and stats["persistent"] is True and stats["db_path"] == path
    assert s2.unique_hash_count() == np.unique(_hashes(60, 20)[0]).size
    s2.checkpoint()
    s2.vacuum()
    s2.close()


def test_sqlite_rejects_memory_path_and_prunes_dead_thread_connections(tmp_path):
    with pytest.raises(StorageError, match="memory"):
        SQLiteStore(":memory:")
    store = SQLiteStore(str(tmp_path / "prune.db"))

    def touch() -> None:
        store.count_tracks()

    for _ in range(5):
        t = threading.Thread(target=touch)
        t.start()
        t.join()
    assert store.open_connections == 1  # only the main thread's connection survives
    store.close()


def test_sqlite_batched_writes_are_searchable_and_flushed(tmp_path):
    store = SQLiteStore(str(tmp_path / "batch.db"), write_batch_rows=1_000)
    small = store.add_track(_track("small"), *_hashes(90, 100))
    assert store.pending_rows == 100 and store.get_stats()["pending_rows"] == 100
    # buffered rows are visible to lookups
    h, r, _t = store.query_hashes(np.unique(_hashes(90, 100)[0]))
    assert set(r.tolist()) == {small.ref} and h.size == 100
    # crossing the threshold flushes everything in one sorted transaction
    big = store.add_track(_track("big"), *_hashes(91, 2000))
    assert store.pending_rows == 0
    conn = sqlite3.connect(str(tmp_path / "batch.db"))
    assert conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 2100
    assert conn.execute("SELECT COUNT(*) FROM tracks WHERE flushed=0").fetchone()[0] == 0
    conn.close()
    # deleting a buffered track removes its pending rows too
    store.add_track(_track("gone"), *_hashes(92, 10))
    gone = store.list_tracks(query="gone")[0][0]
    assert store.pending_rows == 10
    store.delete_track(gone.track_id)
    assert store.pending_rows == 0
    store.close()
    # a batch that never reached the disk (simulated crash) is repaired on the next open
    conn = sqlite3.connect(str(tmp_path / "batch.db"))
    conn.execute("UPDATE tracks SET flushed=0 WHERE ref=?", (big.ref,))
    conn.commit()
    conn.close()
    reopened = SQLiteStore(str(tmp_path / "batch.db"), write_batch_rows=1_000)
    assert reopened.get_track(big.track_id) is None and reopened.get_track(small.track_id) is not None
    reopened.close()


def test_sqlite_common_hash_cap(tmp_path):
    store = SQLiteStore(str(tmp_path / "cap.db"), write_batch_rows=0)
    common = np.full(50, 777, dtype=np.int64)
    for i in range(3):
        store.add_track(_track(f"t{i}"), np.concatenate([common, np.array([1000 + i])]), np.arange(51, dtype=np.int64))
    stats: dict = {}
    h, _r, _t = store.query_hashes(np.array([777, 1000, 1001], dtype=np.int64), max_rows_per_hash=100, stats=stats)
    assert stats["skipped_hashes"] == 1 and 777 not in h.tolist() and set(h.tolist()) == {1000, 1001}
    h, _r, _t = store.query_hashes(np.array([777], dtype=np.int64))
    assert h.size == 150  # with no cap every row comes back
    store.close()


def test_sqlite_concurrent_writers(tmp_path):
    store = SQLiteStore(str(tmp_path / "c.db"))
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for j in range(5):
                store.add_track(_track(f"w{i}-{j}"), *_hashes(1000 * i + j, 300))
        except Exception as exc:  # pragma: no cover (failure path)
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and store.count_tracks() == 30
    store.close()


def test_sqlite_like_escaping(tmp_path):
    store = SQLiteStore(str(tmp_path / "l.db"))
    store.add_track(_track("100% match"), *_hashes(70, 3))
    store.add_track(_track("under_score"), *_hashes(71, 3))
    store.add_track(_track("plain"), *_hashes(72, 3))
    assert store.list_tracks(query="100%")[1] == 1
    assert store.list_tracks(query="_score")[1] == 1
    assert store.list_tracks(query="%")[1] == 1
    store.close()


def test_create_storage_factory(tmp_path):
    s = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path=str(tmp_path / "f.db"))
    store = create_storage(s)
    assert store.backend_name == "sqlite" and store.get_meta(META_SIGNATURE) == s.fingerprint_signature()
    store.add_track(_track("x"), *_hashes(80, 3))
    store.close()
    other = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path=str(tmp_path / "f.db"), fan_value=3)
    with pytest.raises(FingerprintCompatibilityError):
        create_storage(other)
    other_warn = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path=str(tmp_path / "f.db"), fan_value=3, fingerprint_compat="warn")
    create_storage(other_warn).close()
    mem = create_storage(Settings.load(dotenv=False, env={}, storage_type="memory"))
    assert mem.backend_name == "memory"
    assert json.loads(mem.get_meta("fingerprint_params"))["fan_value"] == 10
