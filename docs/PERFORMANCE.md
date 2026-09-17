# Performance and sizing

This page explains where AudioFP spends time and memory, what grows with what,
and how to size storage and hardware. Every number below is either derived from
the code (default settings in `fingerprint/config.py`) or measured on one
developer machine and marked **indicative**. Measure your own material before
committing to hardware; the last section shows how.

Measurement environment for the indicative figures: Windows 11, 24 logical CPUs,
Python 3.13, numpy 2.3, SQLite on a local SSD, **synthetic** test audio (the
repository ships no real recordings; the generator in `tests/conftest.py`
produces sparse, tonal material at ~14 peaks/s, well below the ~55 peaks/s
practical ceiling explained below). Real music will generally be denser;
telephone speech with pauses will be sparser.

## Indicative figures

| What | Result | Notes |
|---|---|---|
| Fingerprint a 30-minute mono WAV (`pytest -m slow tests/test_scale.py`) | ~3.5 s, RSS growth ~40 MB | 25,474 peaks, 254,685 hashes (~141 hashes/s). Measured when normalisation still decoded the file twice; the current single-pass code decodes and resamples (from 22.05 kHz) once, so expect the same or less. |
| Same file, AudioFP 1.x implementation | RSS grew to ~1.3 GB for a 1-hour file | Reported figure for the removed whole-file-in-memory pipeline; not reproducible from this tree (not verified). |
| 4-s clip search against a small library, `POST /api/v1/search` | ~15-25 ms `processing_time_ms` | In-process time: decoding the clip (measured with the old two-pass normalisation; now one pass), lookup, scoring (excludes the HTTP upload). |
| Matcher only (`Matcher.match`), 4-5 s clip vs a 10-40 minute library | 5-7 ms | 465-565 query hashes, ~100-160 rows returned. |
| 60-s query in `occurrences` mode vs a 40-minute library | ~80 ms | 8,275 query hashes, ~8,500 rows. |
| SQLite insert rate (`SQLiteStore.add_track`) | 270,000-400,000 rows/s | Measured with immediate writes (one transaction per track, keys pre-sorted - equivalent to `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0`). The default now buffers 2,000,000 rows and writes them in one sorted transaction per batch (`flush()`), which touches each B-tree leaf once per batch instead of once per track; not re-measured. |
| SQLite bytes per fingerprint row | ~17 bytes (40-track library, after checkpoint) | Plan with 20-25 bytes; see sizing. |
| `unique_hash_count()` on 346k rows | ~31 ms | Full index scan; grows linearly. |
| Delete one track from a 516k-row SQLite library | ~170 ms | Full scan of the fingerprint B-tree (see SQLite section); `sqlite_track_index=true` makes it an index lookup. |
| Indexing 8 x 2-minute stereo 44.1 kHz WAVs, `index_workers` 1 / 2 / 4 / 8 | 430x / 770x / 1130x / 1190x real time (a re-run with different synthetic material gave 245x / 450x / 725x / 925x) | Diminishing returns beyond 4 workers; absolute figures depend on the material. |

## Memory model: what is streaming and what accumulates

Fingerprinting never holds a whole file in memory. Memory is flat in file
length; only the small outputs (peaks and hashes) grow with duration.

**Decode (`fingerprint/core/decoder.py`, `iter_audio_chunks`)** yields float32
mono blocks of roughly `chunk_seconds` seconds at `sample_rate`. libsndfile
reads WAV/FLAC/OGG/Opus/MP3/AIFF blocks directly; sample-rate conversion uses
`soxr`'s streaming resampler. M4A/AAC/WMA and every video container go through
an `ffmpeg` subprocess that writes 16-bit PCM to a pipe, already mono and
resampled, so no extra buffers are needed in Python. ffmpeg is optional at
runtime and only required for those formats (and as a fallback when libsndfile
cannot open a file, e.g. MP3 on a libsndfile build without MP3 support).

**STFT and peak picking (`fingerprint/core/fingerprinter.py`, `PeakExtractor`)**
processes each chunk as it arrives and keeps only a `peak_neighborhood_size`
wide strip of spectrogram columns as context for the next chunk (plus an
equally narrow strip of not-yet-finalised columns). Chunked processing yields
exactly the same peaks as processing the whole signal at once (asserted by
`test_chunked_peaks_identical_to_whole_signal` in `tests/test_core.py`), so
`chunk_seconds` is purely a memory/CPU knob:

