# Performance and sizing

Where AudioFP spends time and memory, what grows with what, and how to size
storage and hardware. Every number here either follows from the default
settings in `fingerprint/config.py` or was measured on one developer machine,
and the measured ones are labelled indicative. Measure your own material before
you buy hardware. The last section shows how.

The indicative figures come from Windows 11 with 24 logical CPUs, Python 3.13,
numpy 2.3 and SQLite on a local SSD. The test audio is synthetic, because the
repository ships no real recordings. The generator in `tests/conftest.py`
produces sparse, tonal material at about 14 peaks/s, well below the ~55 peaks/s
practical ceiling explained below. Real music will generally be denser.
Telephone speech with pauses will be sparser.

## Indicative figures

| What | Result | Notes |
|---|---|---|
| Fingerprint a 30-minute mono WAV (`pytest -m slow tests/test_scale.py`) | ~3.5 s, RSS growth ~40 MB | 25,474 peaks, 254,685 hashes, about 141 hashes/s. Measured while normalisation still decoded the file twice. The current single-pass code decodes and resamples from 22.05 kHz once, so expect the same or less. |
| Same file, AudioFP 1.x implementation | RSS grew to ~1.3 GB for a 1-hour file | Reported figure for the old whole-file-in-memory pipeline, which is gone from this tree, so we could not reproduce or verify it. |
| 4-s clip search against a small library, `POST /api/v1/search` | ~15-25 ms `processing_time_ms` | In-process time for decoding the clip, the lookup and scoring. The HTTP upload is excluded. Decoding was measured with the old two-pass normalisation, which is now one pass. |
| Matcher only (`Matcher.match`), 4-5 s clip vs a 10-40 minute library | 5-7 ms | 465-565 query hashes, ~100-160 rows returned. |
| 60-s query in `occurrences` mode vs a 40-minute library | ~80 ms | 8,275 query hashes, ~8,500 rows. |
| SQLite insert rate (`SQLiteStore.add_track`) | 270,000-400,000 rows/s | Measured with immediate writes: one transaction per track with pre-sorted keys, which is what `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0` does. The default now buffers 2,000,000 rows and `flush()` writes each batch in one sorted transaction, so a B-tree leaf is touched once per batch where immediate writes touch it once per track. Not re-measured. |
| SQLite bytes per fingerprint row | ~17 bytes (40-track library, after checkpoint) | Plan with 20-25 bytes. The index size section below explains why. |
| `unique_hash_count()` on 346k rows | ~31 ms | Full index scan. Grows linearly. |
| Delete one track from a 516k-row SQLite library | ~170 ms | Full scan of the fingerprint B-tree, explained in the SQLite section. `sqlite_track_index=true` makes it an index lookup. |
| Indexing 8 x 2-minute stereo 44.1 kHz WAVs, `index_workers` 1 / 2 / 4 / 8 | 430x / 770x / 1130x / 1190x real time (a re-run with different synthetic material gave 245x / 450x / 725x / 925x) | Diminishing returns beyond 4 workers. The absolute figures depend on the material. |

## Memory model: what is streaming and what accumulates

Fingerprinting never holds a whole file in memory. Memory use is flat in file
length. Only the outputs, peaks and hashes, grow with duration, and they are
small.

The decoder is `iter_audio_chunks` in `fingerprint/core/decoder.py`. It yields
float32 mono blocks of roughly `chunk_seconds` seconds at `sample_rate`.
libsndfile reads WAV/FLAC/OGG/Opus/MP3/AIFF blocks directly, and sample-rate
conversion goes through `soxr`'s streaming resampler. M4A/AAC/WMA and every
video container go through an `ffmpeg` subprocess that writes 16-bit PCM to a
pipe, already mono and resampled, so Python needs no extra buffers. ffmpeg is
optional at runtime. It is only needed for those formats, and as a fallback
when libsndfile cannot open a file, for example MP3 on a libsndfile build
without MP3 support.

STFT and peak picking happen in `PeakExtractor` in
`fingerprint/core/fingerprinter.py`. It processes each chunk as it arrives.
Between chunks it carries over only a strip of spectrogram columns
`peak_neighborhood_size` wide as context, plus an equally narrow strip of
columns that are not final yet. Chunked processing yields exactly the same
peaks as processing the whole signal at once, which
`test_chunked_peaks_identical_to_whole_signal` in `tests/test_core.py` checks.
So `chunk_seconds` is purely a memory and CPU knob:

