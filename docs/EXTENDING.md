# Extending AudioFP

This guide is for developers who want to build on AudioFP rather than just run it:
embed it in a Python program, script pattern searches over call recordings, add a
storage backend, an endpoint or a background job type, or prepare the ground for a
transcript search engine next to the fingerprint engine.

The package is a pipeline (`fingerprint/__init__.py`):

| Package | Role |
|---|---|
| `fingerprint.core` | decoding (`decoder`), peak extraction and hashing (`fingerprinter`, `hash_generator`), matching (`matcher`) |
| `fingerprint.storage` | `StorageBackend` contract, `MemoryStore`, `SQLiteStore`, `PostgresStore`, `create_storage()` |
| `fingerprint.indexing` | folder scanning, `Indexer`, progress tracking |
| `fingerprint.jobs` | `JobManager` / `Job` (bounded thread pool, cancellation, JSON persistence) |
| `fingerprint.api` | `Runtime` (wires everything), Flask app factory, `/api/v1` blueprint, bundled UI |
| `fingerprint.cli` | the `audiofp` command (also `python -m fingerprint`) |
| `fingerprint.config` | `Settings` dataclass; every field is overridable with `AUDIOFP_<FIELD_UPPER>` |

Everything below names real classes, functions and fields. Where a section describes
something that does *not* exist yet (the transcript search engine), it says so.

---

## Using AudioFP as a library

Install the package (`pip install -e .`) and import from `fingerprint`. The core,
indexing and storage packages do not import Flask; importing
`fingerprint.api.runtime.Runtime` pulls it in, because the `api` package's `__init__`
imports the app factory.

### Settings

`Settings.load()` resolves configuration in layers: field defaults, then the profile
(`development` / `production` / `testing`), then `AUDIOFP_*` environment variables
(optionally from a `.env` file), then explicit keyword overrides. It calls
`Settings.validate()` and raises `ConfigurationError` on bad values or unknown
override names.

```python
from fingerprint.config import Settings

# Reproducible: ignore the process environment and .env entirely.
settings = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path="data/qa.db")

# Or honour AUDIOFP_* variables (and ./.env) like the CLI does:
settings = Settings.load()
```

Fields flagged `fingerprint=True` in `fingerprint/config.py` (`sample_rate`, `n_fft`,
`hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`,
`min_hash_time_delta`, `max_hash_time_delta`) plus `FINGERPRINT_ALGORITHM_VERSION`
form the **fingerprint signature** (`settings.fingerprint_signature()`). A database is
stamped with it on first use; every process that reads or writes that database must
use the same values. `audiofp config --describe` prints the full option table.

The library does not configure logging for you. Call
`fingerprint.utils.logging.configure_logging(level, fmt, log_file)` if you want the
`fingerprint.*` loggers on stderr (the CLI and `create_app()` do this).

### Storage

```python
from fingerprint.storage import create_storage

store = create_storage(settings)          # MemoryStore | SQLiteStore | PostgresStore
...
store.close()
```

`create_storage(settings, *, check_compat=True)` picks the backend from
`settings.storage_type` and calls `store.initialize(signature, params, settings.fingerprint_compat)`.
On a signature mismatch it raises `FingerprintCompatibilityError` (`fingerprint_compat="strict"`,
the default), logs a warning (`"warn"`) or continues (`"ignore"`). Pass
`check_compat=False` only for maintenance code such as `audiofp db reset`.

### Indexing

```python
from fingerprint.indexing import Indexer

indexer = Indexer(settings, store)        # builds its own Fingerprinter; pass one to share it
outcome = indexer.index_file(
    "patterns/disclaimer.wav",
    title="Recorded-line disclaimer",
    tags=["compliance", "disclaimer"],
    metadata={"phrase": "this call may be recorded", "version": 3},
)
print(outcome.status)                     # "indexed" | "duplicate" | "failed"
```

`Indexer.index_file()` never raises for a problem with the *file* (unsupported
extension, undecodable audio, zero hashes): it returns an `IndexOutcome` with
`status="failed"`, `error` and `error_code`. It does raise `StorageError` when the
backend fails, because retrying other files would not help. Other details:

- Duplicate detection follows `settings.dedupe`: `"content"` (SHA-256 of the file,
  default), `"path"` or `"none"`. A duplicate outcome carries `duplicate_of` (the
  existing `track_id`) and `track` (the existing record).
- `metadata` is stored as `{"source": source, **metadata}`, so a `source` key of
  your own overrides the default (`"file"` for `index_file`, the API uses `"upload"`
  and `"directory"`, the CLI `"cli"`).
- `title`/`artist` fall back to `metadata_from_filename()` (`Artist - Title.ext`).
- `index_paths(paths, tags=..., progress=..., should_cancel=...)` and
  `index_directory(directory, recursive=True, ...)` run many files on a thread pool
  (`settings.effective_index_workers`) and return an `IndexSummary`; they call
  `storage.flush()` at the end of the run, and a `StorageError` from any file stops
  the run and is re-raised as `StorageError("Indexing stopped after N file(s): ...")`.
  Byte-identical files inside one batch yield a single track: the duplicate check is
  repeated under the indexer's `_store_lock` right before `add_track()`. Call
  `indexer.close()` when done to shut the pool down.

### Fingerprinting

