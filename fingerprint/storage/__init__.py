"""Storage backends and the factory that picks one from :class:`~fingerprint.config.Settings`."""

from __future__ import annotations

from ..config import Settings
from ..utils.exceptions import ConfigurationError
from .base import META_PARAMS, META_SIGNATURE, SORTABLE_FIELDS, StorageBackend, TrackRecord, normalize_tags, validate_track_changes
from .memory_store import MemoryStore
from .sqlite_store import SQLiteStore

__all__ = [
    "META_PARAMS",
    "META_SIGNATURE",
    "SORTABLE_FIELDS",
    "MemoryStore",
    "SQLiteStore",
    "StorageBackend",
    "TrackRecord",
    "create_storage",
    "normalize_tags",
    "validate_track_changes",
]


def create_storage(settings: Settings, *, check_compat: bool = True) -> StorageBackend:
    """Instantiate the configured backend and verify fingerprint compatibility."""
    kind = settings.storage_type
    if kind == "memory":
        store: StorageBackend = MemoryStore()
    elif kind == "sqlite":
        store = SQLiteStore(
            settings.sqlite_path_resolved,
            cache_mb=settings.sqlite_cache_mb,
            mmap_mb=settings.sqlite_mmap_mb,
            write_batch_rows=settings.sqlite_write_batch_rows,
            track_index=settings.sqlite_track_index,
        )
    elif kind == "postgres":
        from .postgres_store import PostgresStore

        store = PostgresStore(settings.postgres_dsn, pool_size=settings.postgres_pool_size)
    else:  # pragma: no cover - validated in Settings
        raise ConfigurationError(f"Unknown storage type {kind!r}")

    if check_compat:
        try:
            store.initialize(settings.fingerprint_signature(), settings.fingerprint_params(), settings.fingerprint_compat)
        except Exception:
            store.close()
            raise
    return store