- The transient buffers per chunk scale linearly with `chunk_seconds`: the
  decoder block, windowed frames, FFT output, magnitude block and max-filter
  mask. At the default 30 s that is a few tens of MB whatever the file length,
  and it is what the ~40 MB RSS growth above is.
- Smaller chunks use less transient memory and cost more Python-level
  iterations. `Settings.validate` requires
  `chunk_seconds * sample_rate >= 4 * n_fft`, about 0.74 s at the defaults.

What does accumulate per file: two `int32` arrays per peak and an `int64` hash
plus an `int32` anchor time per hash, so 12 bytes per hash. With
`normalize=True` there is also one `float32` magnitude per candidate peak until
`finish()`. A one-hour recording at ~141 hashes/s is ~500k hashes, about 6 MB.
`SQLiteStore.add_track` then keeps the hashes and times as two `int64` arrays,
16 bytes per row, in the process-wide write buffer. That buffer is bounded by
`sqlite_write_batch_rows`, 2,000,000 rows or about 32 MB. `flush()` first
concatenates hashes, refs and times and sorts them with `np.lexsort`. The
sorted copies are proportional to the batch: 24 bytes per row plus the sort
index, on the order of 100 MB for a full default buffer. It then converts them
to Python tuples `INSERT_SLICE` (200,000) rows at a time, so the tuple
transient is the only part that is bounded independently of the batch size.

Normalisation is single-pass. `Fingerprinter.fingerprint_file(normalize=True)`
is the default and what the indexer and the search path use, and it
peak-normalises without decoding twice. `PeakExtractor(..., normalize=True)`
tracks the running peak sample, keeps candidate peaks with their magnitudes
against the running threshold `min_amplitude * running peak`, and applies the
exact final threshold `min_amplitude * peak` in `finish()`. The result is
bit-identical to fingerprinting the peak-normalised signal, and decode,
resample and STFT each run once. For ffmpeg-backed formats that is one ffmpeg
subprocess per file. `normalize` is a keyword argument on `fingerprint_file`.
There is no `Settings` field for it and so no environment switch either.
`max_seconds` is forwarded to ffmpeg as `-t` with one second of slack, so a
truncated query never decodes more than a second of the part it will discard.

## What scales with what

### Time resolution and hash density

| Quantity | Formula (defaults) | Value |
|---|---|---|
| Frames per second | `sample_rate / hop_length` | 21.5 frames/s (one frame = 46.4 ms, exposed as `frame_seconds` by `GET /api/v1/info`) |
| Frequency bins | `n_fft / 2 + 1` | 1,025 |
| Practical peak density ceiling | `bins * frames_per_s / peak_neighborhood_size^2` | ~55 peaks/s (white noise measures 55.8) |
| Hashes per peak | up to `fan_value`, minus pairs further than `max_hash_time_delta` frames apart | 9.97 measured with `fan_value=10`. `max_hash_time_delta=200` is ~9.3 s and rarely binds. |
| Hashes per second | ~ `fan_value` x peaks/s | ~141 measured on synthetic material, ~550 practical ceiling |

`generate_hashes` pairs every peak with the next `fan_value` peaks, so the hash
count is linear in the peak count, and `min_amplitude` and
`peak_neighborhood_size` set the peak count. The practical ceiling is the
random-field density of local maxima in a square window
`peak_neighborhood_size` on a side: about one peak per
`peak_neighborhood_size^2` spectrogram cells. Broadband noise reaches it. The
hard packing limit of `scipy.ndimage.maximum_filter` is a few times higher,
since maxima can sit half a window apart when magnitudes never decrease, but
real audio does not get near it.

`sample_rate`, `n_fft`, `hop_length`, `peak_neighborhood_size`,
`min_amplitude`, `fan_value`, `min_hash_time_delta` and `max_hash_time_delta`
are all part of `Settings.fingerprint_signature()`. Change any of them, say to
trade storage for matching quality, and an existing database becomes
incompatible. With `fingerprint_compat=strict`, the default, the store refuses
to open until you run `audiofp db reset` and re-index.

### Index size

