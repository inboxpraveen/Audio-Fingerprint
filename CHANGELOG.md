# Changelog

All notable changes to AudioFP are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [2.0.0] — 2026-09-17

A ground-up hardening release. **Databases created by 1.x are not compatible** (the hash layout and framing changed); re-index your library after upgrading — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#upgrading-from-1x).

### Added
- **Occurrences search mode** (`mode=occurrences`): every alignment between query and track, with start/end spans in both the query and the track, signed offsets (negative = track content found inside the query), jitter merging and a per-track cap. This is the building block for pattern search on call recordings.
- **Match scoring you can trust**: `aligned_hashes` (distinct hashes at the best offset), `confidence` (fraction of the matched region explained), `peak_ratio` (spike sharpness vs. the histogram background) and a `quality` label; three configurable thresholds (`min_aligned_hashes`, `min_confidence`, `min_peak_ratio`) reject chance matches instead of always returning `top_k` "matches".
- **Streaming, flat-memory pipeline**: chunked decoding (libsndfile + soxr, ffmpeg fallback) and chunked STFT/peak extraction that is bit-identical to whole-file processing. An hour-long file no longer needs gigabytes of RAM.
- **Typed configuration** from `AUDIOFP_*` environment variables / `.env`, with profiles and validation; `audiofp config --describe` prints the reference table.
- **Fingerprint compatibility stamp**: the fingerprint parameters are hashed into the database; opening it with different parameters is refused (configurable) rather than silently degrading matches.
- **Duplicate detection** on index (content SHA-256 by default, path or none).
- **Job manager**: bounded worker pool, progress/ETA/rate, per-file error lists, cancellation, persisted history that survives restarts (`interrupted` status), shared fingerprint thread pool.
- **API**: uniform JSON error envelope with stable codes and request ids, optional API-key auth, folder allow-list (`AUDIOFP_INDEX_ROOTS`), pagination/search/sort/filter for tracks, metadata editing (title, artist, tags, custom JSON), bulk delete, Range-capable audio streaming, tags endpoint, runtime search defaults, `/info`, richer `/health`, OpenAPI document and an interactive `/docs` page.
- **CLI** `audiofp` (`serve`, `index`, `search`, `tracks`, `stats`, `doctor`, `config`, `db`) with a progress bar and JSON output; `python -m fingerprint`.
- **Web UI** rebuilt: Search (both modes, thresholds, microphone recording, match timelines, play-from-match, recent searches), Library (pagination, search/sort/filter, upload queue with progress, folder indexing, bulk selection, track drawer with metadata editing), Activity (jobs with progress, ETA, errors, cancel, history), Settings (API key, server defaults, theme, system info). Keyboard and screen-reader friendly, dark/light/system themes, mobile layout.
- **PostgreSQL backend** rewritten on psycopg 3 with pooling, `COPY` bulk loads and batched lookups (`pip install "audiofp[postgres]"`), exercised by the CI contract tests.
- **Search safety rails**: hashes stored more than `AUDIOFP_MAX_ROWS_PER_HASH` times in the library are skipped as stop words and the vote join is capped at `AUDIOFP_MAX_SEARCH_VOTES`, so one search can never exhaust memory; the API returns a `diagnostics` block (rows fetched, votes, tracks scored, hashes skipped) with every result.
- **Playback tokens**: when an API key is set, `GET /tracks/{id}/stream-token` issues a short-lived HMAC token for `<audio src>` URLs so the master key never appears in a URL, proxy log or copied link.
- **Test suite** (config, core, storage contract, indexing, jobs, API, CLI, opt-in scale test), ruff linting, GitHub Actions CI on Linux/Windows/macOS, Dockerfile + compose, `.env.example`, contributor and security docs.

