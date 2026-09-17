# Changelog

Notable changes to AudioFP are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow [SemVer](https://semver.org/).

## [2.0.0] - 2026-09-17

This release reworks most of the code base. Databases created by 1.x cannot be reused, because the hash layout and the STFT framing changed. Re-index your library after upgrading; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#upgrading-from-1x).

### Added
- An `occurrences` search mode. It returns every alignment between the query and a track, with start and end spans in both, signed offsets (negative means the track's content was found inside the query), merging of jittered offsets and a per-track cap. This is the building block for pattern search on call recordings.
- Match scores that can be acted on: `aligned_hashes` (distinct hashes at the best offset), `confidence` (the share of the matched region the alignment explains), `peak_ratio` (how sharp the spike is compared with the histogram background) and a `quality` label. Three thresholds (`min_aligned_hashes`, `min_confidence`, `min_peak_ratio`) reject chance matches. Previously `top_k` "matches" always came back.
- A streaming pipeline with flat memory use. Decoding is chunked (libsndfile with soxr, ffmpeg as fallback) and so are the STFT and peak extraction, and the result is bit-identical to processing the whole file at once. An hour-long file no longer needs gigabytes of RAM.
- Typed configuration from `AUDIOFP_*` environment variables and `.env`, with profiles and validation. `audiofp config --describe` prints the reference table.
- A fingerprint compatibility stamp. The fingerprint parameters are hashed into the database, and opening it with different parameters is refused (configurable) so matching can't quietly degrade.
- Duplicate detection when indexing: by content SHA-256 by default, or by path, or off.
- A job manager with a bounded worker pool, progress, ETA and rate, per-file error lists, cancellation, and a persisted history that survives restarts (jobs that were running get the `interrupted` status). The fingerprint thread pool is shared.
- API: one JSON error envelope with stable codes and request ids, optional API key auth, a folder allow-list (`AUDIOFP_INDEX_ROOTS`), pagination, search, sort and filter for tracks, metadata editing (title, artist, tags, custom JSON), bulk delete, audio streaming with Range support, a tags endpoint, runtime search defaults, `/info`, a more detailed `/health`, an OpenAPI document and an interactive `/docs` page.
- Guard rails for searches. Hashes stored more than `AUDIOFP_MAX_ROWS_PER_HASH` times in the library are skipped as stop words and the vote join is capped at `AUDIOFP_MAX_SEARCH_VOTES`, so a single search can't exhaust memory. Every result carries a `diagnostics` block (rows fetched, votes, tracks scored, hashes skipped).
- Playback tokens. When an API key is set, `GET /tracks/{id}/stream-token` issues a short-lived HMAC token for `<audio src>` URLs, so the master key never ends up in a URL, a proxy log or a copied link.
- The `audiofp` CLI (`serve`, `index`, `search`, `tracks`, `stats`, `doctor`, `config`, `db`) with a progress bar and JSON output. `python -m fingerprint` works too.
- A rebuilt web UI. Search with both modes, thresholds, microphone recording, match timelines, play from the matched position and recent searches. A library page with pagination, search, sort and filter, an upload queue with progress, folder indexing, bulk selection and a track drawer for editing metadata. An activity page for jobs with progress, ETA, errors, cancel and history. A settings page for the API key, server defaults, theme and system info. Works with the keyboard and screen readers, has dark, light and system themes and a mobile layout.
- The PostgreSQL backend was rewritten on psycopg 3 with pooling, `COPY` bulk loads and batched lookups (`pip install "audiofp[postgres]"`). The contract tests run against it in CI.
- A test suite (config, core, storage contract, indexing, jobs, API, CLI and an opt-in scale test), ruff, GitHub Actions on Linux, Windows and macOS, a Dockerfile and compose file, `.env.example`, and contributor and security docs.

### Changed
- Package layout. `fingerprint.training` became `fingerprint.indexing`, the `config/` package became `fingerprint/config.py`, and `run.py` is now a thin wrapper around `audiofp serve`.
- Storage schema v3: integer track refs, a `WITHOUT ROWID` clustered fingerprint table (about half the disk of v1), a `meta` table, a cheap `get_stats` with no full scans, and temp-table batch lookups. Fingerprint rows are buffered and written in large sorted batches (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`), which keeps indexing throughput flat as the database grows. A `flushed` flag per track lets a crash in the middle of a batch be repaired on the next start, so no half-indexed tracks are left behind. Deleting tracks is a single scan, or an index lookup with `AUDIOFP_SQLITE_TRACK_INDEX=true`.
- Normalisation happens in a single decode pass (running peak, exact final threshold), so indexing reads each file once instead of twice.
- Candidate tracks are pre-filtered with a vectorised histogram bound before the per-track scoring, so search latency no longer grows with library size. The occurrences mode uses sorted-offset slices and a candidate-bin cap; a 30-minute query went from 157 s to under a second.
- The in-memory backend keeps a segmented index instead of re-sorting the whole table after every add. The PostgreSQL backend streams `COPY` in chunks and escapes `LIKE` wildcards.
- CORS is off in every profile unless `AUDIOFP_CORS_ORIGINS` is set. `docker-compose.yml` requires `AUDIOFP_API_KEY` and binds to `127.0.0.1` by default. `/docs` pins Swagger UI with Subresource Integrity and falls back to a plain route table when offline.
- JSON endpoints refuse bodies over 1 MiB (`413`) or without a `Content-Length` (`411`) before reading them. Upload form fields are validated with the same limits as `PATCH /tracks/{id}` before the file is saved. `POST /tracks/index-directory` checks the folder allow-list before it looks at the path.
- `audiofp serve` treats `SIGTERM` (`systemctl stop`, `docker stop`) as a clean shutdown: running jobs stop at the next file boundary, buffered fingerprints are flushed and the storage is closed. `Runtime.close()` can be called more than once.
- `AUDIOFP_LOG_FILE=auto` is the default. It writes `<data_dir>/logs/audiofp.log` in production and nothing elsewhere. A log directory that can't be written falls back to console logging with a warning instead of failing at startup.
- The light theme's status colours (success, error, info, warning) were darkened to meet WCAG AA contrast on text.
- The hash layout is now 12/12/12 bits (`n_fft` up to 8190, time delta up to 4095 frames). Pairs farther apart than `max_hash_time_delta` (200 frames by default) are dropped.
- API resources are tracks (`/api/v1/tracks`). The 1.x `/songs*`, `/upload` and `/index` paths still work as deprecated aliases. Responses use `track_id`; the `songs` array and `total_songs` field are kept as aliases.
- `librosa` is gone (the numpy STFT is equivalent). `waitress` is the production server, which also works on Windows; `gunicorn` is an optional extra.

### Fixed
- Frequency bin 1024 overflowed the 10-bit hash field in 1.x.
- Re-indexing a folder no longer duplicates the whole library.
- Uploads with non-ASCII names keep their extension.
- Upload paths are stored absolute, so playback works whatever the working directory is.
- `get_stats` no longer scans the fingerprint table on every UI poll.
- ffmpeg's stderr is drained in a thread, so chatty inputs can't deadlock it, and a process that finished on its own is no longer killed and reported as failed. A decode that fails after libsndfile has already produced audio is reported as an error rather than silently restarted with ffmpeg, which used to duplicate audio.
- Two identical files in one indexing batch are de-duplicated under a lock. Before, both could be inserted.
- SQLite connections of finished threads are pruned, and `:memory:` is rejected up front (each thread would have seen its own empty database).
- A storage failure in the middle of a job now fails the job with the error, instead of ending it as `completed` with nothing stored.
- Web UI: Enter on a button no longer triggers a search, live regions are patched in place (no more lost focus while polling), dialogs trap and restore focus, occurrence chips and segments work with the keyboard, the library page is clamped after deletes, and a failed load shows a retry instead of an endless skeleton.

## [1.0.0] - 2026-09-06

First release: Shazam-style fingerprinting with SQLite storage, a Flask API and a single-page UI.