Every hash becomes one row `(hash_value, track_ref, time_offset)`. The `tracks`
table is one row per file and does not matter for sizing. In SQLite the row
lives in the clustered `WITHOUT ROWID` primary key
`(hash_value, track_ref, time_offset)`. The 36-bit hash is stored as a 4- or
6-byte integer and the two small integers as 1 to 3 bytes each. Add a record
header and a cell pointer and a packed library comes to ~17 bytes/row. Plan
with 20 to 25 bytes/row to allow for B-tree page slack after interleaved
inserts and deletes.

### Matcher memory and the "common hash" caveat

`Matcher.match` in `fingerprint/core/matcher.py` works in three stages. The
second one sets its memory use.

1. `store.query_hashes(unique_query_hashes, max_rows_per_hash=..., stats=...)`
   returns every stored row whose hash appears in the query, except the rows
   of hashes that occur more than `max_rows_per_hash` times in the library
   (default 2000, `0` disables). SQLite and PostgreSQL find those with a
   `GROUP BY hash_value HAVING COUNT(*) > ?` pre-query and the memory store
   uses `np.unique(..., return_counts=True)`. The number skipped comes back in
   `stats["skipped_hashes"]` and ends up as `diagnostics.skipped_common_hashes`.
   There is no other `LIMIT`.
2. The rows are joined to the query anchors. Each row is repeated once per
   query anchor carrying the same hash. The repeat count for each row is
   `counts`, which is `right` minus `left`, where those are the `searchsorted`
   bounds of its hash in the sorted query hashes. That produces the vote
   arrays: `offsets`, `qtimes`, `qhashes` and so on. At peak this costs roughly
   200 to 250 bytes per returned row, our estimate for the driver tuples plus
   about 8 `int64` arrays per vote. One million rows is therefore ~250 MB of
   transient memory. The join is capped at `max_search_votes`
   (default 5,000,000). When the projected vote count exceeds it, `_cap_votes`
   drops the rows of the most vote-heavy hashes first until it fits, counts
   them in `diagnostics.dropped_for_vote_cap` and logs a warning naming
   `AUDIOFP_MAX_SEARCH_VOTES`. So the vote arrays are bounded whatever the
   library contains. The rows themselves have already been fetched by then.
   The stop-word cap in stage 1 is what keeps those small.
3. Votes are grouped per track. A vectorised prefilter, one `np.unique` over
   `(track, offset)` keys, drops every track whose best raw offset bin times
   `2 * offset_tolerance_frames + 1` cannot reach `min_aligned_hashes`.
   `_score_track` runs only for the survivors, which is
   `diagnostics.scored_tracks` out of `diagnostics.candidate_tracks`. That is a
   Python loop with `argsort` and a sort-based `_count_distinct` on the
   track's votes.

The number of rows returned is the sum, over the query's distinct hashes, of
how often each one occurs in the library. The most common hash values dominate
that sum, so library size alone is a poor guide. Very repetitive material
produces the same few hash values thousands of times per track: hold music,
dial tones, IVR prompts, test tones, sustained notes. A long query with the
same kind of material repeats them on the query side too, and the join
multiplies the two. This is the one place where memory can grow well past what
the library size suggests. The code has these mitigations:

- `max_rows_per_hash` (`AUDIOFP_MAX_ROWS_PER_HASH`, default 2000) skips the
  "stop word" hashes at the lookup, before any row is returned. The skipped
  count shows per query as `diagnostics.skipped_common_hashes`. A hash shared
  by more than 2000 rows says almost nothing about which track matches, so the
  default costs nothing on ordinary material. Raise it, or set `0`, only if a
  pattern you need to find is itself that repetitive.
- `max_search_votes` (`AUDIOFP_MAX_SEARCH_VOTES`, default 5,000,000) caps the
  join in stage 2 and reports what it dropped in
  `diagnostics.dropped_for_vote_cap`. It is the last line of defence when the
  query side is repetitive too.
- `max_query_seconds` (default 3600) truncates long queries, and the response
  sets `query.truncated` when that happened.
- `min_hash_time_delta=1` drops pairs between simultaneous peaks, where
  `delta_t == 0`. On tonal material those are the harmonics of one chord and
  the most repetitive class of hash. The default `0` keeps the pairs. This is
  a fingerprint parameter, so changing it means a re-index.
