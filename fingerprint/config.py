"""Typed, environment-driven configuration.

Configuration is resolved in three layers, later layers overriding earlier ones:

1. Built-in defaults (the field defaults on :class:`Settings`).
2. The selected *profile* (``development`` / ``production`` / ``testing``).
3. Environment variables prefixed with ``AUDIOFP_`` (optionally loaded from a
   ``.env`` file in the working directory).

Example::

    AUDIOFP_PROFILE=production AUDIOFP_PORT=8080 AUDIOFP_API_KEY=secret audiofp serve

Every option is documented in ``docs/CONFIGURATION.md`` (generated from the
metadata on the fields below, so the two cannot drift).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from .utils.exceptions import ConfigurationError

ENV_PREFIX = "AUDIOFP_"
PROFILES = ("development", "production", "testing")

# Bump whenever a change to the algorithm makes previously stored fingerprints
# incompatible with freshly computed ones (hash layout, STFT framing, peak
# picking rules ...).  Parameter changes are covered separately by the signature.
FINGERPRINT_ALGORITHM_VERSION = 2


def _doc(help_text: str, **extra: Any) -> dict[str, Any]:
    meta = {"help": help_text}
    meta.update(extra)
    return meta


@dataclass
class Settings:
    """All runtime configuration for AudioFP.

    Field metadata: ``help`` (docs), ``secret`` (never printed), ``fingerprint``
    (part of the fingerprint signature), ``env=False`` (not overridable via env).
    """

    # ------------------------------------------------------------------ general
    profile: str = field(default="development", metadata=_doc("Configuration profile: development, production or testing."))
    debug: bool = field(default=False, metadata=_doc("Enable Flask debug mode (development only - never in production)."))
    data_dir: str = field(default="data", metadata=_doc("Root folder for the database, uploads, logs and job history."))

    # ------------------------------------------------------------------ audio
    sample_rate: int = field(default=11025, metadata=_doc("Working sample rate in Hz. All audio is resampled to this before fingerprinting.", fingerprint=True))
    n_fft: int = field(default=2048, metadata=_doc("STFT window size in samples (must be even).", fingerprint=True))
    hop_length: int = field(
        default=512, metadata=_doc("STFT hop size in samples. Time resolution of fingerprints is hop_length / sample_rate seconds.", fingerprint=True)
    )
    chunk_seconds: float = field(
        default=30.0, metadata=_doc("Audio is decoded and fingerprinted in chunks of this many seconds so memory stays flat for hour-long files.")
    )
    max_query_seconds: float = field(
        default=3600.0,
        metadata=_doc(
            "Query audio longer than this is truncated (the response flags query.truncated). Hour-long call recordings fit the default; raise it for longer material."
        ),
    )

    # ------------------------------------------------------------------ fingerprint
    peak_neighborhood_size: int = field(
        default=20,
        metadata=_doc(
            "Size (frames x frequency bins) of the local-maximum window used to pick spectral peaks. Smaller = denser fingerprints.", fingerprint=True
        ),
    )
    min_amplitude: float = field(
        default=10.0, metadata=_doc("Minimum linear STFT magnitude for a peak to be kept (filters silence and noise floor).", fingerprint=True)
    )
    fan_value: int = field(
        default=10,
        metadata=_doc("Number of following peaks each anchor peak is paired with. Higher = more hashes, more robust, more storage.", fingerprint=True),
    )
    min_hash_time_delta: int = field(
        default=0,
        metadata=_doc(
            "Minimum frame distance between paired peaks. 0 also pairs simultaneous peaks (harmonics of a chord), which are noise-robust; 1 excludes them for very repetitive tonal material.",
            fingerprint=True,
        ),
    )
    max_hash_time_delta: int = field(
        default=200, metadata=_doc("Maximum frame distance between paired peaks (200 frames ~ 9 s at the default rate).", fingerprint=True)
    )

    # ------------------------------------------------------------------ matching
    top_k: int = field(default=5, metadata=_doc("Default number of matches returned by a search."))
    max_top_k: int = field(default=50, metadata=_doc("Upper bound a client may request for top_k."))
    min_aligned_hashes: int = field(default=10, metadata=_doc("A candidate needs at least this many hashes aligned at one time offset to count as a match."))
    min_confidence: float = field(default=0.02, metadata=_doc("Minimum fraction of query hashes aligned at the best offset (0-1)."))
    min_peak_ratio: float = field(
        default=12.0,
        metadata=_doc(
            "Minimum ratio between the best offset's aligned count and the mean count across all offsets (peak sharpness). Chance matches are flat; real matches spike."
        ),
    )
    offset_tolerance_frames: int = field(
        default=1, metadata=_doc("Adjacent offset bins within +/- this many frames are merged when scoring (absorbs clip-start jitter).")
    )
    max_occurrences_per_track: int = field(default=25, metadata=_doc("In 'occurrences' mode, cap on reported occurrences per track."))
    max_rows_per_hash: int = field(
        default=2000,
        metadata=_doc(
            "Query hashes that occur more than this many times in the library are ignored as 'stop words' (hold-music loops, test tones). 0 disables the cap."
        ),
    )
    max_search_votes: int = field(
        default=5_000_000,
        metadata=_doc(
            "Upper bound on offset votes examined per search; the most common hashes are dropped first when a query would exceed it (keeps memory bounded on very repetitive material)."
        ),
    )

    # ------------------------------------------------------------------ storage
    storage_type: str = field(default="sqlite", metadata=_doc("Storage backend: memory, sqlite or postgres."))
    sqlite_path: str = field(default="", metadata=_doc("SQLite database file. Defaults to <data_dir>/fingerprints.db."))
    sqlite_cache_mb: int = field(default=64, metadata=_doc("SQLite page cache per connection in MB."))
    sqlite_mmap_mb: int = field(default=256, metadata=_doc("SQLite memory-mapped I/O window in MB (0 disables)."))
    sqlite_write_batch_rows: int = field(
        default=2_000_000,
        metadata=_doc(
            "Fingerprint rows buffered in memory (about 16 bytes each) before they are written to SQLite in one sorted transaction; 0 writes every track immediately. Buffered tracks are searchable; the buffer is flushed at the end of every indexing run and on shutdown."
        ),
    )
    sqlite_track_index: bool = field(
        default=False,
        metadata=_doc(
            "Maintain a secondary index on track_ref so deleting tracks is fast on very large libraries (costs about as much disk as the fingerprint table itself)."
        ),
    )
    postgres_dsn: str = field(default="", metadata=_doc("PostgreSQL connection string, e.g. postgresql://user:pass@host:5432/audiofp.", secret=True))
    postgres_pool_size: int = field(default=4, metadata=_doc("Maximum pooled PostgreSQL connections."))
    fingerprint_compat: str = field(
        default="strict",
        metadata=_doc(
            "What to do when the database was built with different fingerprint parameters: strict (refuse to start), warn (log and continue), ignore."
        ),
    )

    # ------------------------------------------------------------------ files / uploads
    upload_dir: str = field(default="", metadata=_doc("Where uploaded files are stored. Defaults to <data_dir>/uploads."))
    max_upload_mb: int = field(default=2048, metadata=_doc("Maximum request body size for uploads in MB."))
    keep_failed_uploads: bool = field(default=False, metadata=_doc("Keep uploaded files on disk when indexing fails (useful for debugging)."))
    dedupe: str = field(default="content", metadata=_doc("Duplicate detection when indexing: content (SHA-256 of file bytes), path (same file path), none."))
    index_roots: list[str] = field(
        default_factory=list,
        metadata=_doc("Directories the server is allowed to index via the API (comma-separated). Empty = any path in development, none in production."),
    )
    allow_directory_indexing: bool = field(default=True, metadata=_doc("Expose POST /tracks/index-directory. Disable to only allow uploads."))

    # ------------------------------------------------------------------ jobs
    index_workers: int = field(default=0, metadata=_doc("Threads used to fingerprint files concurrently. 0 = min(4, CPU count)."))
    max_concurrent_jobs: int = field(default=2, metadata=_doc("How many indexing jobs may run at the same time (each shares the index worker pool)."))
    job_history_limit: int = field(default=200, metadata=_doc("Finished jobs kept in memory / on disk."))
    job_max_errors: int = field(default=500, metadata=_doc("Per-file errors retained per job."))
    persist_jobs: bool = field(default=True, metadata=_doc("Write job records to <data_dir>/jobs so history survives restarts."))

    # ------------------------------------------------------------------ API / server
    host: str = field(default="127.0.0.1", metadata=_doc("Bind address for `audiofp serve`. Use 0.0.0.0 to expose on the network."))
    port: int = field(default=5000, metadata=_doc("Port for `audiofp serve`."))
    api_key: str = field(
        default="", metadata=_doc("If set, every /api/v1 request (except /health) must send it as X-API-Key or Authorization: Bearer.", secret=True)
    )
    cors_origins: list[str] = field(
        default_factory=list,
        metadata=_doc(
            "Allowed CORS origins for browser clients on other origins (comma-separated; '*' allows any). Empty = same-origin only, which is all the bundled UI needs. Never use '*' on a server without an API key: any website could then drive the API."
        ),
    )
    server_threads: int = field(default=8, metadata=_doc("Worker threads for the production (waitress) server."))
    trust_proxy: bool = field(default=False, metadata=_doc("Honour X-Forwarded-* headers from a reverse proxy."))

    # ------------------------------------------------------------------ logging
    log_level: str = field(default="INFO", metadata=_doc("Log level: DEBUG, INFO, WARNING, ERROR."))
    log_format: str = field(default="text", metadata=_doc("Log output format: text or json."))
    log_file: str = field(
        default="auto",
        metadata=_doc(
            "Log file path (rotating). 'auto' = <data_dir>/logs/audiofp.log in the production profile and console-only otherwise; 'none' (or empty) = console only."
        ),
    )
    log_max_mb: int = field(default=20, metadata=_doc("Rotate the log file after this many MB."))
    log_backup_count: int = field(default=5, metadata=_doc("Rotated log files to keep."))
    access_log: bool = field(default=True, metadata=_doc("Log one line per HTTP request."))

    # ------------------------------------------------------------------ derived helpers
    @property
    def sqlite_path_resolved(self) -> str:
        return self.sqlite_path or os.path.join(self.data_dir, "fingerprints.db")

    @property
    def upload_dir_resolved(self) -> str:
        return self.upload_dir or os.path.join(self.data_dir, "uploads")

    @property
    def jobs_dir(self) -> str:
        return os.path.join(self.data_dir, "jobs")

    @property
    def runtime_settings_path(self) -> str:
        return os.path.join(self.data_dir, "runtime-settings.json")

    @property
    def log_file_resolved(self) -> str:
        """Concrete log file path, or '' for console-only logging."""
        value = (self.log_file or "").strip()
        if value.lower() in ("", "none", "off", "false"):
            return ""
        if value.lower() == "auto":
            return os.path.join(self.data_dir, "logs", "audiofp.log") if self.is_production else ""
        return value

    @property
    def max_content_length(self) -> int:
        return int(self.max_upload_mb) * 1024 * 1024

    @property
    def effective_index_workers(self) -> int:
        if self.index_workers and self.index_workers > 0:
            return int(self.index_workers)
        return max(1, min(4, os.cpu_count() or 1))

    @property
    def is_production(self) -> bool:
        return self.profile == "production"

    # ------------------------------------------------------------------ fingerprint signature
    def fingerprint_params(self) -> dict[str, Any]:
        """Parameters that change the produced fingerprints (stored in the DB)."""
        params = {f.name: getattr(self, f.name) for f in fields(self) if f.metadata.get("fingerprint")}
        params["algorithm_version"] = FINGERPRINT_ALGORITHM_VERSION
        return params

    def fingerprint_signature(self) -> str:
        """Short stable hash of :meth:`fingerprint_params`."""
        blob = json.dumps(self.fingerprint_params(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ validation
    def validate(self) -> None:
        errors: list[str] = []
        if self.profile not in PROFILES:
            errors.append(f"profile must be one of {PROFILES}, got {self.profile!r}")
        if self.storage_type not in ("memory", "sqlite", "postgres"):
            errors.append(f"storage_type must be memory, sqlite or postgres, got {self.storage_type!r}")
        if self.storage_type == "postgres" and not self.postgres_dsn:
            errors.append("postgres_dsn is required when storage_type=postgres")
        if self.n_fft <= 0 or self.n_fft % 2:
            errors.append("n_fft must be a positive even number")
        if not (0 < self.hop_length <= self.n_fft):
            errors.append("hop_length must be between 1 and n_fft")
        if self.sample_rate < 4000:
            errors.append("sample_rate must be >= 4000 Hz")
        if self.n_fft // 2 > 4095:
            errors.append("n_fft too large: frequency bins must fit in 12 bits (n_fft <= 8190)")
        if not (0 <= self.min_hash_time_delta <= self.max_hash_time_delta <= 4095):
            errors.append("require 0 <= min_hash_time_delta <= max_hash_time_delta <= 4095")
        if self.fan_value < 1:
            errors.append("fan_value must be >= 1")
        if self.peak_neighborhood_size < 3:
            errors.append("peak_neighborhood_size must be >= 3")
        if self.chunk_seconds * self.sample_rate < 4 * self.n_fft:
            errors.append("chunk_seconds is too small for the STFT window")
        if not (0 <= self.min_confidence <= 1):
            errors.append("min_confidence must be between 0 and 1")
        if self.top_k < 1 or self.max_top_k < self.top_k:
            errors.append("require 1 <= top_k <= max_top_k")
        if self.max_rows_per_hash < 0 or self.max_search_votes < 10_000:
            errors.append("max_rows_per_hash must be >= 0 and max_search_votes >= 10000")
        if self.log_format not in ("text", "json"):
            errors.append("log_format must be text or json")
        if self.fingerprint_compat not in ("strict", "warn", "ignore"):
            errors.append("fingerprint_compat must be strict, warn or ignore")
        if self.dedupe not in ("content", "path", "none"):
            errors.append("dedupe must be content, path or none")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append(f"unknown log_level {self.log_level!r}")
        if errors:
            raise ConfigurationError("Invalid configuration: " + "; ".join(errors), details={"errors": errors})

    # ------------------------------------------------------------------ presentation
    def public_dict(self) -> dict[str, Any]:
        """Settings safe to expose (secrets redacted)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.metadata.get("secret"):
                value = "***" if value else ""
            out[f.name] = value
        return out

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, profile: str | None = None, env: dict[str, str] | None = None, dotenv: bool = True, **overrides: Any) -> Settings:
        """Build settings from defaults + profile + environment + explicit overrides."""
        env = dict(os.environ if env is None else env)
        if dotenv:
            env = {**_read_dotenv(env.get(ENV_PREFIX + "ENV_FILE", ".env")), **env}

        profile = profile or env.get(ENV_PREFIX + "PROFILE") or "development"
        values: dict[str, Any] = {"profile": profile}
        values.update(_profile_defaults(profile))

        for f in fields(cls):
            if f.metadata.get("env") is False:
                continue
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            if raw == "" and str(f.type) not in ("str", "<class 'str'>") and not str(f.type).startswith("list"):
                continue  # an empty value only makes sense for text/list settings (e.g. AUDIOFP_LOG_FILE= disables the file)
            values[f.name] = _coerce(f.name, f.type, raw)

        for key, value in overrides.items():
            if value is None:
                continue
            if key not in {f.name for f in fields(cls)}:
                raise ConfigurationError(f"Unknown setting {key!r}")
            values[key] = value

        settings = cls(**values)
        settings.validate()
        return settings