- Transient buffers per chunk (decoder block, windowed frames, FFT output,
  magnitude block, max-filter mask) scale linearly with `chunk_seconds`. At the
  default 30 s this is a few tens of MB regardless of file length, which is what
  the ~40 MB RSS growth above is.
- Smaller chunks use less transient memory and more Python-level iterations;
  `Settings.validate` requires `chunk_seconds * sample_rate >= 4 * n_fft`
  (~0.74 s at defaults).

**What accumulates** per file: two `int32` arrays per peak and an `int64` hash
plus an `int32` anchor time per hash, i.e. 12 bytes per hash (with
`normalize=True` also one `float32` magnitude per candidate peak until
`finish()`). A one-hour recording at ~141 hashes/s is ~500k hashes, about
6 MB. `SQLiteStore.add_track` then keeps the hashes and times as two `int64`
arrays (16 bytes per row) in the process-wide write buffer, bounded by
`sqlite_write_batch_rows` (2,000,000 rows, about 32 MB). `flush()` first
concatenates hashes, refs and times and sorts them (`np.lexsort`), which
costs sorted copies proportional to the batch (24 bytes per row plus the sort
index, on the order of 100 MB for a full default buffer), then converts them
to Python tuples `INSERT_SLICE` (200,000) rows at a time, so only the
tuple transient is bounded independently of the batch size.

**Single-pass normalisation.** `Fingerprinter.fingerprint_file(normalize=True)`
(the default, and what the indexer and the search path use) peak-normalises
without decoding twice: `PeakExtractor(..., normalize=True)` tracks the
running peak sample, keeps candidate peaks with their magnitudes against the
running threshold `min_amplitude * running peak`, and applies the exact final
threshold `min_amplitude * peak` in `finish()`. The result is bit-identical to
fingerprinting the peak-normalised signal, and decode, resample and STFT each
run once (one ffmpeg subprocess per file for ffmpeg-backed formats).
`normalize` is a keyword argument on `fingerprint_file`, not a
`Settings` field, so there is no environment switch for it. `max_seconds` is
forwarded to ffmpeg as `-t` (with one second of slack), so a truncated query
never decodes more than a second of the part it will discard.

## What scales with what

### Time resolution and hash density

| Quantity | Formula (defaults) | Value |
|---|---|---|
| Frames per second | `sample_rate / hop_length` | 21.5 frames/s (one frame = 46.4 ms, exposed as `frame_seconds` by `GET /api/v1/info`) |
| Frequency bins | `n_fft / 2 + 1` | 1,025 |
| Practical peak density ceiling | `bins * frames_per_s / peak_neighborhood_size^2` | ~55 peaks/s (white noise measures 55.8) |
| Hashes per peak | up to `fan_value`, minus pairs further than `max_hash_time_delta` frames apart | 9.97 measured with `fan_value=10` (`max_hash_time_delta=200` is ~9.3 s, rarely binding) |
| Hashes per second | ~ `fan_value` x peaks/s | ~141 measured on synthetic material; ~550 practical ceiling |

`generate_hashes` pairs every peak with the next `fan_value` peaks, so hash
count is linear in peak count; `min_amplitude` and `peak_neighborhood_size`
set the peak count. The practical ceiling is the random-field density of local
maxima in a `peak_neighborhood_size` square window, about one peak per
`peak_neighborhood_size^2` spectrogram cells, which broadband noise reaches.
The hard packing limit of `scipy.ndimage.maximum_filter` is a few times higher
(maxima may sit half a window apart when magnitudes never decrease), but real
audio does not approach it.

All of `sample_rate`, `n_fft`, `hop_length`, `peak_neighborhood_size`,
`min_amplitude`, `fan_value`, `min_hash_time_delta` and `max_hash_time_delta`
are part of `Settings.fingerprint_signature()`. Changing any of them to trade
storage for robustness means the existing database no longer matches; with
`fingerprint_compat=strict` (default) the store refuses to open until you run
`audiofp db reset` and re-index.