- Per-track scoring is bounded. `_find_occurrences` examines at most
  `MAX_CANDIDATE_BINS = 400` offset bins per track, strongest first, and only
  bins whose smoothed vote count could pass `min_aligned_hashes` and
  `min_peak_ratio`. That caps CPU per track on repetitive material. It does
  nothing for the vote arrays from stage 2, which `max_search_votes` bounds.

`matched_rows` in every search result is the vote count for that track. The
`diagnostics` object shows the whole funnel for each query: `query_hashes`,
`db_rows`, `votes`, `candidate_tracks`, `scored_tracks`,
`skipped_common_hashes` and `dropped_for_vote_cap`.

## Sizing table

Storage per hour of indexed audio at default settings. Rows per hour is the
measured or estimated hash rate times 3,600, and the byte columns assume the
20 to 25 bytes/row planning figure for SQLite. Treat every row as an estimate.
The hash rate of your own material is the only number that matters, and
`audiofp stats --json` gives you the inputs for it,
`total_hashes / total_duration_sec`.

| Material | Hashes/s | Rows per hour | SQLite per hour | SQLite per 1,000 hours | Basis |
|---|---|---|---|---|---|
| Sparse tonal audio with silence (Windows system sounds) | ~30 | ~110k | 2-3 MB | 2-3 GB | measured |
| Telephone speech with pauses | 50-150 | 180k-540k | 4-14 MB | 4-14 GB | estimate, unmeasured |
| Synthetic pseudo-music from the test-suite | ~141 | ~510k | 10-13 MB | 10-13 GB | measured |
| Dense music (planning figure) | ~300 | ~1.1M | 22-27 MB | 22-27 GB | estimate, unmeasured |
| Broadband noise (practical ceiling) | ~550 | ~2.0M | 40-50 MB | 40-50 GB | measured, and it matches the derived random-field density |

RAM for search: the hot part of the SQLite B-tree should sit in the OS page
cache or the mmap window (`sqlite_mmap_mb`). Lookups are random reads across
the whole key range, so a library much larger than RAM becomes disk-bound on
the first touch of each page. Add the matcher's transient memory from above
for each concurrent search.

## Indexing throughput

Per file the pipeline is an optional SHA-256 pass, then one decode pass that
also does the STFT, peaks and hashes (normalisation is folded into the same
pass). After that a short SQLite transaction writes the `tracks` row and the
hashes go onto the in-memory write buffer. One sorted write transaction runs per
`sqlite_write_batch_rows` rows, which is 2,000,000 by default or about 4 hours
of audio at ~141 hashes/s, and one more at the end of the run. On the
measurement machine a single worker fingerprints WAV at several hundred times
real time. The store insert for an hour of audio, ~500k rows, took about 1.5 to
2 s when measured with immediate per-track writes, `sqlite_write_batch_rows=0`.
Batching moves that cost to the flush, where each B-tree leaf is visited once
per batch. We have not re-measured it.