```python
from fingerprint.core import Fingerprinter

fingerprinter = Fingerprinter(settings)
fp = fingerprinter.fingerprint_file("calls/2026-09-01-0001.wav")   # streaming, flat memory
fp = fingerprinter.fingerprint_array(samples)                      # numpy array, already at settings.sample_rate
```

- `fingerprint_file(path, *, max_seconds=None, normalize=True, backend="auto", display_name=None)`
  decodes in `settings.chunk_seconds` chunks, once. With `normalize=True` the result is
  exactly the fingerprint of the peak-normalised recording, computed in the same pass
  (`PeakExtractor` tracks the peak sample and thresholds against `min_amplitude * peak`;
  `fp.gain` reports the implied `1 / peak`). `fingerprint_chunks(chunks, normalize=True, extra=None)`
  is the underlying entry point for your own chunk stream. `backend` is `"auto"`,
  `"soundfile"` or `"ffmpeg"`. WAV/FLAC/OGG/Opus/MP3 (and the other `NATIVE_AUDIO_EXTENSIONS` in
  `fingerprint/formats.py`) are decoded by libsndfile; video containers and
  M4A/AAC/WMA need ffmpeg on `PATH` (`FFmpegNotFoundError` otherwise).
- `fingerprint_array(audio, normalize=True)` accepts `(n,)`, `(n, ch)` or `(ch, n)`
  float arrays and **assumes they are already at `settings.sample_rate`** (11025 Hz by
  default). Resample first (the test-suite uses `soxr`).
- The result is a `Fingerprint` with `hashes` (int64), `hash_times` (int32 anchor
  frames), `peak_times`, `peak_freqs`, `num_samples`, `sample_rate`, `hop_length`, and
  the helpers `num_hashes`, `duration_sec`, `frames_to_seconds(frames)`.
  One frame is `hop_length / sample_rate` seconds (about 46 ms with the defaults).

### Matching

```python
from fingerprint.core import Matcher, MatchOptions

matcher = Matcher(settings)
options = MatchOptions.from_settings(settings, mode="occurrences", top_k=50)
matches = matcher.match(fp, store, options)     # list[TrackMatch], strongest first
```

`MatchOptions` fields and their dataclass defaults (the `from_settings()` classmethod
takes the thresholds from `Settings` instead, applies keyword overrides such as
`mode=`, then clamps):

| Field | Default | Meaning |
|---|---|---|
| `mode` | `"identify"` | `"identify"` = best alignment per track; `"occurrences"` = every alignment above the thresholds per track |
| `top_k` | `5` | Maximum number of **tracks** returned (clamped to `settings.max_top_k`, default 50) |
| `min_aligned_hashes` | `10` | Distinct query hashes that must vote for one offset |
| `min_confidence` | `0.02` | `aligned_hashes / distinct query hashes inside the matched span` |
| `min_peak_ratio` | `12.0` | Spike height over the histogram background; chance matches sit around 3-9 |
| `offset_tolerance_frames` | `1` | Adjacent offset bins merged when scoring (clip-start jitter) |
| `max_occurrences_per_track` | `25` | Cap per track in occurrences mode |
| `max_rows_per_hash` | `2000` | Query hashes stored more than this many times in the library are skipped as stop words (`0` disables); passed to `store.query_hashes()` |
| `max_search_votes` | `5_000_000` | Budget for the row x anchor join; the most common hashes are dropped first when it would be exceeded |

After every `match()` call, `matcher.last_diagnostics` (a `MatchDiagnostics`) holds
`query_hashes`, `db_rows`, `votes`, `candidate_tracks`, `scored_tracks`,
`skipped_common_hashes` and `dropped_for_vote_cap`; `to_dict()` is what the API returns
as `diagnostics`.

`top_k` matters for pattern search: if you index 30 compliance phrases and search one
call with the default `top_k=5`, at most five phrases can be reported. Raise it (up to
`AUDIOFP_MAX_TOP_K`).

Result objects (`fingerprint/core/matcher.py`):

| `TrackMatch` field | Meaning |
|---|---|
| `track_ref` / `track` | Internal integer ref and the `TrackRecord` (title, filename, tags, metadata, ...) |
| `aligned_hashes`, `confidence`, `peak_ratio`, `offset_frames` | Copied from the strongest occurrence |
| `matched_rows` | Raw hash votes for this track before scoring |
| `occurrences` | `list[Occurrence]`; `occurrences[0]` (also `match.best`) is the strongest, the rest are sorted by `offset_frames` |

| `Occurrence` field | Meaning |
|---|---|
| `offset_frames` | `track_frame - query_frame`; **negative** means the track's audio lies inside the query |
| `aligned_hashes`, `confidence`, `peak_ratio` | Scores for this alignment |
| `query_start_frames`, `query_end_frames` | Span of query frames covered by the aligned hashes (outliers trimmed) |
| `track_start_frames`, `track_end_frames` | Properties: the query span shifted by `offset_frames` |

Convert frames with `fp.frames_to_seconds(...)`. `quality_label(confidence, peak_ratio)`
returns `"strong"` (confidence >= 0.15 and peak_ratio >= 30), `"likely"` (>= 0.05 and
>= 18) or `"weak"`; the UI and CLI use it.

### Complete example

Indexes a folder of short patterns with tags, then searches one call recording in
occurrences mode and prints a timeline. Works with the SQLite backend and no server.