### Index size

Every hash becomes one row `(hash_value, track_ref, time_offset)`. The `tracks`
table is one row per file and negligible. In SQLite the row lives in the
clustered `WITHOUT ROWID` primary key `(hash_value, track_ref, time_offset)`:
the 36-bit hash is stored as a 4- or 6-byte integer, the two small integers as
1-3 bytes each, plus record header and cell pointer, which is why a packed
library measures ~17 bytes/row. Plan with **20-25 bytes/row** to allow for B-tree page
slack after interleaved inserts and deletes.

### Matcher memory and the "common hash" caveat

`Matcher.match` (`fingerprint/core/matcher.py`) works in three stages, and its
memory is set by the second one:

1. `store.query_hashes(unique_query_hashes, max_rows_per_hash=..., stats=...)`
   returns every stored row whose hash appears in the query, **except** the
   rows of hashes that occur more than `max_rows_per_hash` (default 2000, `0`
   disables) times in the library: SQLite and PostgreSQL find those with a
   `GROUP BY hash_value HAVING COUNT(*) > ?` pre-query, the memory store with
   `np.unique(..., return_counts=True)`, and report how many were skipped in
   `stats["skipped_hashes"]` (`diagnostics.skipped_common_hashes`). There is no
   other `LIMIT`.
2. The rows are joined to the query anchors: each row is repeated once per
   query anchor carrying the same hash (`counts = right - left`), producing the
   vote arrays (`offsets`, `qtimes`, `qhashes`, ...). At peak this costs roughly
   200-250 bytes per returned row (driver tuples plus ~8 `int64` arrays per
   vote; estimate). One million rows is therefore ~250 MB of transient memory.
   The join is capped at `max_search_votes` (default 5,000,000): when the
   projected vote count exceeds it, `_cap_votes` drops the rows of the most
   vote-heavy hashes first until it fits, counts them in
   `diagnostics.dropped_for_vote_cap` and logs a warning naming
   `AUDIOFP_MAX_SEARCH_VOTES`, so the vote arrays are bounded whatever the
   library contains (the rows themselves have already been fetched at that
   point; the stop-word cap in stage 1 is what keeps *those* small).
3. Votes are grouped per track. A vectorised prefilter (one `np.unique` over
   `(track, offset)` keys) drops every track whose best raw offset bin times
   `2 * offset_tolerance_frames + 1` cannot reach `min_aligned_hashes`, and
   `_score_track` runs only for the survivors (`diagnostics.scored_tracks` out
   of `diagnostics.candidate_tracks`), a Python loop with `argsort` and a
   sort-based `_count_distinct` on that track's votes.

Rows returned = sum over the query's distinct hashes of that hash's occurrence
count in the library. This is dominated by the most common hash values, not by
library size alone. Very repetitive material (hold music, dial tones, IVR
prompts, test tones, sustained notes) produces the same few hash values
thousands of times per track; a long query containing the same kind of material
repeats them on the query side too, and the join multiplies the two. This is
the one place where memory can grow well beyond the library-size intuition.
Mitigations that exist in the code:

- `max_rows_per_hash` (`AUDIOFP_MAX_ROWS_PER_HASH`, default 2000) skips the
  "stop word" hashes at the lookup, before any row is returned; the skipped
  count is visible per query as `diagnostics.skipped_common_hashes`. A hash
  shared by more than 2000 rows carries almost no information about *which*
  track matches, so the default costs nothing on ordinary material; set it
  higher (or `0`) only if a genuine pattern is itself that repetitive.
- `max_search_votes` (`AUDIOFP_MAX_SEARCH_VOTES`, default 5,000,000) caps the
  join in stage 2 (`diagnostics.dropped_for_vote_cap`), the last line of
  defence when the query side is repetitive too.
- `max_query_seconds` (default 3600) truncates queries; the response sets
  `query.truncated`.
- `min_hash_time_delta=1` drops pairs between simultaneous peaks (`delta_t == 0`,
  the harmonics of one chord), the most repetitive hash class on tonal material.
  The default is `0`, which keeps those pairs; it is a fingerprint parameter
  (re-index required).