| Lever | Where | Effect |
|---|---|---|
| `index_workers` (`AUDIOFP_INDEX_WORKERS`, `audiofp index --workers N`) | `Settings.effective_index_workers`; `0` = `min(4, cpu_count)` | Files are fingerprinted concurrently on one `ThreadPoolExecutor`, `Indexer.executor`. Measured 430x / 770x / 1130x / 1190x real time for 1 / 2 / 4 / 8 workers. The re-run gave 245x / 450x / 725x / 925x. |
| Why threads scale at all | numpy FFT/sort/ufuncs, libsndfile, soxr and the ffmpeg pipe release the GIL | The heavy parts run in parallel. The Python glue does not: the chunk loop, `tolist()` and the `executemany` at flush time. Hence the plateau. |
| Why writes do not scale | `SQLiteStore._write_lock` | `add_track`, which writes the `tracks` row, and `flush()`, the batched fingerprint write, are serialised per process. While a flush runs the other workers wait at their next `add_track`. Fingerprinting dominates, so this is rarely the bottleneck, but it is the ceiling. `sqlite_write_batch_rows` sets how often the pause happens and how long it lasts. |
| `sqlite_write_batch_rows` (default 2,000,000) | `SQLiteStore.flush()` | Rows buffered before one sorted write. `0` writes every track immediately, which is how 2.0 behaved before batching and is useful when durability per file matters more than throughput. Buffered tracks are searchable, and `GET /stats` shows the buffer as `pending_rows`. |
| Submission window | `Indexer.index_paths` keeps at most `2 x workers` files in flight | Bounds memory for 100,000-file runs and makes cancellation prompt. |
| `max_concurrent_jobs` (default 2) | `JobManager` has its own small pool for job runners | All jobs share the one indexer pool. Raising this adds no fingerprint parallelism, it just lets more jobs make progress at the same time. |
| `dedupe` (default `content`) | `Indexer.index_file` | `content` streams the file once through `sha256_file` in 1 MB blocks, about 20 ms for a 10 MB WAV and negligible next to fingerprinting, then looks up `idx_tracks_content_hash`. `path` is only an index lookup on `idx_tracks_filepath`. `none` skips the check. |
| Source format | `decoder.select_backend` | libsndfile formats decode in-process. ffmpeg formats pay one subprocess spawn per file, with ffmpeg doing the mono mix and resample. |
| Source sample rate | `soxr` HQ streaming resampler | Anything not already at `sample_rate` (11025) is resampled once. Native 11025 Hz material skips this. |
| Identical files in one batch | `Indexer._store_lock` | The duplicate check runs again under a lock right before `add_track`, so when several workers fingerprint byte-identical files only one track is stored. The others still cost their fingerprinting time and end as `duplicate`. |
| Failure isolation | `IndexOutcome` per file | A bad file costs only its own decode attempt and the run continues. |

Deleting or replacing a track in SQLite deletes its fingerprint rows by
`track_ref`. By default the only index on that table is the primary key, which
is led by `hash_value`. So each such statement scans the whole fingerprint
B-tree while holding the write lock: ~170 ms for 0.5M rows and linear from
there. `delete_track` and `delete_tracks` issue
`DELETE ... WHERE track_ref IN (...)` for up to 500 refs at a time in one
transaction, so a bulk delete costs one scan per 500 tracks. `add_track` with
an existing `track_id`, which is a replace, still runs one `WHERE track_ref=?`
scan. Rows of tracks still in the write buffer are just dropped from the
buffer. `sqlite_track_index=true` (`AUDIOFP_SQLITE_TRACK_INDEX`) adds
`idx_fp_track ON fingerprints (track_ref)`. Every delete then becomes an index
lookup, and the disk footprint of the fingerprint table roughly doubles. Turn
it on for libraries where tracks are deleted or replaced routinely. PostgreSQL
always has `idx_fp_track` and `ON DELETE CASCADE`, so deletes there are always
indexed.

## Search latency

`Runtime.search_file` fingerprints the query with
`max_seconds=max_query_seconds` and calls `Matcher.match`.
`processing_time_ms` in the response covers both. It leaves out the multipart
upload itself, which `POST /api/v1/search` saves to a temp file first.

| Lever | Effect |
|---|---|
| Query length | Fingerprinting cost and hash count are linear in seconds, ~141 hashes/s on the test material. An hour-long recording is ~7 s of fingerprinting at the rate measured above, plus a larger lookup. `max_query_seconds` caps it. |
| Library hash distribution | The rows returned by `query_hashes` drive lookup time, join memory and the number of tracks scored (see the common-hash caveat above). `max_rows_per_hash` and `max_search_votes` bound them, and `diagnostics` in the response shows the counts. |
| Mode | `identify` stops after the best offset per track. `occurrences` examines up to `MAX_CANDIDATE_BINS` bins per track and returns up to `max_occurrences_per_track`, default 25 and at most 500, which the API exposes as the form field `max_occurrences`. |
| Thresholds | A higher `min_aligned_hashes` rejects more tracks in the vectorised prefilter, before any per-track Python work, and at the cheap early check in `_score_track`. Both it and `min_peak_ratio` shrink the candidate-bin set in `_find_occurrences`. Both can be changed per request or through `PUT /api/v1/settings` without re-indexing. |
| `top_k` | Only affects the final `get_tracks_by_ref` metadata fetch. Negligible. |
| SQLite page cache and mmap | `sqlite_cache_mb`, `sqlite_mmap_mb` (below). The first search after start-up is cold. |
| Concurrency | The production server runs `server_threads` waitress threads, 8 by default. Each has its own SQLite connection and the numpy parts run in parallel. Searches are otherwise single-threaded. |