```python
"""Index compliance patterns, then find every occurrence inside one call."""

from pathlib import Path

from fingerprint.config import Settings
from fingerprint.core import Fingerprinter, Matcher, MatchOptions, quality_label
from fingerprint.indexing import Indexer
from fingerprint.storage import create_storage

settings = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path="data/patterns.db")
store = create_storage(settings)
fingerprinter = Fingerprinter(settings)
indexer = Indexer(settings, store, fingerprinter)
matcher = Matcher(settings)

try:
    for path in sorted(Path("patterns").glob("*.wav")):
        outcome = indexer.index_file(str(path), tags=["compliance"], metadata={"phrase": path.stem})
        print(f"{outcome.status:9} {path.name} {outcome.error or ''}")

    query = fingerprinter.fingerprint_file("calls/call-0001.wav")
    options = MatchOptions.from_settings(settings, mode="occurrences", top_k=settings.max_top_k)
    timeline = []
    for match in matcher.match(query, store, options):
        for occ in match.occurrences:
            timeline.append(
                {
                    "start_sec": query.frames_to_seconds(occ.query_start_frames),
                    "end_sec": query.frames_to_seconds(occ.query_end_frames),
                    "pattern": match.track.title,
                    "tags": match.track.tags,
                    "confidence": occ.confidence,
                    "peak_ratio": occ.peak_ratio,
                    "quality": quality_label(occ.confidence, occ.peak_ratio),
                }
            )
    for row in sorted(timeline, key=lambda r: r["start_sec"]):
        print(f"{row['start_sec']:8.2f}-{row['end_sec']:8.2f}s  {row['pattern']:<32} {row['quality']:<7} conf={row['confidence']:.3f}")
finally:
    indexer.close()
    store.close()
```

### Getting the API's JSON shape without the server

`Runtime` is the object the Flask app and the CLI share. `Runtime.search_file()` applies
the persisted runtime defaults (`<data_dir>/runtime-settings.json`, editable through
`PUT /api/v1/settings`), truncates queries at `settings.max_query_seconds` and returns a
`SearchResult`; `format_search()` turns it into exactly the JSON `POST /api/v1/search`
and `audiofp search --json` emit.

```python
from fingerprint.api.responses import format_search
from fingerprint.api.runtime import Runtime

rt = Runtime(settings)                    # creates storage, fingerprinter, matcher, indexer, job manager
try:
    payload = format_search(rt.search_file("calls/call-0001.wav", mode="occurrences", top_k=50))
finally:
    rt.close()
```

---

## Pattern search recipes (call-center QA)

The building block is occurrences mode with the roles reversed from music
identification: **index the short things you are looking for, query with the long
recording.** Each call is fingerprinted once and compared against every pattern in one
pass; the library stays small (one track per phrase, jingle or hold-music loop).

### 1. Build the pattern library

Cut each pattern to a clean clip (the disclaimer as actually played, the IVR greeting,
one loop of the hold music) and index it with tags that describe what it is. The
test-suite works with 3-second patterns; very short or near-silent clips produce few
hashes and cannot pass `min_aligned_hashes`.

```bash
audiofp index ./patterns/compliance --tags "compliance,disclaimer" --json > indexed-compliance.json
audiofp index ./patterns/ivr        --tags "ivr,greeting"
audiofp index ./patterns/hold       --tags "hold-music"
```

`--json` prints the `IndexSummary` (`indexed`, `duplicates`, `failed`, `track_ids`,
`errors`, `duplicates_of`). Through the API, `POST /api/v1/tracks` takes the same
information as form fields (`audio`, `title`, `artist`, `tags` comma-separated,
`metadata` as a JSON string) and returns `202` with a job to poll;
`POST /api/v1/tracks/index-directory` takes `{"directory_path": ..., "recursive": true, "tags": [...]}`.

### 2. Search each recording in occurrences mode

```bash
audiofp search calls/call-0001.wav --mode occurrences --top-k 50
```

```
Query: call-0001.wav  612.4s, 41210 peaks, 389560 hashes  (1450.2 ms)

1. Recorded-line disclaimer  [strong]  confidence=0.412  aligned=188  peak_ratio=61.3
   - track content found in query at 0:12-0:16 (track 0:00-0:04; aligned 188, strong)
```

`--top-k` counts tracks, so set it to at least the number of patterns you index. Over
HTTP the same call is `POST /api/v1/search` with form fields `audio`, `mode=occurrences`,
`top_k`, and optionally `min_confidence`, `min_aligned_hashes`, `min_peak_ratio`,
`max_occurrences` (1-500). Calls longer than `settings.max_query_seconds` (3600 s by
default) are truncated and the response sets `query.truncated=true`.

### 3. Interpreting offsets and spans

`offset_frames` is `track_frame - query_frame`. With a short indexed pattern and a long
query, the pattern starts *after* the recording does, so the offset is **negative** and
the pattern sits inside the recording at `-offset`. The JSON produced by
`fingerprint/api/responses.py` spells this out per occurrence:

| Field | Meaning |
|---|---|
| `offset_sec` | Signed `track time - query time` |
| `query_offset_sec` | `max(0, -offset_sec)`: where the pattern starts inside the recording |
| `track_offset_sec` | `max(0, offset_sec)`: where the query starts inside the track (identify mode) |
| `query_start_sec`, `query_end_sec` | Span of the **recording** covered by aligned hashes - the timeline fields |
| `track_start_sec`, `track_end_sec` | Span of the **pattern** that matched (near 0 to the pattern length for a full hit) |
| `aligned_hashes`, `confidence`, `peak_ratio`, `quality` | Scores; `quality` is `strong` / `likely` / `weak` |