### Changed
- Package layout: `fingerprint.training` → `fingerprint.indexing`; `config/` package replaced by `fingerprint/config.py`; `run.py` is now a thin wrapper around `audiofp serve`.
- Storage schema v3: integer track refs, `WITHOUT ROWID` clustered fingerprint table (about half the disk of v1), `meta` table, cheap `get_stats` (no full scans), temp-table batch lookups. Fingerprint rows are buffered and written in large sorted batches (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`), which keeps indexing throughput flat as the database grows; a `flushed` flag per track lets a crash mid-batch be repaired on the next start instead of leaving half-indexed tracks behind. Deleting tracks is a single scan (or an index lookup with `AUDIOFP_SQLITE_TRACK_INDEX=true`).
- Normalisation is applied in a single decode pass (running peak + exact final threshold), so indexing reads each file once instead of twice.
- Candidate tracks are pre-filtered with a vectorised histogram bound before per-track scoring, so search latency no longer grows linearly with library size; the occurrences mode uses sorted-offset slices and a candidate-bin cap (a 30-minute query went from 157 s to under a second).
- The in-memory backend keeps a segmented index instead of re-sorting the whole table after every add; the PostgreSQL backend streams `COPY` in chunks and escapes `LIKE` wildcards.
- CORS is off in every profile unless `AUDIOFP_CORS_ORIGINS` is set; `docker-compose.yml` requires `AUDIOFP_API_KEY` and binds to `127.0.0.1` by default; `/docs` pins Swagger UI with Subresource Integrity and falls back to a plain route table offline.
- JSON endpoints refuse bodies over 1 MiB (`413`) or without a `Content-Length` (`411`) before reading them; upload form fields are validated with the same limits as `PATCH /tracks/{id}` before the file is saved; `POST /tracks/index-directory` checks the folder allow-list before it probes the path.
- `audiofp serve` handles `SIGTERM` (`systemctl stop`, `docker stop`) as a clean shutdown: running jobs are cancelled at the next file boundary, buffered fingerprints are flushed and storage is closed; `Runtime.close()` is idempotent.
- `AUDIOFP_LOG_FILE=auto` (the default) writes `<data_dir>/logs/audiofp.log` in production and nothing elsewhere; an unwritable log directory degrades to console logging with a warning instead of failing startup.
- Light theme status colours (success/error/info/warning) were darkened to meet WCAG AA contrast on text.
- Hash layout is now 12/12/12 bits (`n_fft` up to 8190, `Δt` up to 4095 frames); pairs farther apart than `max_hash_time_delta` (default 200 frames) are dropped.
- API resources are **tracks** (`/api/v1/tracks`); the 1.x `/songs*`, `/upload` and `/index` paths remain as deprecated aliases. Responses use `track_id` (the `songs` array and `total_songs` fields are kept as aliases).
- `librosa` is no longer a dependency (numpy STFT is equivalent); `waitress` is the production server (works on Windows), `gunicorn` is an optional extra.

### Fixed
- Frequency bin 1024 overflowed the 10-bit hash field in 1.x.
- Re-indexing a folder no longer duplicates the whole library.
- Uploads with non-ASCII names no longer lose their extension.
- Upload paths are stored absolute, so playback works regardless of the working directory.
- `get_stats` no longer scans the fingerprint table on every UI poll.
- ffmpeg's stderr is drained in a thread (no more dead-locks on chatty inputs) and a finished process is never killed and reported as failed; a decode that fails after libsndfile has already produced audio is reported instead of silently restarting with ffmpeg (which duplicated audio).
- Two identical files in one indexing batch are de-duplicated under a lock (previously both could be inserted).
- SQLite connections of finished threads are pruned; `:memory:` is rejected up front (each thread would have seen its own empty database).
- A storage failure mid-job now fails the job (`failed` with the error) instead of ending it as `completed` with nothing stored.
- The web UI: Enter on a button no longer triggers a search, live regions are patched instead of re-rendered (no more focus loss during polling), dialogs trap and restore focus, occurrence chips and segments are keyboard-operable, the library page is clamped after deletes, and load errors show a retry instead of an endless skeleton.

## [1.0.0] — 2026-09-06

Initial release: Shazam-style fingerprinting with SQLite storage, Flask API and a single-page UI.