The lookup itself is one statement. `SQLiteStore.query_hashes` loads the
distinct query hashes into a `TEMP` table and joins it against `fingerprints`.
The planner runs that as one primary-key probe per query hash,
`SEARCH f USING PRIMARY KEY (hash_value=?)`. With `max_rows_per_hash` set,
which is the default, a `GROUP BY ... HAVING COUNT(*) > ?` pre-query over the
same join removes the stop-word hashes from the temp table first. Each query
hash is probed twice that way, but the heavy ones are never materialised. Rows
still in the write buffer are matched in memory with `np.isin` per buffered
track and appended. There is no chunking by SQLite's variable limit, so a
1-hour query is still a single query. `PostgresStore` does the same with
`WHERE hash_value = ANY(%s)`, a `GROUP BY hash_value HAVING COUNT(*) > %s`
pre-query and the covering index.

## SQLite tuning

`SQLiteStore._connect` sets these pragmas on every connection:

| Pragma | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Readers do not block the single writer, so searches keep working while indexing runs. |
| `synchronous` | `NORMAL` | Safe against process crashes with WAL, and avoids an fsync per transaction. |
| `cache_size` | `-(sqlite_cache_mb * 1024)` KiB, default 64 MB | Page cache per connection, allocated on demand. Connections are per thread (`threading.local`), so the worst case is roughly `(server_threads + index_workers) x sqlite_cache_mb`. |
| `temp_store` | `MEMORY` | The per-query `query_hashes` temp table never touches disk. |
| `mmap_size` | `sqlite_mmap_mb` MB, default 256 MB | Reads the database through the OS page cache without copying pages into SQLite's own cache. Shared between connections, so it is not multiplied. For read-heavy deployments set it to at least the size of your `fingerprints.db`. `0` disables it. |
| `busy_timeout` | 30 s (constructor default, there is no setting for it) | Other processes touching the same file wait up to that long before they get `SQLITE_BUSY`. |
| `foreign_keys` | `ON` | The v3 schema declares no foreign keys, so this has no effect and no cost. |

The two cache knobs are `Settings` fields, `AUDIOFP_SQLITE_CACHE_MB` and
`AUDIOFP_SQLITE_MMAP_MB`. Two more shape the write path.
`AUDIOFP_SQLITE_WRITE_BATCH_ROWS` is the number of rows buffered before one
sorted write, default 2,000,000, and `0` means immediate writes.
`AUDIOFP_SQLITE_TRACK_INDEX` adds the secondary index on `track_ref` for fast
deletes and is off by default.

Other behaviour worth knowing:

- Writes are batched. `add_track` inserts the `tracks` row with `flushed=0`
  and buffers the hashes. `flush()` concatenates the buffer, sorts it by
  `(hash_value, track_ref, time_offset)` with `np.lexsort`, runs the
  `executemany` in `INSERT_SLICE` slices of 200,000 rows inside one
  transaction, then marks the tracks `flushed=1`. Each B-tree page is touched
  once per batch, and the tree fills sequentially for the first batch. A flush
  runs when the buffer reaches `sqlite_write_batch_rows`, at the end of every
  `Indexer.index_paths` run, after every upload job, on `close()`, and before
  `vacuum()`, `checkpoint()` and `unique_hash_count()`. Tracks a crash leaves
  at `flushed=0` are removed, with a warning, the next time the store opens.
- `get_stats()` reads `COUNT(*)`, `SUM(num_hashes)` and `SUM(duration)` from
  the `tracks` table only, and adds the sizes of the `.db` and `-wal` files
  plus the buffer's `pending_rows`. It never scans `fingerprints`, so
  `GET /api/v1/stats` and the UI can poll it freely. `db_size_bytes` includes
  the WAL, which can grow to the size of the main file again until a
  checkpoint. `audiofp db vacuum` runs `PRAGMA wal_checkpoint(TRUNCATE)` and
  then `VACUUM`. That rewrites the whole file, so it needs the write lock and
  free disk.
- `unique_hash_count()` is `SELECT COUNT(DISTINCT hash_value) FROM fingerprints`,
  a full scan of the clustered index. That was ~31 ms per 350k rows here, so
  tens of seconds on a 100M-row library and longer when it does not fit in
  memory. Only `audiofp stats --full` runs it, and only on SQLite: the CLI
  checks for the method, and `PostgresStore` and `MemoryStore` do not
  implement it.