Per-call timeline rule: take every occurrence of every match, sort by
`query_start_sec`. Notes that matter when you turn this into QA verdicts:

- `query_end_sec` runs slightly short of the true end of the pattern, because the span
  is measured on hash *anchors* and the last anchors of a pattern pair with peaks
  outside it (the test `test_occurrences_mode_finds_pattern_twice_and_negative_offsets`
  pins this behaviour). Do not compare spans to the pattern length with a tight
  tolerance.
- `confidence` is normalised by the query hashes *inside the matched span*, so a
  4-second pattern in a 10-minute call can still score highly. It is not "fraction of
  the whole call".
- Occurrences of the same pattern at most `2 * offset_tolerance_frames + 1` frames
  apart, or covering the same query and track region, are collapsed into one (the
  stronger candidate is kept).
- The same phrase indexed twice (two takes of the disclaimer) shows up as two tracks
  matching the same span. Group by tag (or by your own `metadata` key) rather than by
  track when you count.
- `max_occurrences_per_track` (25) caps how many hits per pattern are reported; hold
  music that loops for ten minutes will hit the cap. Raise `AUDIOFP_MAX_OCCURRENCES_PER_TRACK`
  or pass `max_occurrences` per request.
- `track_start_sec` / `track_end_sec` tell you *how much* of the pattern was heard: a
  disclaimer cut off half-way matches only its first seconds.

### 4. Batch script using the CLI `--json` output

One JSON line per occurrence, sorted per call, using `jq`:

```bash
#!/usr/bin/env bash
# Search every recording for every indexed pattern and build timeline.jsonl.
set -u
: > timeline.jsonl
for call in calls/*.wav; do
  # Exit status is 1 when nothing matched - do not let that stop the loop.
  audiofp search "$call" --mode occurrences --top-k 50 --json -q > result.json || true
  jq -c --arg call "$call" '
    [ .matches[] as $m
      | $m.occurrences[]
      | {call: $call, track_id: $m.track_id, pattern: $m.title, tags: $m.tags,
         start_sec: .query_start_sec, end_sec: .query_end_sec,
         confidence, peak_ratio, quality} ]
    | sort_by(.start_sec)[]' result.json >> timeline.jsonl
done

# Calls with no "disclaimer"-tagged hit of at least "likely" quality:
jq -r 'select((.tags | index("disclaimer")) and .quality != "weak") | .call' timeline.jsonl | sort -u > compliant.txt
comm -23 <(ls calls/*.wav | sort) compliant.txt
```

Things to know before relying on this:

- `audiofp search --json` exits `0` on a match, `1` when `found` is `false` or an
  AudioFP error occurred, and `2` for usage errors such as a missing clip path. Log
  lines go to stderr, so stdout is clean JSON; `-q` lowers the log level to WARNING.
- Pass `--mode occurrences` explicitly. When `--mode` is omitted the CLI uses the mode
  stored in `<data_dir>/runtime-settings.json` (whatever was last set via the UI or
  `PUT /api/v1/settings`), which is `identify` by default.
- The CLI flags are `--mode`, `--top-k`, `--min-confidence`, `--min-aligned`,
  `--min-peak-ratio`, `--json`, plus the common `--profile`, `--data-dir`, `--storage`,
  `--sqlite-path`, `--log-level`, `--quiet`. There is no `--max-occurrences` flag; the
  per-track cap comes from `settings.max_occurrences_per_track`.
- Every `audiofp search` invocation builds a full `Runtime` (opens the database, creates
  the job and index thread pools). For thousands of calls, the Python loop from the
  library example above, or the API, is much cheaper.

### 5. Custom metadata and tags per track

Tags are normalised by `normalize_tags()` in `fingerprint/storage/base.py`: lower-cased,
trimmed, de-duplicated, at most 50 per track; a comma-separated string or a list is
accepted. Metadata is a free-form JSON object.

| Where | Tags | Metadata |
|---|---|---|
| CLI `audiofp index` | `--tags "compliance,disclaimer"` (applied to every file of the run) | not available on the CLI |
| `Indexer.index_file()` | `tags=[...]` | `metadata={...}` |
| `POST /api/v1/tracks` (multipart) | `tags=compliance,disclaimer` | `metadata={"phrase": "...", "script_version": 3}` sent as a JSON **string** form field |
| `POST /api/v1/tracks/index-directory` (JSON) | `"tags": ["batch"]` | not available |
| `PATCH /api/v1/tracks/{track_id}` (JSON) | `{"tags": ["qa", "disclaimer"]}` (replaces the list) | `{"metadata": {...}}` (replaces the object; max 64 KB, enforced by `validate_track_changes()`) |

Only `title`, `artist`, `tags` and `metadata` can be edited after indexing; anything
else is rejected with `validation_error`. Reading them back:

- `GET /api/v1/tracks?tag=disclaimer` filters by one exact tag (also `q`, `sort`,
  `order`, `source_type`, `page`, `per_page`); `GET /api/v1/tags` lists distinct tags with
  counts. In Python: `store.list_tracks(tag="disclaimer")` returns `(page, total)`.
- `audiofp tracks list --q disclaimer --json` searches a substring across title, artist,
  filename and tags; the CLI has no exact `--tag` filter.