- Per-track scoring is bounded: `_find_occurrences` examines at most
  `MAX_CANDIDATE_BINS = 400` offset bins per track, strongest first, and only
  bins whose smoothed vote count could pass `min_aligned_hashes` and
  `min_peak_ratio`. This caps CPU per track for repetitive material; the vote
  arrays from stage 2 are bounded by `max_search_votes`, not by this.

`matched_rows` in every search result is the vote count for that track, and
the `diagnostics` object (`query_hashes`, `db_rows`, `votes`,
`candidate_tracks`, `scored_tracks`, `skipped_common_hashes`,
`dropped_for_vote_cap`) shows the whole funnel, which makes the effect visible
per query.

## Sizing table

Storage per hour of indexed audio at default settings. Rows/hour is the
measured or estimated hash rate times 3,600; bytes assume the 20-25 bytes/row
planning figure for SQLite. **All rows are estimates**; the hash rate of your
own material is the only number that matters, and `audiofp stats --json`
reports the inputs (`total_hashes / total_duration_sec`).

| Material | Hashes/s | Rows per hour | SQLite per hour | SQLite per 1,000 hours | Basis |
|---|---|---|---|---|---|
| Sparse tonal audio with silence (Windows system sounds) | ~30 | ~110k | 2-3 MB | 2-3 GB | measured |
| Telephone speech with pauses | 50-150 | 180k-540k | 4-14 MB | 4-14 GB | estimate, unmeasured |
| Synthetic pseudo-music from the test-suite | ~141 | ~510k | 10-13 MB | 10-13 GB | measured |
| Dense music (planning figure) | ~300 | ~1.1M | 22-27 MB | 22-27 GB | estimate, unmeasured |
| Broadband noise (practical ceiling) | ~550 | ~2.0M | 40-50 MB | 40-50 GB | measured; matches the derived random-field density |

RAM for search: the hot part of the SQLite B-tree should sit in the OS page
cache or the mmap window (`sqlite_mmap_mb`); lookups are random reads across
the whole key range, so a library much larger than RAM becomes disk-bound on
the first touch of each page. Add the matcher's transient memory (above) per
concurrent search.

## Indexing throughput

Per file the pipeline is: optional SHA-256 pass -> one decode pass + STFT +
peaks + hashes (normalisation is folded into the same pass) -> a short SQLite
transaction for the `tracks` row, with the hashes appended to the in-memory
write buffer -> one sorted write transaction per `sqlite_write_batch_rows`
rows (2,000,000 by default; about 4 hours of audio at ~141 hashes/s) and at
the end of the run. On the measurement machine a single worker fingerprints
WAV at several hundred times real time; the store insert for an hour of audio
(~500k rows) took ~1.5-2 s when measured with immediate per-track writes
(`sqlite_write_batch_rows=0`); batching moves that cost to the flush and
visits each B-tree leaf once per batch instead of once per track (not
re-measured).