def _profile_defaults(profile: str) -> dict[str, Any]:
    if profile == "development":
        return {"debug": True, "log_level": "DEBUG"}
    if profile == "production":
        return {"debug": False, "log_level": "INFO", "host": "0.0.0.0"}
    if profile == "testing":
        return {"debug": False, "storage_type": "memory", "persist_jobs": False, "log_level": "WARNING", "data_dir": os.path.join("data", "test")}
    return {}


def _coerce(name: str, type_hint: Any, raw: str) -> Any:
    """Convert an environment string to the field's type."""
    hint = str(type_hint)
    try:
        if hint in ("bool", "<class 'bool'>"):
            lowered = raw.strip().lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            raise ValueError("expected a boolean")
        if hint in ("int", "<class 'int'>"):
            return int(raw.strip())
        if hint in ("float", "<class 'float'>"):
            return float(raw.strip())
        if hint.startswith("list"):
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw
    except ValueError as exc:
        raise ConfigurationError(f"Invalid value for {ENV_PREFIX}{name.upper()}={raw!r}: {exc}") from exc


def _read_dotenv(path: str) -> dict[str, str]:
    """Minimal ``.env`` reader (KEY=VALUE, quotes and ``#`` comments supported)."""
    result: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key.startswith(ENV_PREFIX):
            result[key] = value
    return result


def describe_settings() -> list[dict[str, Any]]:
    """Field metadata for documentation generators (``audiofp config --describe``)."""
    rows = []
    for f in fields(Settings):
        default = f.default if f.default is not MISSING else (f.default_factory() if f.default_factory is not MISSING else None)  # type: ignore[misc]
        rows.append(
            {
                "name": f.name,
                "env": ENV_PREFIX + f.name.upper(),
                "type": str(f.type).replace("<class '", "").replace("'>", ""),
                "default": default,
                "help": f.metadata.get("help", ""),
                "fingerprint": bool(f.metadata.get("fingerprint")),
                "secret": bool(f.metadata.get("secret")),
            }
        )
    return rows