- **Search results cannot be filtered by tag server-side.** Every match carries the
  track's `tags` and `track_id` (and `TrackMatch.track.metadata` in Python), so filter
  client-side as the script above does. If you want a hard split (say, patterns for
  two different clients), use separate databases (`AUDIOFP_SQLITE_PATH`) rather than tags.

---

## Adding a storage backend

`fingerprint/storage/base.py` defines the contract. A backend stores tracks (metadata
addressed by an opaque `track_id`, plus a small integer `ref` that keeps the
fingerprint table compact) and the inverted index `hash -> (track_ref, frame)`.
`MemoryStore` (about 200 lines) is the easiest reference implementation to read;
`SQLiteStore` shows schema versioning, thread-local connections and batched writes.

Subclass `StorageBackend`, set `backend_name`, and implement the abstract methods:

| Method | Contract (what `tests/test_storage.py` checks) |
|---|---|
| `get_meta(key)` / `set_meta(key, value)` | String key/value store; survives `clear()`. Used for the fingerprint signature and params |
| `add_track(record, hashes, times)` | Persist the record and its fingerprints; return the record with `ref` set, `tags` normalised, `num_hashes = len(hashes)`. Adding an existing `track_id` replaces it **atomically** (old hashes gone) |
| `get_track(track_id)` | `TrackRecord` or `None` |
| `get_tracks_by_ref(refs)` | `dict[ref, TrackRecord]`; unknown refs are simply absent |
| `find_by_content_hash(h)` / `find_by_filepath(p)` | Exact lookup; empty string returns `None` |
| `list_tracks(*, query, sort, order, offset, limit, source_type, tag)` | Returns `(page, total_matching)`. `query` is a case-insensitive substring over title, artist, filename and tags; `tag` is exact; validate `sort`/`order` with `self._sort_key()` (raises `ValidationError`) |
| `update_track(track_id, changes)` | Run `changes` through `validate_track_changes()`; `NotFoundError` for unknown ids |
| `delete_track(track_id)` | `True` if it existed, else `False`; removes its fingerprints |
| `count_tracks()` | Cheap |
| `query_hashes(hash_values, max_rows_per_hash=None, stats=None)` | Return three aligned **int64** arrays `(hash, track_ref, time_offset)` with one row per stored fingerprint whose hash is in the input, including several rows per track for repeated hashes; empty arrays for no input or no hit (`self._empty_hash_result()`). When `max_rows_per_hash` is given, skip every hash that has more than that many rows in the store *entirely* (do not truncate it) and, when `stats` is a dict, set `stats["skipped_hashes"]` to how many were skipped (`SQLiteStore` and `PostgresStore` use a `GROUP BY ... HAVING COUNT(*) > ?` pre-query, `MemoryStore` `np.unique(..., return_counts=True)`). Must handle batches of thousands of hashes in one call (the contract test sends 5000; `SQLiteStore` loads them into a temporary table and joins rather than building a giant `IN (...)` list, `PostgresStore` uses `= ANY(%s)`) |
| `get_stats()` | Cheap, never scans the fingerprint table; must contain `storage_type`, `total_tracks`, `total_hashes`, `total_duration_sec` (`db_path` and `db_size_bytes` are optional and shown by `audiofp stats`; `persistent` is optional too) |
| `clear()` | Delete every track and fingerprint, keep meta |

Non-abstract methods you usually keep: `initialize()` (signature check/stamp),
`close()`, `health_check()`, `delete_tracks()`, `require_track()` and `flush()`. `flush()`
returns 0 in the base class; override it if your backend buffers writes, because
`Indexer.index_paths()` calls it at the end of every run, the upload job runner after
each upload, and `SQLiteStore` shows the expected contract (buffered tracks stay
searchable, `close()` flushes, a failed flush keeps the rows and raises). Raise
`StorageError` (HTTP 503) for backend failures; the indexer stops a whole run on it
instead of failing file by file and re-raises it as
`Indexing stopped after N file(s): ...`, which makes the job `failed`.

`query_hashes()` is the hot path: the matcher calls it once per search with every
distinct query hash (hundreds of thousands for an hour-long call). Design the index
for batched lookups, not per-hash round trips.

Registration checklist:

1. `fingerprint/storage/__init__.py`: add a branch to `create_storage()`. Import the
   driver lazily inside the branch, as `PostgresStore` does, so the dependency stays optional.
2. `fingerprint/config.py`: `Settings.validate()` hard-codes
   `("memory", "sqlite", "postgres")` for `storage_type`; extend it and the field's help text.
   Add any connection settings as new `Settings` fields (they become `AUDIOFP_*` variables automatically).
3. `fingerprint/cli.py`: `build_parser()` lists the `--storage` choices.
4. `tests/test_storage.py`: add your backend to `BACKENDS` in the `store` fixture. The
   PostgreSQL entry is gated on `AUDIOFP_TEST_POSTGRES_DSN`; do the same for anything
   that needs a live server, and add a CI job like the `postgres` one in
   `.github/workflows/ci.yml`.
5. `pyproject.toml`: an optional-dependency group for the driver, and
   `[tool.coverage.run] omit` if the backend cannot run in the default test job.

Run `pytest -q tests/test_storage.py` until every parametrised case passes.

---

## Adding an endpoint