| Lever | Where | Effect |
|---|---|---|
| `index_workers` (`AUDIOFP_INDEX_WORKERS`, `audiofp index --workers N`) | `Settings.effective_index_workers`; `0` = `min(4, cpu_count)` | Files are fingerprinted concurrently on one `ThreadPoolExecutor` (`Indexer.executor`). Measured 430x -> 770x -> 1130x -> 1190x real time for 1/2/4/8 workers (re-run: 245x -> 450x -> 725x -> 925x). |
| Why threads scale at all | numpy FFT/sort/ufuncs, libsndfile, soxr and the ffmpeg pipe release the GIL | The heavy parts run in parallel; Python glue (chunk loop, `tolist()`, `executemany` at flush time) does not, hence the plateau. |
| Why writes do not scale | `SQLiteStore._write_lock` | `add_track` (the `tracks` row) and `flush()` (the batched fingerprint write) are serialised per process; while a flush runs, other workers wait at their next `add_track`. Fingerprinting dominates, so this is rarely the bottleneck, but it is the ceiling. `sqlite_write_batch_rows` sets how often the pause happens and how long it lasts. |
| `sqlite_write_batch_rows` (default 2,000,000) | `SQLiteStore.flush()` | Rows buffered before one sorted write; `0` writes every track immediately (the 2.0 behaviour before batching, useful when durability per file matters more than throughput). Buffered tracks are searchable and `GET /stats` shows the buffer as `pending_rows`. |
| Submission window | `Indexer.index_paths` keeps at most `2 x workers` files in flight | Bounds memory for 100,000-file runs and makes cancellation prompt. |
| `max_concurrent_jobs` (default 2) | `JobManager` has its own small pool for job *runners* | All jobs share the one indexer pool; raising this adds no fingerprint parallelism, it only lets more jobs progress at once. |
| `dedupe` (default `content`) | `Indexer.index_file` | `content` streams the file once through `sha256_file` (1 MB blocks; ~20 ms for a 10 MB WAV, negligible next to fingerprinting) and looks up `idx_tracks_content_hash`. `path` is an index lookup only (`idx_tracks_filepath`); `none` skips the check. |
| Source format | `decoder.select_backend` | libsndfile formats decode in-process. ffmpeg formats pay one subprocess spawn per file, with ffmpeg doing the mono mix and resample. |
| Source sample rate | `soxr` HQ streaming resampler | Anything not already at `sample_rate` (11025) is resampled once; native 11025 Hz material skips this. |
| Identical files in one batch | `Indexer._store_lock` | The duplicate check is repeated under a lock right before `add_track`, so several workers fingerprinting byte-identical files store one track; the others cost their fingerprinting time and end as `duplicate`. |
| Failure isolation | `IndexOutcome` per file | A bad file costs only its own decode attempt; the run continues. |

Deleting or replacing a track in SQLite deletes its fingerprint rows by
`track_ref`, and by default the only index on that table is the primary key
led by `hash_value`, so each such statement scans the whole fingerprint
B-tree while holding the write lock (~170 ms for 0.5M rows, linear from
there). `delete_track` and `delete_tracks` issue `DELETE ... WHERE track_ref
IN (...)` for up to 500 refs at a time in one transaction, so a bulk delete
costs one scan per 500 tracks rather than one per track; `add_track` with an
existing `track_id` (replace) still runs one `WHERE track_ref=?` scan. Rows of
tracks still in the write buffer are simply dropped from the buffer.
`sqlite_track_index=true` (`AUDIOFP_SQLITE_TRACK_INDEX`) adds
`idx_fp_track ON fingerprints (track_ref)`, which makes every delete an index
lookup at the price of roughly doubling the disk footprint of the fingerprint
table; enable it on libraries where tracks are deleted or replaced routinely.
PostgreSQL has `idx_fp_track` and `ON DELETE CASCADE` unconditionally, so
deletes there are always indexed.

## Search latency

`Runtime.search_file` fingerprints the query (`max_seconds=max_query_seconds`)
and calls `Matcher.match`; `processing_time_ms` in the response covers both but
not the multipart upload itself, which `POST /api/v1/search` saves to a temp
file first.

| Lever | Effect |
|---|---|
| Query length | Fingerprinting cost and hash count are linear in seconds (~141 hashes/s on the test material). An hour-long recording is ~7 s of fingerprinting at the rate measured above, plus a larger lookup. `max_query_seconds` caps it. |
| Library hash distribution | Rows returned by `query_hashes` (see the common-hash caveat) drive lookup time, join memory and the number of tracks scored; `max_rows_per_hash` and `max_search_votes` bound them, and `diagnostics` in the response shows the counts. |
| Mode | `identify` stops after the best offset per track; `occurrences` examines up to `MAX_CANDIDATE_BINS` bins per track and returns up to `max_occurrences_per_track` (default 25, API form field `max_occurrences`, max 500). |
| Thresholds | Higher `min_aligned_hashes` rejects more tracks in the vectorised prefilter (before any per-track Python work) and at the cheap early check in `_score_track`; both it and `min_peak_ratio` shrink the candidate-bin set in `_find_occurrences`. They can be changed per request or via `PUT /api/v1/settings` without re-indexing. |
| `top_k` | Only affects the final `get_tracks_by_ref` metadata fetch; negligible. |
| SQLite page cache and mmap | `sqlite_cache_mb`, `sqlite_mmap_mb` (below). The first search after start-up is cold. |
| Concurrency | The production server runs `server_threads` (default 8) waitress threads; each has its own SQLite connection and the numpy parts run in parallel. Searches are otherwise single-threaded. |