- `MemoryStore` keeps the index as a list of sorted segments, one per added
  track, each an `argsort` of that track's hashes only. Once there are more
  than `MAX_SEGMENTS` (64) it merges them into one segment with a full
  concatenate and `argsort`. Deleted tracks are masked out of lookups until
  that compaction. A lookup is one `searchsorted` per segment, so interleaving
  writes and searches does not force a full rebuild per query. Fine for tests
  and demos. Still not for large libraries, since everything lives in RAM.

## When to move to PostgreSQL

SQLite is the right default for one server process on one machine. Lookup
cost is a B-tree probe in both backends, so library size alone is rarely the
reason to switch. Move when:

- More than one process writes. `_write_lock` is a per-process lock, so
  several gunicorn workers (`fingerprint/api/wsgi.py`, `gunicorn --workers N`),
  or a separate `audiofp index` process next to a running server, serialise
  only through SQLite's file lock and `busy_timeout`. If you stay on SQLite,
  keep gunicorn at `--workers 1` and use threads.
- Several hosts need the same library. SQLite over a network filesystem is not
  supported.
- Tracks are deleted or replaced regularly at scale and you do not want to pay
  for `sqlite_track_index`. SQLite does full scans by default and PostgreSQL
  has an indexed cascade.
- You need backups, replication or failover handled by the database.

`PostgresStore` needs `pip install "audiofp[postgres]"`,
`AUDIOFP_STORAGE_TYPE=postgres` and `AUDIOFP_POSTGRES_DSN`. It bulk-loads with
`COPY ... FROM STDIN` in text format, sending chunks of 200,000 tab-separated
rows per `copy.write()` rather than calling `write_row()` once per row. We
have not measured the throughput. Lookups are one round trip with
`WHERE hash_value = ANY(%s)`, after the `GROUP BY ... HAVING` stop-word
pre-query, and are served from the covering index
`idx_fp_hash (hash_value) INCLUDE (track_ref, time_offset)`. There is no write
buffer on this backend, so every track is committed as it is indexed.
Index-only scans depend on PostgreSQL's visibility map, so make sure autovacuum
keeps up after large loads. `postgres_pool_size` defaults to 4 and bounds the
pooled connections, and searches and index workers share the pool. Expect
several times the disk of SQLite per row, because PostgreSQL stores the row and
the index separately while SQLite's clustered key is the row. A heap tuple plus
two index entries is on the order of 80 to 100 bytes per hash (our estimate,
unmeasured).

## Measuring it yourself

- `pytest -m slow tests/test_scale.py -s` writes a 30-minute WAV, fingerprints
  it while sampling RSS, which needs `psutil` from the `dev` extra, and prints
  peaks, hashes, elapsed time, memory growth and a search time.
- `audiofp stats` prints tracks, hashes, indexed duration and database size,
  and it is cheap. `audiofp stats --full` adds the unique-hash count, which is
  slow. With `--json` the hash rate of your material is
  `total_hashes / total_duration_sec`.
- Every search response carries `processing_time_ms`, `query.num_hashes`,
  `query.truncated`, per-match `matched_rows` and the `diagnostics` funnel:
  `db_rows`, `votes`, `candidate_tracks`, `scored_tracks`,
  `skipped_common_hashes` and `dropped_for_vote_cap`. The server log line
  `Search <name>: <n>s audio, <n> hashes, <k> match(es) in <n> ms [mode=...]`
  has the headline numbers. A `Search vote cap hit` warning appears whenever
  `max_search_votes` bit.
- `audiofp stats --json`, or `GET /api/v1/stats`, reports `pending_rows` on
  SQLite. The log line `Flushed <n> fingerprints for <k> track(s) in <n>s`
  times every batched write.
- Every indexed file logs `Indexed <name>: <n>s audio, <n> peaks, <n> hashes
  in <n>s (<track_id>)`. `audiofp index --json` prints the run summary from
  `IndexSummary.to_dict()`, which includes `elapsed_sec`.
- `audiofp doctor` reports whether ffmpeg is available, which decides whether
  M4A/AAC/WMA and video can be indexed at all.