Routes live in `fingerprint/api/routes/`, one module per area (`search.py`,
`tracks.py`, `jobs.py`, `system.py`). Each module registers on the shared blueprint
`api_bp` (mounted at `/api/v1` by `create_app()`) and reaches the application through
`runtime()`, which returns the `Runtime` stored in `app.extensions["audiofp"]`. Handlers
stay thin: parse the request, call a `Runtime`/storage method, format the result with
helpers from `fingerprint/api/responses.py`.

Example: a new module `fingerprint/api/routes/patterns.py` exposing
`GET /api/v1/patterns?tag=...` (a thin wrapper over `list_tracks(tag=...)`):

```python
"""GET /api/v1/patterns - tracks carrying a given tag."""

from __future__ import annotations

from flask import jsonify, request

from ...utils.exceptions import ValidationError
from ..responses import format_page, format_track
from ..validators import parse_pagination
from . import api_bp, runtime


@api_bp.route("/patterns", methods=["GET"])
def list_patterns():
    rt = runtime()
    tag = (request.args.get("tag") or "").strip().lower()
    if not tag:
        raise ValidationError("'tag' is required", details={"field": "tag"})
    page, per_page = parse_pagination(request.args)
    items, total = rt.storage.list_tracks(tag=tag, offset=(page - 1) * per_page, limit=per_page)
    return jsonify(format_page([format_track(t) for t in items], total, page, per_page))
```

Then:

1. **Import the module** in `fingerprint/api/routes/__init__.py`. Routes are registered
   as a side effect of `from . import jobs, search, system, tracks` at the bottom of
   that file; a module that is not imported does not exist.
2. **Validate with the helpers** in `fingerprint/api/validators.py`: `parse_int`,
   `parse_float`, `parse_bool`, `parse_choice`, `parse_pagination` (`page`/`per_page`,
   max 200), `require_upload` (multipart file, extension check), `clean_directory_path`
   (normalises without touching the filesystem, so authorisation can run before an
   existence check). They raise `ValidationError` with a `details` dict naming the
   field. For a JSON body call `check_json_body_size(request)` first (or reuse
   `_json_body()` from `routes/tracks.py`): it answers `413 payload_too_large` above
   `MAX_JSON_BODY_BYTES` (1 MiB) and `411 length_required` for a chunked body
   (`Transfer-Encoding` without `Content-Length`), before the body is parsed.
3. **Raise, do not build error responses.** Any `AudioFPError` subclass
   (`fingerprint/utils/exceptions.py`) is rendered by `register_error_handlers()` in
   `fingerprint/api/errors.py` with its `code` and `http_status`; Flask's own
   `HTTPException`s and unexpected exceptions get the same envelope:

   ```json
   {"error": "'tag' is required", "code": "validation_error", "status": 400, "details": {"field": "tag"}, "request_id": "3f9c2a1b7e55"}
   ```

   `error` stays a plain string for 1.x clients. Need a new error type? Subclass
   `AudioFPError` with class attributes `code` and `http_status`.
4. **Authentication is automatic.** `create_app()` runs `check_request()` from
   `fingerprint/api/auth.py` for every `/api/` path when `AUDIOFP_API_KEY` is set. Only
   paths in `auth.PUBLIC_PATHS` (`/api/v1/health`, `/api/v1/openapi.json`) and CORS
   preflight (`OPTIONS`) requests are exempt, and `GET` requests matching
   `auth.STREAM_PATH` (the audio-stream routes) may carry a stream token (`?token=`,
   minted by `make_stream_token()`) instead of the key. If a new endpoint must be
   reachable from a URL alone, mint and verify a scoped token the same way rather
   than accepting the key in a query string - the access log only redacts `token` and
   `api_key`.
5. **Document it in `fingerprint/api/openapi.py`.** `build_openapi()` is hand-written so
   descriptions can be real explanations. This is not optional:
   `tests/test_api.py::test_openapi_covers_every_route` compares every registered
   `(METHOD, path)` under `/api/v1` with the spec and fails on routes that are missing
   *or* documented but not registered. Paths are written without the `/api/v1` prefix
   and with `<track_id>` converters rewritten as `{track_id}`; every alias route and
   method needs its own entry. Add response schemas under `components.schemas` when you
   introduce a new shape.
6. **Test it** in `tests/test_api.py` with the `client` fixture (Flask test client over
   SQLite in a temp dir) and the `upload()` / `wait_for_job()` helpers from
   `tests/conftest.py`.
7. Mention it in `docs/API.md`, and in the bundled `/docs` page
   (`fingerprint/static/docs.html`) if end users should see it.

Prefer putting real logic on `Runtime` (`fingerprint/api/runtime.py`) rather than in the
route function: the CLI reuses `Runtime` without Flask.

---

## Adding a job type

`JobManager` (`fingerprint/jobs/manager.py`) is storage-agnostic and knows nothing about
indexing; the two existing job types (`"upload"`, `"directory"`) are just closures
created in `Runtime.start_upload_job()` and `Runtime.start_directory_job()`.

```python
job = manager.submit(job_type, label, runner, *, total=0, meta=None)   # -> Job, queued immediately
```

- `runner` is `Callable[[Job], dict | None]`. Its return value becomes `job.result`
  (persisted as JSON when `persist_jobs` is on, so keep it bounded; write large output
  to a file under `settings.data_dir` and return its path).