The lookup itself is one statement: `SQLiteStore.query_hashes` loads the
distinct query hashes into a `TEMP` table and joins it against `fingerprints`,
which the planner executes as one primary-key probe per query hash
(`SEARCH f USING PRIMARY KEY (hash_value=?)`). With `max_rows_per_hash` set
(the default) a `GROUP BY ... HAVING COUNT(*) > ?` pre-query over the same join
removes the stop-word hashes from the temp table first, so each query hash is
probed twice but the heavy ones are never materialised. Rows still in the
write buffer are matched in memory (`np.isin` per buffered track) and
appended. There is no chunking by SQLite's variable limit, so a 1-hour query
is still a single query. `PostgresStore` does the same with
`WHERE hash_value = ANY(%s)`, a `GROUP BY hash_value HAVING COUNT(*) > %s`
pre-query and the covering index.

## SQLite tuning

`SQLiteStore._connect` sets these pragmas on every connection:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers do not block the (single) writer; searches keep working while indexing runs. |
| `synchronous` | `NORMAL` | Safe with WAL against process crashes; avoids an fsync per transaction. |
| `cache_size` | `-(sqlite_cache_mb * 1024)` KiB, default 64 MB | Page cache **per connection**, allocated on demand. Connections are per thread (`threading.local`), so the worst case is roughly `(server_threads + index_workers) x sqlite_cache_mb`. |
| `temp_store` | `MEMORY` | The per-query `query_hashes` temp table never touches disk. |
| `mmap_size` | `sqlite_mmap_mb` MB, default 256 MB | Reads the database through the OS page cache without copying into the page cache; shared between connections, not multiplied. Set it to at least the size of your `fingerprints.db` for read-heavy deployments; `0` disables. |
| `busy_timeout` | 30 s (constructor default, not a setting) | Other processes touching the same file wait instead of failing with `SQLITE_BUSY`. |
| `foreign_keys` | `ON` | The schema (v3) declares no foreign keys, so this has no effect (and no cost). |

The two cache knobs are `Settings` fields: `AUDIOFP_SQLITE_CACHE_MB` and
`AUDIOFP_SQLITE_MMAP_MB`. Two more shape the write path:
`AUDIOFP_SQLITE_WRITE_BATCH_ROWS` (rows buffered before one sorted write,
default 2,000,000; `0` = immediate writes) and `AUDIOFP_SQLITE_TRACK_INDEX`
(secondary index on `track_ref` for fast deletes, default off).

Other behaviour worth knowing:

- Writes are batched: `add_track` inserts the `tracks` row (with `flushed=0`)
  and buffers the hashes; `flush()` concatenates the buffer, sorts it by
  `(hash_value, track_ref, time_offset)` (`np.lexsort`) and runs the
  `executemany` in `INSERT_SLICE` (200,000-row) slices inside one transaction,
  then marks the tracks `flushed=1`. Each B-tree page is therefore touched
  once per *batch*, not once per track, and the tree fills sequentially for the
  first batch. A flush runs when the buffer reaches `sqlite_write_batch_rows`,
  at the end of every `Indexer.index_paths` run, after every upload job, on
  `close()` and before `vacuum()`, `checkpoint()` and `unique_hash_count()`.
  Tracks left with `flushed=0` by a crash are removed (with a warning) the
  next time the store opens.
- `get_stats()` reads `COUNT(*)`, `SUM(num_hashes)` and `SUM(duration)` from the
  `tracks` table only and adds the sizes of the `.db` and `-wal` files plus the
  buffer's `pending_rows`. It never scans `fingerprints`, so `GET /api/v1/stats`
  and the UI can poll it freely. `db_size_bytes` includes the WAL, which can be
  as large as the main file again until a checkpoint; `audiofp db vacuum` runs
  `PRAGMA wal_checkpoint(TRUNCATE)` and then `VACUUM` (rewrites the whole file;
  needs the write lock and free disk).
- `unique_hash_count()` is `SELECT COUNT(DISTINCT hash_value) FROM fingerprints`,
  a full scan of the clustered index (~31 ms per 350k rows here, so tens of
  seconds on a 100M-row library, longer when it does not fit in memory). It is
  only run by `audiofp stats --full`, and only on SQLite: the CLI checks for the
  method and `PostgresStore` / `MemoryStore` do not implement it.
