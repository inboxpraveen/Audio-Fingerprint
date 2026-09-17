# Configuration reference

AudioFP is configured with environment variables (prefix `AUDIOFP_`), optionally loaded from a `.env` file in the working directory (see `.env.example`). Resolution order, later wins:

1. built-in defaults (below)
2. the **profile** (`development`, `production`, `testing`) - see [Profiles](#profiles)
3. environment variables / `.env`
4. explicit CLI flags such as `--port`, `--data-dir`, `--storage`

Show the effective configuration with `audiofp config` (secrets redacted) and this table with `audiofp config --describe`.

> **Fingerprint options** (marked *fingerprint*) change the produced fingerprints. Their values are hashed into a *fingerprint signature* that is stamped into the database when it is created. Opening a database with a different signature is refused (`fingerprint_compat=strict`) so matching never silently degrades - re-index with `audiofp db reset` or point at a new database.


## General

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `profile` | `AUDIOFP_PROFILE` | str | `development` | Configuration profile: development, production or testing. |
| `debug` | `AUDIOFP_DEBUG` | bool | `False` | Enable Flask debug mode (development only - never in production). |
| `data_dir` | `AUDIOFP_DATA_DIR` | str | `data` | Root folder for the database, uploads, logs and job history. |

## Audio & fingerprinting (changing a *fingerprint* option requires re-indexing)

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `sample_rate` | `AUDIOFP_SAMPLE_RATE` | int | `11025` | Working sample rate in Hz. All audio is resampled to this before fingerprinting. *(fingerprint)* |
| `n_fft` | `AUDIOFP_N_FFT` | int | `2048` | STFT window size in samples (must be even). *(fingerprint)* |
| `hop_length` | `AUDIOFP_HOP_LENGTH` | int | `512` | STFT hop size in samples. Time resolution of fingerprints is hop_length / sample_rate seconds. *(fingerprint)* |
| `chunk_seconds` | `AUDIOFP_CHUNK_SECONDS` | float | `30.0` | Audio is decoded and fingerprinted in chunks of this many seconds so memory stays flat for hour-long files. |
| `max_query_seconds` | `AUDIOFP_MAX_QUERY_SECONDS` | float | `3600.0` | Query audio longer than this is truncated (the response flags query.truncated). Hour-long call recordings fit the default; raise it for longer material. |
| `peak_neighborhood_size` | `AUDIOFP_PEAK_NEIGHBORHOOD_SIZE` | int | `20` | Size (frames x frequency bins) of the local-maximum window used to pick spectral peaks. Smaller = denser fingerprints. *(fingerprint)* |
| `min_amplitude` | `AUDIOFP_MIN_AMPLITUDE` | float | `10.0` | Minimum linear STFT magnitude for a peak to be kept (filters silence and noise floor). *(fingerprint)* |
| `fan_value` | `AUDIOFP_FAN_VALUE` | int | `10` | Number of following peaks each anchor peak is paired with. Higher = more hashes, more robust, more storage. *(fingerprint)* |
| `min_hash_time_delta` | `AUDIOFP_MIN_HASH_TIME_DELTA` | int | `0` | Minimum frame distance between paired peaks. 0 also pairs simultaneous peaks (harmonics of a chord), which are noise-robust; 1 excludes them for very repetitive tonal material. *(fingerprint)* |
| `max_hash_time_delta` | `AUDIOFP_MAX_HASH_TIME_DELTA` | int | `200` | Maximum frame distance between paired peaks (200 frames ~ 9 s at the default rate). *(fingerprint)* |

## Matching thresholds

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `top_k` | `AUDIOFP_TOP_K` | int | `5` | Default number of matches returned by a search. |
| `max_top_k` | `AUDIOFP_MAX_TOP_K` | int | `50` | Upper bound a client may request for top_k. |
| `min_aligned_hashes` | `AUDIOFP_MIN_ALIGNED_HASHES` | int | `10` | A candidate needs at least this many hashes aligned at one time offset to count as a match. |
| `min_confidence` | `AUDIOFP_MIN_CONFIDENCE` | float | `0.02` | Minimum fraction of query hashes aligned at the best offset (0-1). |
| `min_peak_ratio` | `AUDIOFP_MIN_PEAK_RATIO` | float | `12.0` | Minimum ratio between the best offset's aligned count and the mean count across all offsets (peak sharpness). Chance matches are flat; real matches spike. |
| `offset_tolerance_frames` | `AUDIOFP_OFFSET_TOLERANCE_FRAMES` | int | `1` | Adjacent offset bins within +/- this many frames are merged when scoring (absorbs clip-start jitter). |
| `max_occurrences_per_track` | `AUDIOFP_MAX_OCCURRENCES_PER_TRACK` | int | `25` | In 'occurrences' mode, cap on reported occurrences per track. |
| `max_rows_per_hash` | `AUDIOFP_MAX_ROWS_PER_HASH` | int | `2000` | Query hashes that occur more than this many times in the library are ignored as 'stop words' (hold-music loops, test tones). 0 disables the cap. |
| `max_search_votes` | `AUDIOFP_MAX_SEARCH_VOTES` | int | `5000000` | Upper bound on offset votes examined per search; the most common hashes are dropped first when a query would exceed it (keeps memory bounded on very repetitive material). |

## Storage

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `storage_type` | `AUDIOFP_STORAGE_TYPE` | str | `sqlite` | Storage backend: memory, sqlite or postgres. |
| `sqlite_path` | `AUDIOFP_SQLITE_PATH` | str |  | SQLite database file. Defaults to <data_dir>/fingerprints.db. |
| `sqlite_cache_mb` | `AUDIOFP_SQLITE_CACHE_MB` | int | `64` | SQLite page cache per connection in MB. |
| `sqlite_mmap_mb` | `AUDIOFP_SQLITE_MMAP_MB` | int | `256` | SQLite memory-mapped I/O window in MB (0 disables). |
| `sqlite_write_batch_rows` | `AUDIOFP_SQLITE_WRITE_BATCH_ROWS` | int | `2000000` | Fingerprint rows buffered in memory (about 16 bytes each) before they are written to SQLite in one sorted transaction; 0 writes every track immediately. Buffered tracks are searchable; the buffer is flushed at the end of every indexing run and on shutdown. |
| `sqlite_track_index` | `AUDIOFP_SQLITE_TRACK_INDEX` | bool | `False` | Maintain a secondary index on track_ref so deleting tracks is fast on very large libraries (costs about as much disk as the fingerprint table itself). |
| `postgres_dsn` | `AUDIOFP_POSTGRES_DSN` | str |  | PostgreSQL connection string, e.g. postgresql://user:pass@host:5432/audiofp. *(secret)* |
| `postgres_pool_size` | `AUDIOFP_POSTGRES_POOL_SIZE` | int | `4` | Maximum pooled PostgreSQL connections. |
| `fingerprint_compat` | `AUDIOFP_FINGERPRINT_COMPAT` | str | `strict` | What to do when the database was built with different fingerprint parameters: strict (refuse to start), warn (log and continue), ignore. |

## Uploads & indexing

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `upload_dir` | `AUDIOFP_UPLOAD_DIR` | str |  | Where uploaded files are stored. Defaults to <data_dir>/uploads. |
| `max_upload_mb` | `AUDIOFP_MAX_UPLOAD_MB` | int | `2048` | Maximum request body size for uploads in MB. |
| `keep_failed_uploads` | `AUDIOFP_KEEP_FAILED_UPLOADS` | bool | `False` | Keep uploaded files on disk when indexing fails (useful for debugging). |
| `dedupe` | `AUDIOFP_DEDUPE` | str | `content` | Duplicate detection when indexing: content (SHA-256 of file bytes), path (same file path), none. |
| `index_roots` | `AUDIOFP_INDEX_ROOTS` | list[str] |  | Directories the server is allowed to index via the API (comma-separated). Empty = any path in development, none in production. |
| `allow_directory_indexing` | `AUDIOFP_ALLOW_DIRECTORY_INDEXING` | bool | `True` | Expose POST /tracks/index-directory. Disable to only allow uploads. |

## Background jobs

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `index_workers` | `AUDIOFP_INDEX_WORKERS` | int | `0` | Threads used to fingerprint files concurrently. 0 = min(4, CPU count). |
| `max_concurrent_jobs` | `AUDIOFP_MAX_CONCURRENT_JOBS` | int | `2` | How many indexing jobs may run at the same time (each shares the index worker pool). |
| `job_history_limit` | `AUDIOFP_JOB_HISTORY_LIMIT` | int | `200` | Finished jobs kept in memory / on disk. |
| `job_max_errors` | `AUDIOFP_JOB_MAX_ERRORS` | int | `500` | Per-file errors retained per job. |
| `persist_jobs` | `AUDIOFP_PERSIST_JOBS` | bool | `True` | Write job records to <data_dir>/jobs so history survives restarts. |

## API & server

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `host` | `AUDIOFP_HOST` | str | `127.0.0.1` | Bind address for `audiofp serve`. Use 0.0.0.0 to expose on the network. |
| `port` | `AUDIOFP_PORT` | int | `5000` | Port for `audiofp serve`. |
| `api_key` | `AUDIOFP_API_KEY` | str |  | If set, every /api/v1 request (except /health) must send it as X-API-Key or Authorization: Bearer. *(secret)* |
| `cors_origins` | `AUDIOFP_CORS_ORIGINS` | list[str] |  | Allowed CORS origins (comma-separated). '*' allows any. Empty disables CORS. |
| `server_threads` | `AUDIOFP_SERVER_THREADS` | int | `8` | Worker threads for the production (waitress) server. |
| `trust_proxy` | `AUDIOFP_TRUST_PROXY` | bool | `False` | Honour X-Forwarded-* headers from a reverse proxy. |

## Logging

| Setting | Environment variable | Type | Default | Description |
|---|---|---|---|---|
| `log_level` | `AUDIOFP_LOG_LEVEL` | str | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR. |
| `log_format` | `AUDIOFP_LOG_FORMAT` | str | `text` | Log output format: text or json. |
| `log_file` | `AUDIOFP_LOG_FILE` | str | `auto` | Log file path (rotating). 'auto' = <data_dir>/logs/audiofp.log in the production profile and console-only otherwise; 'none' (or empty) = console only. |
| `log_max_mb` | `AUDIOFP_LOG_MAX_MB` | int | `20` | Rotate the log file after this many MB. |
| `log_backup_count` | `AUDIOFP_LOG_BACKUP_COUNT` | int | `5` | Rotated log files to keep. |
| `access_log` | `AUDIOFP_ACCESS_LOG` | bool | `True` | Log one line per HTTP request. |

## Profiles

| Profile | Overrides | Typical use |
|---|---|---|
| `development` (default) | `debug=true`, `log_level=DEBUG`, binds to `127.0.0.1`, Flask dev server, no log file, directory indexing allowed for any path (unless `index_roots` is set) | local use |
| `production` | `debug=false`, `log_level=INFO`, `host=0.0.0.0`, `log_file=auto` resolves to `<data_dir>/logs/audiofp.log`, waitress server, `index_roots` is required and directory indexing only inside it | deployments |
| `testing` | `debug=false`, `storage_type=memory`, `persist_jobs=false`, `log_level=WARNING`, `data_dir=data/test` | test-suites |

CORS is off in every profile; set `AUDIOFP_CORS_ORIGINS` explicitly when a separate frontend calls the API.

Select a profile with `AUDIOFP_PROFILE=...` or `audiofp serve --profile ...`.


## Derived paths

| Path | Default | Setting |
|---|---|---|
| SQLite database | `<data_dir>/fingerprints.db` | `sqlite_path` |
| Uploaded files | `<data_dir>/uploads/` | `upload_dir` |
| Job history | `<data_dir>/jobs/` | `persist_jobs` |
| Runtime search defaults (`PUT /api/v1/settings`) | `<data_dir>/runtime-settings.json` | - |
| Log file (`log_file=auto`, production only) | `<data_dir>/logs/audiofp.log` | `log_file` | `AUDIOFP_LOG_FILE` | str | `auto` | Log file path (rotating). 'auto' = <data_dir>/logs/audiofp.log in the production profile and console-only otherwise; 'none' (or empty) = console only. |

## Examples

```bash
# Production on port 8080 with an API key, JSON logs and two allowed folders
AUDIOFP_PROFILE=production AUDIOFP_PORT=8080 AUDIOFP_API_KEY=s3cret \nAUDIOFP_LOG_FORMAT=json AUDIOFP_INDEX_ROOTS=/srv/calls,/srv/jingles audiofp serve

# PostgreSQL storage
AUDIOFP_STORAGE_TYPE=postgres AUDIOFP_POSTGRES_DSN=postgresql://audiofp:pw@db/audiofp audiofp serve

# Denser fingerprints for very short clips (new database required)
AUDIOFP_PEAK_NEIGHBORHOOD_SIZE=12 AUDIOFP_FAN_VALUE=15 AUDIOFP_SQLITE_PATH=data/dense.db audiofp index ./library
```


## Runtime-adjustable defaults

`top_k`, `min_confidence`, `min_aligned_hashes`, `min_peak_ratio` and the default search `mode` can also be changed while the server runs (Settings view in the UI or `PUT /api/v1/settings`). They are persisted to `<data_dir>/runtime-settings.json` and take precedence over the environment values above; every search request may still override them per call.