- Jobs move `pending -> running -> completed | failed | cancelled`. An exception
  marks the job `failed` with `job.error` set to `exc.message` when the exception has
  one (all `AudioFPError`s do), otherwise `"TypeName: text"`. A job that was running when
  the process died is reloaded as `interrupted`.
- The pool has `settings.max_concurrent_jobs` threads (default 2). Indexing jobs
  additionally share the indexer's worker pool; a CPU-heavy job type should do the same
  or add its own bounded executor rather than spawning threads per item.
- A job that adds tracks should end with `self.storage.flush()`, as `start_upload_job()`
  does (`Indexer.index_paths()` flushes by itself): with SQLite's batched writes the
  fingerprints of a finished job otherwise stay in memory until the buffer fills or
  the server shuts down cleanly.

Inside the runner, the `Job` API is:

| Call | Purpose |
|---|---|
| `job.update(total=..., completed=..., succeeded=..., failed=..., skipped=..., current_item=...)` | Thread-safe counter updates; the UI's percent, rate and ETA derive from `total`/`completed`. Unknown field names raise `AttributeError` |
| `job.add_error(item, message, code=None, limit=500)` | Append to the bounded error list. Pass `limit=settings.job_max_errors` as `Runtime` does; the manager's `max_errors` is not applied automatically |
| `job.should_cancel()` | `True` once `POST /api/v1/jobs/{id}/cancel` was called; stop cooperatively (used as `should_cancel=` for `Indexer.index_paths`) |
| `job.check_cancelled()` | Raises `JobCancelled`, which the manager turns into status `cancelled` |
| `manager.touch(job)` | Persist progress to disk, throttled to once per 2 seconds |

Example: a batch pattern search over a server-side folder, as a `Runtime` method next to
`start_directory_job()`:

```python
def start_batch_search_job(self, directory: str, *, mode: str = "occurrences") -> Job:
    self.check_directory_allowed(directory)                       # AUDIOFP_INDEX_ROOTS / production rules
    paths = find_media_files(directory)
    if not paths:
        raise ValidationError("No supported audio or video files were found in that directory.", details={"directory": directory})
    if self.jobs.active_count() >= self.settings.max_concurrent_jobs * 4:
        raise JobError("Too many jobs queued; wait for running jobs to finish.", http_status=429)

    def run(job: Job) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        succeeded = failed = 0
        for i, path in enumerate(paths, 1):
            job.check_cancelled()
            job.update(current_item=os.path.basename(path))
            try:
                result = self.search_file(path, filename=os.path.basename(path), mode=mode, top_k=self.settings.max_top_k)
            except AudioFPError as exc:                            # undecodable file: record and continue
                failed += 1
                job.add_error(path, exc.message, exc.code, self.settings.job_max_errors)
            else:
                succeeded += 1
                for m in result.matches:
                    for o in m.occurrences:
                        rows.append(
                            {
                                "file": path,
                                "track_id": m.track.track_id if m.track else None,
                                "start_sec": round(result.query.frames_to_seconds(o.query_start_frames), 3),
                                "end_sec": round(result.query.frames_to_seconds(o.query_end_frames), 3),
                                "confidence": o.confidence,
                                "peak_ratio": o.peak_ratio,
                            }
                        )
            job.update(completed=i, succeeded=succeeded, failed=failed)
            self.jobs.touch(job)
        job.update(current_item=None)
        return {"directory": directory, "occurrences": rows}

    return self.jobs.submit("batch-search", directory, run, total=len(paths), meta={"directory": directory, "mode": mode})
```

(`AudioFPError` would need importing in `runtime.py`; `os`, `Any`, `Job`, `JobError`,
`ValidationError` and `find_media_files` already are.)

To expose it, add a route (see the previous section) that calls the method and returns
`202` with `{"job_id": job.id, "job": format_job(job), ...}` like `index_directory()`
does. `GET /api/v1/jobs?type=batch-search` filters by the string you passed to
`submit()`; the `type` enum in `openapi.py`'s `/jobs` parameters is documentation only
(no test enforces it), but update it. Cover the runner in `tests/test_indexing_jobs.py`
style tests: submit, wait for `job.is_terminal`, assert `status`, `result` and `errors`.

---

## Where a keyword / transcript search would plug in

> **Not implemented.** Nothing in this section exists in the repository. It records the
> intended shape so that a contributor adds a second engine without bending the
> fingerprint engine out of shape.

Fingerprints answer "is this exact audio present, and where?". Call-center QA also asks
"did the agent *say* the required words?", which no fingerprint can answer. That needs a
second engine with its own index, sitting **next to** the fingerprint index and sharing
everything else:

| Shared (exists today) | Owned by the new engine (to be written) |
|---|---|
| `TrackRecord` and its `track_id`, `filepath`, `tags`, `metadata` | A transcript/keyword index keyed by `track_id` (for example an SQLite FTS table in its own file, or a table beside `tracks`) |
| `JobManager` - a new job type (say `"transcribe"`) submitted from `Runtime` | A transcribe runner that decodes, runs ASR and writes segments |
| `Runtime` - one more attribute, initialised in `__init__` and closed in `close()` | A search function returning `(track_id, start_sec, end_sec, text, score)` rows |
| `api_bp` and the OpenAPI document | New routes (for example a text search endpoint and a per-track transcript endpoint) |
| The UI's library list (its search box also matches tags), upload form and jobs panel | A results view for text hits |
| The `meta` key/value store (`get_meta`/`set_meta`) | A model/version stamp for the transcript index, mirroring the fingerprint signature idea |