- `MemoryStore` keeps the index as a list of sorted segments, one per added
  track (an `argsort` of that track's hashes only), and merges them into one
  segment (a full concatenate + `argsort`) once there are more than
  `MAX_SEGMENTS` (64); deleted tracks are masked out of lookups until that
  compaction. A lookup is one `searchsorted` per segment, so interleaved
  writes and searches no longer trigger a full rebuild per query. Fine for
  tests and demos; still not for large libraries, since everything lives in
  RAM.

## When to move to PostgreSQL

SQLite is the right default for one server process on one machine; lookup
cost is a B-tree probe in both backends, so library size alone is rarely the
reason to switch. Move when:

- More than one process writes: `_write_lock` is a per-process lock, so several
  gunicorn workers (`fingerprint/api/wsgi.py`, `gunicorn --workers N`) or a
  separate `audiofp index` process next to a running server serialise only via
  SQLite's file lock and `busy_timeout`. Keep gunicorn at `--workers 1` with
  threads if you stay on SQLite.
- Several hosts need the same library; SQLite over a network filesystem is not
  supported.
- Tracks are deleted or replaced regularly at scale and you do not want to pay
  for `sqlite_track_index` (full scans in SQLite by default, indexed cascade in
  PostgreSQL).
- You need backups, replication or failover handled by the database.

`PostgresStore` (`pip install "audiofp[postgres]"`, `AUDIOFP_STORAGE_TYPE=postgres`,
`AUDIOFP_POSTGRES_DSN`) bulk-loads with `COPY ... FROM STDIN` in text format,
sending chunks of 200,000 tab-separated rows per `copy.write()` rather than one
`write_row()` call per row (throughput unmeasured), looks up with
`WHERE hash_value = ANY(%s)` in one round trip (after the `GROUP BY ... HAVING`
stop-word pre-query), and serves lookups from the covering index
`idx_fp_hash (hash_value) INCLUDE (track_ref, time_offset)`. There is no write
buffer on this backend: every track is committed as it is indexed.
Index-only scans depend on PostgreSQL's visibility map, so make sure autovacuum
keeps up after large loads. `postgres_pool_size` (default 4) bounds pooled
connections; searches and index workers share the pool. Expect several times
the disk of SQLite per row: a heap tuple plus two index entries is on the order
of 80-100 bytes per hash (estimate, unmeasured), because PostgreSQL stores the
row and the index separately whereas SQLite's clustered key is the row.

## Measuring it yourself

- `pytest -m slow tests/test_scale.py -s` writes a 30-minute WAV, fingerprints
  it while sampling RSS (needs `psutil`, in the `dev` extra) and prints peaks,
  hashes, elapsed time, memory growth and a search time.
- `audiofp stats` prints tracks, hashes, indexed duration and database size
  cheaply; `audiofp stats --full` adds the unique-hash count (slow). With
  `--json` the hash rate of your material is `total_hashes / total_duration_sec`.
- Every search response carries `processing_time_ms`, `query.num_hashes`,
  `query.truncated`, per-match `matched_rows` and the `diagnostics` funnel
  (`db_rows`, `votes`, `candidate_tracks`, `scored_tracks`,
  `skipped_common_hashes`, `dropped_for_vote_cap`); the server log line
  `Search <name>: <n>s audio, <n> hashes, <k> match(es) in <n> ms [mode=...]`
  has the headline numbers, and a `Search vote cap hit` warning appears
  whenever `max_search_votes` bit.
- `audiofp stats --json` (or `GET /api/v1/stats`) reports `pending_rows` on
  SQLite; the log line `Flushed <n> fingerprints for <k> track(s) in <n>s`
  times every batched write.
- Every indexed file logs `Indexed <name>: <n>s audio, <n> peaks, <n> hashes
  in <n>s (<track_id>)`; `audiofp index --json` prints the run summary
  (`IndexSummary.to_dict()`, including `elapsed_sec`).
- `audiofp doctor` reports whether ffmpeg is available, which decides whether
  M4A/AAC/WMA and video can be indexed at all.