Design notes:

- Decode through the existing decoder rather than a second audio stack:
  `fingerprint.core.decoder.load_audio(path, 16000)` (or `iter_audio_chunks(path, 16000, chunk_seconds)`
  for long calls) returns float32 mono at any sample rate you ask for, uses libsndfile
  for WAV/FLAC/OGG/Opus/MP3 and ffmpeg for video and M4A/AAC/WMA, and raises the same
  `AudioDecodeError` / `FFmpegNotFoundError` the indexer already handles.
- Do not extend the `StorageBackend` contract with transcript methods. Every backend
  would have to implement them and `tests/test_storage.py` would grow for a feature most
  deployments will not enable. Give the engine its own small store interface.
- Do not put transcripts into `TrackRecord.metadata`: `PATCH` caps metadata at 64 KB,
  and `list_tracks(query=...)` searches only title, artist, filename and tags.
- There is no delete hook: `delete_track()` in `fingerprint/api/routes/tracks.py` and the
  bulk delete call the storage directly. The transcript index must be cleaned up there
  too (or tolerate orphans and prune them in a job).
- Word timestamps are what make a per-call timeline possible; whichever ASR you pick,
  store `start_sec`/`end_sec` per segment (or per word) so text hits can be merged with
  fingerprint occurrences on the same time axis.

Illustration of the transcribe step with `faster-whisper` (an external project, not a
dependency of AudioFP; the snippet is a sketch, not code from this repository):

```python
from faster_whisper import WhisperModel          # not installed by AudioFP

from fingerprint.core.decoder import load_audio

audio = load_audio(record.filepath, 16000)                      # float32 mono, 16 kHz
model = WhisperModel("small", device="cpu", compute_type="int8")
segments, info = model.transcribe(audio, word_timestamps=True)
for seg in segments:
    store_segment(record.track_id, seg.start, seg.end, seg.text)   # your index, keyed by track_id
```

Suggested order of work: the index and its tests first, then the job type, then the
endpoints (plus `openapi.py`), then the UI.

---

## Coding standards

- **Python 3.10+** (`requires-python` in `pyproject.toml`). Modules start with
  `from __future__ import annotations`; use `X | None` style annotations, dataclasses
  for value objects, `logging.getLogger(__name__)` for logs (request and job ids are
  attached automatically through context variables in `fingerprint/utils/logging.py`).
- **Lint and format with ruff** before pushing; CI runs exactly these:

  ```bash
  ruff check fingerprint tests run.py
  ruff format --check fingerprint tests run.py
  ```

  Configuration is in `pyproject.toml`: `line-length = 160`, `target-version = "py310"`,
  rule sets `E, F, W, I, B, UP, C4, SIM, RUF` with a short documented ignore list.
  `make lint` / `make format` wrap the same commands.
- **Tests** (`pytest -q`) generate all audio on the fly with `synth_signal()` from
  `tests/conftest.py`; do not add binary fixtures. Useful fixtures: `settings` (memory
  storage), `sqlite_settings`, `app` / `client` (Flask test client), `audio_dir`, and the
  helpers `upload()` and `wait_for_job()`. Scale tests are marked `slow` and excluded by
  default (`pytest -q -m "slow or not slow"` runs everything). The storage contract
  suite runs against PostgreSQL when `AUDIOFP_TEST_POSTGRES_DSN` is set. CI covers
  Linux, Windows and macOS on Python 3.10, 3.12 and 3.13, installs ffmpeg only on
  Linux, and runs `audiofp doctor`, so code paths must degrade cleanly when ffmpeg is
  absent.
- **Errors** are `AudioFPError` subclasses with a stable `code` and `http_status`;
  never return ad-hoc error JSON from a route.
- **Settings** are dataclass fields on `Settings` with `help` metadata. Adding a field is
  all it takes to get an `AUDIOFP_*` variable, type coercion via `_coerce()` and a row
  in `audiofp config` / `audiofp config --describe`; add a check to `Settings.validate()`
  when values have constraints. Mark a field `fingerprint=True` only if it changes the produced
  hashes - it then becomes part of the signature and existing databases stop opening in
  `strict` mode.
- **Docs**: endpoints go in `fingerprint/api/openapi.py` (test-enforced) and
  `docs/API.md`; configuration is documented from field metadata
  (`audiofp config --describe` prints the Markdown table); user-facing changes go in
  `README.md`.
- **Version rules**
  - `FINGERPRINT_ALGORITHM_VERSION` in `fingerprint/config.py`: bump it whenever a code
    change makes previously stored fingerprints incompatible with freshly computed ones
    (hash layout, STFT framing, peak-picking rules). Changing a *parameter* does not need
    a bump - parameters are already part of the signature. Bumping invalidates every
    existing database (users must re-index with `audiofp db reset`), so do it
    deliberately and say so in the release notes.
  - `SCHEMA_VERSION` in `fingerprint/storage/sqlite_store.py` (currently 3): bump on
    any SQLite schema change and add an idempotent `if from_version < N:` step to
    `_migrate()` (v3 added the `tracks.flushed` column this way, as `_SCHEMA_V3`).
    A database with a newer version than the code is refused with `StorageError`.
  - The package version is declared in both `fingerprint/__init__.py` (`__version__`)
    and `pyproject.toml` (`version`); keep them identical (no test checks this, so
    review it by hand).
