# Extending AudioFP

This guide is for building on AudioFP: embedding it in a Python program, scripting
pattern searches over call recordings, and adding a storage backend, an endpoint or a
background job type. There's also a section on where a transcript search engine would
go next to the fingerprint engine.

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

Everything below names real classes, functions and fields. The one section about
something that doesn't exist yet, the transcript search engine, says so up front.

---

## Using AudioFP as a library

Install the package with `pip install -e .` and import from `fingerprint`. The core,
indexing and storage packages don't import Flask. Importing
`fingerprint.api.runtime.Runtime` does pull it in, because the `api` package's
`__init__` imports the app factory.

### Settings

`Settings.load()` resolves configuration in layers. Field defaults come first, then the
profile (`development`, `production` or `testing`), then `AUDIOFP_*` environment
variables, which can also come from a `.env` file, then explicit keyword overrides. At
the end it calls `Settings.validate()`, which raises `ConfigurationError` on a bad value
or an unknown override name.

```python
from fingerprint.config import Settings

# ignore the process environment and .env, so the result is reproducible
settings = Settings.load(dotenv=False, env={}, storage_type="sqlite", sqlite_path="data/qa.db")

# or honour AUDIOFP_* variables and ./.env, like the CLI does
settings = Settings.load()
```

The fields flagged `fingerprint=True` in `fingerprint/config.py` are `sample_rate`,
`n_fft`, `hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`,
`min_hash_time_delta` and `max_hash_time_delta`. Together with
`FINGERPRINT_ALGORITHM_VERSION` they form the fingerprint signature,
`settings.fingerprint_signature()`. A database is stamped with it on first use, and
every process that reads or writes that database must use the same values.
`audiofp config --describe` prints the full option table.

The library doesn't configure logging. Call
`fingerprint.utils.logging.configure_logging(level, fmt, log_file)` if you want the
`fingerprint.*` loggers on stderr. The CLI and `create_app()` both do this.

### Storage

```python
from fingerprint.storage import create_storage

store = create_storage(settings)          # MemoryStore | SQLiteStore | PostgresStore
...
store.close()
```

`create_storage(settings, *, check_compat=True)` picks the backend from
`settings.storage_type` and calls `store.initialize(signature, params, settings.fingerprint_compat)`.
What happens on a signature mismatch depends on `fingerprint_compat`: `"strict"`, the
default, raises `FingerprintCompatibilityError`, `"warn"` logs a warning and `"ignore"`
carries on. Pass `check_compat=False` only from maintenance code such as
`audiofp db reset`.

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

`Indexer.index_file()` never raises for a problem with the file itself, such as an
unsupported extension, undecodable audio or zero hashes. For those it returns an
`IndexOutcome` with `status="failed"`, `error` and `error_code`. A backend failure is
different: it raises `StorageError`, because retrying the other files wouldn't help.
Some details:

- Duplicate detection follows `settings.dedupe`. `"content"`, the default, compares a
  SHA-256 of the file; the other values are `"path"` and `"none"`. A duplicate outcome
  carries `duplicate_of`, the existing `track_id`, and `track`, the existing record.
- `metadata` is stored as `{"source": source, **metadata}`, so a `source` key of
  your own overrides the default. The default is `"file"` for `index_file`. The API
  uses `"upload"` and `"directory"`, and the CLI uses `"cli"`.
- `title` and `artist` fall back to `metadata_from_filename()`, which parses `Artist - Title.ext`.
- `index_paths(paths, tags=..., progress=..., should_cancel=...)` and
  `index_directory(directory, recursive=True, ...)` run many files on a thread pool of
  `settings.effective_index_workers` threads and return an `IndexSummary`. They call
  `storage.flush()` at the end of the run. A `StorageError` from any file stops the
  run and is re-raised as `StorageError("Indexing stopped after N file(s): ...")`.
  Byte-identical files inside one batch yield a single track, because the duplicate
  check is repeated under the indexer's `_store_lock` right before `add_track()`.
  Call `indexer.close()` when you're done to shut the pool down.

### Fingerprinting

```python
from fingerprint.core import Fingerprinter

fingerprinter = Fingerprinter(settings)
fp = fingerprinter.fingerprint_file("calls/2026-09-01-0001.wav")   # streaming, flat memory
fp = fingerprinter.fingerprint_array(samples)                      # numpy array, already at settings.sample_rate
```

- `fingerprint_file(path, *, max_seconds=None, normalize=True, backend="auto", display_name=None)`
  decodes the file once, in `settings.chunk_seconds` chunks. With `normalize=True` the
  result is the same fingerprint you would get from the peak-normalised recording,
  computed in the same pass. `PeakExtractor` tracks the peak sample and thresholds
  against `min_amplitude * peak`, and `fp.gain` reports the implied `1 / peak`.
- `fingerprint_chunks(chunks, normalize=True, extra=None)` is the underlying entry
  point if you have your own chunk stream.
- `backend` is `"auto"`, `"soundfile"` or `"ffmpeg"`. WAV/FLAC/OGG/Opus/MP3, plus the
  other `NATIVE_AUDIO_EXTENSIONS` in `fingerprint/formats.py`, are decoded by
  libsndfile. Video containers and M4A/AAC/WMA need ffmpeg on `PATH`, and you get
  `FFmpegNotFoundError` when it isn't there.
- `fingerprint_array(audio, normalize=True)` accepts `(n,)`, `(n, ch)` or `(ch, n)`
  float arrays and assumes they are already at `settings.sample_rate`, 11025 Hz by
  default, so resample first. The test-suite uses `soxr` for that.
- The result is a `Fingerprint` with int64 `hashes`, int32 `hash_times` (anchor
  frames), `peak_times`, `peak_freqs`, `num_samples`, `sample_rate` and `hop_length`,
  plus the helpers `num_hashes`, `duration_sec` and `frames_to_seconds(frames)`. One
  frame is `hop_length / sample_rate` seconds, about 46 ms with the defaults.

### Matching

```python
from fingerprint.core import Matcher, MatchOptions

matcher = Matcher(settings)
options = MatchOptions.from_settings(settings, mode="occurrences", top_k=50)
matches = matcher.match(fp, store, options)     # list[TrackMatch], strongest first
```

These are the `MatchOptions` fields with their dataclass defaults. `from_settings()`
fills them from `Settings`, applies keyword overrides such as `mode=`, then clamps:

| Field | Default | Meaning |
|---|---|---|
| `mode` | `"identify"` | `"identify"` reports the best alignment per track, `"occurrences"` every alignment above the thresholds per track |
| `top_k` | `5` | Maximum number of tracks returned; clamped to `settings.max_top_k`, which defaults to 50 |
| `min_aligned_hashes` | `10` | Distinct query hashes that must vote for one offset |
| `min_confidence` | `0.02` | `aligned_hashes / distinct query hashes inside the matched span` |
| `min_peak_ratio` | `12.0` | Spike height over the histogram background; chance matches sit around 3 to 9 |
| `offset_tolerance_frames` | `1` | Adjacent offset bins merged when scoring, to absorb clip-start jitter |
| `max_occurrences_per_track` | `25` | Cap per track in occurrences mode |
| `max_rows_per_hash` | `2000` | Query hashes stored more than this many times in the library are skipped as stop words. `0` disables the skip. Passed through to `store.query_hashes()` |
| `max_search_votes` | `5_000_000` | Budget for the row x anchor join; the most common hashes are dropped first when it would be exceeded |

After every `match()` call, `matcher.last_diagnostics` is a `MatchDiagnostics` with
`query_hashes`, `db_rows`, `votes`, `candidate_tracks`, `scored_tracks`,
`skipped_common_hashes` and `dropped_for_vote_cap`. Its `to_dict()` is what the API
returns as `diagnostics`.

`top_k` matters for pattern search. If you index 30 compliance phrases and search one
call with the default `top_k=5`, at most five phrases can be reported. Raise it, up to
`AUDIOFP_MAX_TOP_K`.

The result objects live in `fingerprint/core/matcher.py`:

| `TrackMatch` field | Meaning |
|---|---|
| `track_ref` / `track` | Internal integer ref and the `TrackRecord` (title, filename, tags, metadata, ...) |
| `aligned_hashes`, `confidence`, `peak_ratio`, `offset_frames` | Copied from the strongest occurrence |
| `matched_rows` | Raw hash votes for this track before scoring |
| `occurrences` | `list[Occurrence]`. `occurrences[0]`, also available as `match.best`, is the strongest; the rest are sorted by `offset_frames` |

| `Occurrence` field | Meaning |
|---|---|
| `offset_frames` | `track_frame` minus `query_frame`. A negative value means the track's audio lies inside the query |
| `aligned_hashes`, `confidence`, `peak_ratio` | Scores for this alignment |
| `query_start_frames`, `query_end_frames` | Span of query frames covered by the aligned hashes, with outliers trimmed |
| `track_start_frames`, `track_end_frames` | Properties: the query span shifted by `offset_frames` |

Convert frames with `fp.frames_to_seconds(...)`. `quality_label(confidence, peak_ratio)`
is what the UI and CLI use for the label: `"strong"` needs confidence >= 0.15 and
peak_ratio >= 30, `"likely"` needs >= 0.05 and >= 18, and anything else is `"weak"`.

### Complete example

This indexes a folder of short patterns with tags, then searches one call recording in
occurrences mode and prints a timeline. It uses the SQLite backend directly, with no
server.

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
the persisted runtime defaults from `<data_dir>/runtime-settings.json` (the file that
`PUT /api/v1/settings` edits), truncates queries at `settings.max_query_seconds` and
returns a `SearchResult`. `format_search()` turns that into the same JSON that
`POST /api/v1/search` and `audiofp search --json` emit.

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
identification: index the short things you are looking for and query with the long
recording. Each call is fingerprinted once and compared against every pattern in one
pass. The library stays small, one track per phrase, jingle or hold-music loop.

### 1. Build the pattern library

Cut each pattern to a clean clip, such as the disclaimer as actually played, the IVR
greeting or one loop of the hold music, and index it with tags that describe what it
is. The test-suite works with 3-second patterns. Very short or near-silent clips
produce few hashes and can't pass `min_aligned_hashes`.

```bash
audiofp index ./patterns/compliance --tags "compliance,disclaimer" --json > indexed-compliance.json
audiofp index ./patterns/ivr        --tags "ivr,greeting"
audiofp index ./patterns/hold       --tags "hold-music"
```

`--json` prints the `IndexSummary`: `indexed`, `duplicates`, `failed`, `track_ids`,
`errors` and `duplicates_of`. Through the API, `POST /api/v1/tracks` takes the same
information as form fields (`audio`, `title`, `artist`, `tags` comma-separated,
`metadata` as a JSON string) and returns `202` with a job to poll.
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
HTTP the same call is `POST /api/v1/search` with the form fields `audio`,
`mode=occurrences` and `top_k`, and optionally `min_confidence`, `min_aligned_hashes`,
`min_peak_ratio` and `max_occurrences` (1 to 500). Calls longer than
`settings.max_query_seconds`, 3600 s by default, are truncated, and the response then
sets `query.truncated=true`.

### 3. Interpreting offsets and spans

`offset_frames` is `track_frame` minus `query_frame`. With a short indexed pattern and
a long query, the pattern starts after the recording does, so the offset is negative
and the pattern sits inside the recording at `-offset`. The JSON produced by
`fingerprint/api/responses.py` spells this out per occurrence:

| Field | Meaning |
|---|---|
| `offset_sec` | Signed: track time minus query time |
| `query_offset_sec` | `max(0, -offset_sec)`: where the pattern starts inside the recording |
| `track_offset_sec` | `max(0, offset_sec)`: where the query starts inside the track (identify mode) |
| `query_start_sec`, `query_end_sec` | Span of the recording covered by aligned hashes; these are the timeline fields |
| `track_start_sec`, `track_end_sec` | Span of the pattern that matched; near 0 to the pattern length for a full hit |
| `aligned_hashes`, `confidence`, `peak_ratio`, `quality` | Scores; `quality` is `strong` / `likely` / `weak` |

To build a per-call timeline, take every occurrence of every match and sort by
`query_start_sec`. A few things matter when you turn this into QA verdicts:

- `query_end_sec` runs slightly short of the true end of the pattern. The span is
  measured on hash anchors, and the last anchors of a pattern pair with peaks outside
  it. The test `test_occurrences_mode_finds_pattern_twice_and_negative_offsets` pins
  this behaviour. Don't compare spans to the pattern length with a tight tolerance.
- `confidence` is normalised by the query hashes inside the matched span, so a
  4-second pattern in a 10-minute call can still score highly. It is a per-span figure
  and doesn't measure what fraction of the whole call matched.
- Occurrences of the same pattern at most `2 * offset_tolerance_frames + 1` frames
  apart, or covering the same query and track region, are collapsed into one. The
  stronger candidate is kept.
- The same phrase indexed twice, say two takes of the disclaimer, shows up as two
  tracks matching the same span. When you count, group by tag or by your own
  `metadata` key, not by track.
- `max_occurrences_per_track`, 25 by default, caps how many hits per pattern are
  reported. Hold music that loops for ten minutes will hit the cap. Raise
  `AUDIOFP_MAX_OCCURRENCES_PER_TRACK` or pass `max_occurrences` per request.
- `track_start_sec` and `track_end_sec` tell you how much of the pattern was heard. A
  disclaimer cut off half-way matches only its first seconds.

### 4. Batch script using the CLI `--json` output

One JSON line per occurrence, sorted per call, using `jq`:

```bash
#!/usr/bin/env bash
# Search every recording for every indexed pattern and build timeline.jsonl.
set -u
: > timeline.jsonl
for call in calls/*.wav; do
  # exit status is 1 when nothing matched, don't let that stop the loop
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
  lines go to stderr, so stdout is clean JSON. `-q` lowers the log level to WARNING.
- Pass `--mode occurrences` explicitly. When `--mode` is omitted the CLI uses the mode
  stored in `<data_dir>/runtime-settings.json`, which is whatever was last set from
  the UI or `PUT /api/v1/settings`, and `identify` by default.
- The flags `audiofp search` takes are `--mode`, `--top-k`, `--min-confidence`,
  `--min-aligned`, `--min-peak-ratio` and `--json`, plus the common `--profile`,
  `--data-dir`, `--storage`, `--sqlite-path`, `--log-level` and `--quiet`. There is no
  `--max-occurrences` flag. The per-track cap comes from
  `settings.max_occurrences_per_track`.
- Every `audiofp search` invocation builds a full `Runtime`: it opens the database and
  creates the job and index thread pools. For thousands of calls, the Python loop from
  the library example above, or the API, is much cheaper.

### 5. Custom metadata and tags per track

Tags go through `normalize_tags()` in `fingerprint/storage/base.py`: lower-cased,
trimmed, de-duplicated and capped at 50 per track. A comma-separated string or a list
is accepted. Metadata is a free-form JSON object.

| Where | Tags | Metadata |
|---|---|---|
| CLI `audiofp index` | `--tags "compliance,disclaimer"`, applied to every file of the run | not available on the CLI |
| `Indexer.index_file()` | `tags=[...]` | `metadata={...}` |
| `POST /api/v1/tracks` (multipart) | `tags=compliance,disclaimer` | `metadata={"phrase": "...", "script_version": 3}`, sent as a JSON string form field |
| `POST /api/v1/tracks/index-directory` (JSON) | `"tags": ["batch"]` | not available |
| `PATCH /api/v1/tracks/{track_id}` (JSON) | `{"tags": ["qa", "disclaimer"]}`, which replaces the list | `{"metadata": {...}}`, which replaces the object; max 64 KB, enforced by `validate_track_changes()` |

Only `title`, `artist`, `tags` and `metadata` can be edited after indexing. Anything
else is rejected with `validation_error`. Reading them back:

- `GET /api/v1/tracks?tag=disclaimer` filters by one exact tag. The other parameters
  are `q`, `sort`, `order`, `source_type`, `page` and `per_page`. `GET /api/v1/tags`
  lists distinct tags with counts. In Python, `store.list_tracks(tag="disclaimer")`
  returns `(page, total)`.
- `audiofp tracks list --q disclaimer --json` searches a substring across title, artist,
  filename and tags. The CLI has no exact `--tag` filter.
- Search results can't be filtered by tag server-side. Every match carries the track's
  `tags` and `track_id`, and `TrackMatch.track.metadata` in Python, so filter on the
  client as the script above does. Tags won't give you a hard split between, say, two
  clients' patterns; use separate databases (`AUDIOFP_SQLITE_PATH`) for that.

---

## Adding a storage backend

`fingerprint/storage/base.py` defines the contract. A backend stores tracks and the
inverted index from hash to `(track_ref, frame)`. Track metadata is addressed by an
opaque `track_id`, plus a small integer `ref` that keeps the fingerprint table compact.
`MemoryStore` is about 200 lines and the easiest reference implementation to read.
`SQLiteStore` shows schema versioning, thread-local connections and batched writes.

Subclass `StorageBackend`, set `backend_name`, and implement the abstract methods:

| Method | Contract (what `tests/test_storage.py` checks) |
|---|---|
| `get_meta(key)` / `set_meta(key, value)` | String key/value store; survives `clear()`. Used for the fingerprint signature and params |
| `add_track(record, hashes, times)` | Persist the record and its fingerprints. Return the record with `ref` set, `tags` normalised and `num_hashes = len(hashes)`. Adding an existing `track_id` replaces it atomically, and the old hashes are removed |
| `get_track(track_id)` | `TrackRecord` or `None` |
| `get_tracks_by_ref(refs)` | `dict[ref, TrackRecord]`; unknown refs are left out |
| `find_by_content_hash(h)` / `find_by_filepath(p)` | Exact lookup; empty string returns `None` |
| `list_tracks(*, query, sort, order, offset, limit, source_type, tag)` | Returns `(page, total_matching)`. `query` is a case-insensitive substring over title, artist, filename and tags, and `tag` is exact. Validate `sort` and `order` with `self._sort_key()`, which raises `ValidationError` |
| `update_track(track_id, changes)` | Run `changes` through `validate_track_changes()`; `NotFoundError` for unknown ids |
| `delete_track(track_id)` | `True` if it existed, else `False`; removes its fingerprints |
| `count_tracks()` | Cheap |
| `query_hashes(hash_values, max_rows_per_hash=None, stats=None)` | Return three aligned int64 arrays `(hash, track_ref, time_offset)`, one row per stored fingerprint whose hash is in the input. A hash repeated within a track gives several rows for that track. Return empty arrays for no input or no hit (`self._empty_hash_result()`). When `max_rows_per_hash` is given, skip every hash that has more than that many rows in the store entirely, without truncating it, and when `stats` is a dict set `stats["skipped_hashes"]` to how many were skipped. `SQLiteStore` and `PostgresStore` do this with a `GROUP BY ... HAVING COUNT(*) > ?` pre-query, `MemoryStore` with `np.unique(..., return_counts=True)`. It must handle batches of thousands of hashes in one call; the contract test sends 5000. `SQLiteStore` loads them into a temporary table and joins on it, which avoids a giant `IN (...)` list, and `PostgresStore` uses `= ANY(%s)` |
| `get_stats()` | Cheap, never scans the fingerprint table. Must contain `storage_type`, `total_tracks`, `total_hashes` and `total_duration_sec`. `db_path` and `db_size_bytes` are optional and shown by `audiofp stats`; `persistent` is optional too |
| `clear()` | Delete every track and fingerprint, keep meta |

The non-abstract methods you usually keep are `initialize()`, which checks or stamps
the signature, `close()`, `health_check()`, `delete_tracks()`, `require_track()` and
`flush()`. `flush()` returns 0 in the base class. Override it if your backend buffers
writes: `Indexer.index_paths()` calls it at the end of every run and the upload job
runner calls it after each upload. `SQLiteStore` shows the expected contract: buffered
tracks stay searchable, `close()` flushes, and a failed flush keeps the rows and
raises. Raise `StorageError` (HTTP 503) for backend failures. The indexer stops the
whole run on it and re-raises it as `Indexing stopped after N file(s): ...`, which
makes the job `failed`.

`query_hashes()` is the hot path. The matcher calls it once per search with every
distinct query hash, which is hundreds of thousands for an hour-long call. Design the
index for batched lookups and avoid per-hash round trips.

Registration checklist:

1. `fingerprint/storage/__init__.py`: add a branch to `create_storage()`. Import the
   driver lazily inside the branch, as `PostgresStore` does, so the dependency stays optional.
2. `fingerprint/config.py`: `Settings.validate()` hard-codes
   `("memory", "sqlite", "postgres")` for `storage_type`. Extend it and the field's help
   text. Add any connection settings as new `Settings` fields; they become `AUDIOFP_*`
   variables automatically.
3. `fingerprint/cli.py`: `build_parser()` lists the `--storage` choices.
4. `tests/test_storage.py`: add your backend to `BACKENDS` in the `store` fixture. The
   PostgreSQL entry is gated on `AUDIOFP_TEST_POSTGRES_DSN`. Do the same for anything
   that needs a live server, and add a CI job like the `postgres` one in
   `.github/workflows/ci.yml`.
5. `pyproject.toml`: an optional-dependency group for the driver, and
   `[tool.coverage.run] omit` if the backend cannot run in the default test job.

Run `pytest -q tests/test_storage.py` until every parametrised case passes.

---

## Adding an endpoint

Routes live in `fingerprint/api/routes/`, one module per area: `search.py`,
`tracks.py`, `jobs.py` and `system.py`. Each module registers on the shared blueprint
`api_bp`, which `create_app()` mounts at `/api/v1`, and reaches the application through
`runtime()`. That returns the `Runtime` stored in `app.extensions["audiofp"]`. Handlers
stay thin: parse the request, call a `Runtime` or storage method, and format the
result with the helpers in `fingerprint/api/responses.py`.

As an example, here is a new module `fingerprint/api/routes/patterns.py` exposing
`GET /api/v1/patterns?tag=...`, a thin wrapper over `list_tracks(tag=...)`:

```python
"""GET /api/v1/patterns: tracks carrying a given tag."""

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

1. Import the module in `fingerprint/api/routes/__init__.py`. Routes are registered
   as a side effect of `from . import jobs, search, system, tracks` at the bottom of
   that file, so a module that isn't imported has no routes.
2. Validate with the helpers in `fingerprint/api/validators.py`: `parse_int`,
   `parse_float`, `parse_bool`, `parse_choice` and `parse_pagination`, which handles
   `page` and `per_page` and caps `per_page` at 200. `require_upload` checks a multipart
   file and its extension. `clean_directory_path` normalises a path without touching
   the filesystem, so authorisation can run before an existence check. All of them
   raise `ValidationError` with a `details` dict naming the field. For a JSON body,
   call `check_json_body_size(request)` first, or reuse `_json_body()` from
   `routes/tracks.py`. It answers `413 payload_too_large` above `MAX_JSON_BODY_BYTES`
   (1 MiB) and `411 length_required` for a chunked body, meaning `Transfer-Encoding`
   without `Content-Length`, before the body is parsed.
3. Errors are raised; the error handler builds the response. Any `AudioFPError`
   subclass from `fingerprint/utils/exceptions.py` is rendered by
   `register_error_handlers()` in `fingerprint/api/errors.py` with its `code` and
   `http_status`. Flask's own `HTTPException`s and unexpected exceptions get the same
   envelope:

   ```json
   {"error": "'tag' is required", "code": "validation_error", "status": 400, "details": {"field": "tag"}, "request_id": "3f9c2a1b7e55"}
   ```

   `error` stays a plain string for 1.x clients. For a new error type, subclass
   `AudioFPError` with the class attributes `code` and `http_status`.
4. Authentication is automatic. `create_app()` runs `check_request()` from
   `fingerprint/api/auth.py` for every `/api/` path when `AUDIOFP_API_KEY` is set. The
   exemptions are the paths in `auth.PUBLIC_PATHS` (`/api/v1/health` and
   `/api/v1/openapi.json`) and CORS preflight `OPTIONS` requests. `GET` requests
   matching `auth.STREAM_PATH`, the audio-stream routes, may carry a stream token in
   `?token=`, minted by `make_stream_token()`, in place of the key. If a new endpoint
   must be reachable from a URL alone, mint and verify a scoped token the same way.
   Don't accept the key in a query string: the access log only redacts `token` and
   `api_key`.
5. Document it in `fingerprint/api/openapi.py`. `build_openapi()` is hand-written so
   the descriptions can be real explanations. You can't skip this step:
   `tests/test_api.py::test_openapi_covers_every_route` compares every registered
   `(METHOD, path)` under `/api/v1` with the spec and fails on routes that are missing,
   and on routes that are documented but not registered. Paths are written without the
   `/api/v1` prefix and with `<track_id>` converters rewritten as `{track_id}`. Every
   alias route and method needs its own entry. Add response schemas under
   `components.schemas` when you introduce a new shape.
6. Test it in `tests/test_api.py` with the `client` fixture, a Flask test client over
   SQLite in a temp dir, and the `upload()` and `wait_for_job()` helpers from
   `tests/conftest.py`.
7. Mention it in `docs/API.md`, and in the bundled `/docs` page
   (`fingerprint/static/docs.html`) if end users should see it.

Put real logic on `Runtime` in `fingerprint/api/runtime.py` and keep the route function
thin. The CLI reuses `Runtime` without Flask.

---

## Adding a job type

`JobManager` in `fingerprint/jobs/manager.py` is storage-agnostic and knows nothing
about indexing. The two existing job types, `"upload"` and `"directory"`, are just
closures created in `Runtime.start_upload_job()` and `Runtime.start_directory_job()`.

```python
job = manager.submit(job_type, label, runner, *, total=0, meta=None)   # returns a Job, queued immediately
```

- `runner` is `Callable[[Job], dict | None]`. Its return value becomes `job.result`,
  which is persisted as JSON when `persist_jobs` is on, so keep it bounded. Write large
  output to a file under `settings.data_dir` and return its path.
- A job starts `pending`, moves to `running` and ends as `completed`, `failed` or
  `cancelled`. An exception marks the job `failed` with `job.error` set to
  `exc.message` when the exception has one, which every `AudioFPError` does, and
  otherwise to `"TypeName: text"`. A job that was running when the process died is
  reloaded as `interrupted`.
- The pool has `settings.max_concurrent_jobs` threads, 2 by default. Indexing jobs
  also share the indexer's worker pool. A CPU-heavy job type should do the same or add
  its own bounded executor. Don't spawn a thread per item.
- A job that adds tracks should end with `self.storage.flush()`, as `start_upload_job()`
  does. `Indexer.index_paths()` flushes by itself. With SQLite's batched writes, the
  fingerprints of a finished job otherwise stay in memory until the buffer fills or
  the server shuts down cleanly.

Inside the runner, the `Job` API is:

| Call | Purpose |
|---|---|
| `job.update(total=..., completed=..., succeeded=..., failed=..., skipped=..., current_item=...)` | Thread-safe counter updates. The UI's percent, rate and ETA derive from `total` and `completed`. Unknown field names raise `AttributeError` |
| `job.add_error(item, message, code=None, limit=500)` | Append to the bounded error list. Pass `limit=settings.job_max_errors` as `Runtime` does; the manager's `max_errors` is not applied automatically |
| `job.should_cancel()` | `True` once `POST /api/v1/jobs/{id}/cancel` was called; stop cooperatively. `Indexer.index_paths` takes it as `should_cancel=` |
| `job.check_cancelled()` | Raises `JobCancelled`, which the manager turns into status `cancelled` |
| `manager.touch(job)` | Persist progress to disk, throttled to once per 2 seconds |

An example: a batch pattern search over a server-side folder, written as a `Runtime`
method next to `start_directory_job()`:

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

`AudioFPError` would need importing in `runtime.py`. `os`, `Any`, `Job`, `JobError`,
`ValidationError` and `find_media_files` are already imported there.

To expose it, add a route as in the previous section that calls the method and returns
`202` with `{"job_id": job.id, "job": format_job(job), ...}`, like `index_directory()`
does. `GET /api/v1/jobs?type=batch-search` filters by the string you passed to
`submit()`. The `type` enum in the `/jobs` parameters in `openapi.py` is documentation
only and no test enforces it, but update it anyway. Cover the runner with tests in the
style of `tests/test_indexing_jobs.py`: submit, wait for `job.is_terminal`, then assert
on `status`, `result` and `errors`.

---

## Where a keyword / transcript search would plug in

> **Not implemented.** Nothing in this section exists in the repository yet. It records
> the intended shape, so that whoever adds a second engine doesn't have to bend the
> fingerprint engine to fit it.

A fingerprint can tell you whether a known piece of audio is present, and where.
Call-center QA also needs to know whether the agent said the required words, and no
fingerprint can answer that. That needs a second engine with its own index, sitting
next to the fingerprint index and sharing everything else:

| Shared (exists today) | Owned by the new engine (to be written) |
|---|---|
| `TrackRecord` and its `track_id`, `filepath`, `tags`, `metadata` | A transcript/keyword index keyed by `track_id`, for example an SQLite FTS table in its own file or a table beside `tracks` |
| `JobManager`, with a new job type (say `"transcribe"`) submitted from `Runtime` | A transcribe runner that decodes, runs ASR and writes segments |
| `Runtime`, with one more attribute, initialised in `__init__` and closed in `close()` | A search function returning `(track_id, start_sec, end_sec, text, score)` rows |
| `api_bp` and the OpenAPI document | New routes, for example a text search endpoint and a per-track transcript endpoint |
| The UI's library list (its search box also matches tags), upload form and jobs panel | A results view for text hits |
| The `meta` key/value store (`get_meta`/`set_meta`) | A model/version stamp for the transcript index, mirroring the fingerprint signature idea |

Design notes:

- Decode through the existing decoder; there's no need for a second audio stack.
  `fingerprint.core.decoder.load_audio(path, 16000)`, or
  `iter_audio_chunks(path, 16000, chunk_seconds)` for long calls, returns float32 mono
  at any sample rate you ask for. It uses libsndfile for WAV/FLAC/OGG/Opus/MP3 and
  ffmpeg for video and M4A/AAC/WMA, and raises the same `AudioDecodeError` and
  `FFmpegNotFoundError` the indexer already handles.
- Don't extend the `StorageBackend` contract with transcript methods. Every backend
  would have to implement them and `tests/test_storage.py` would grow for a feature
  most deployments won't enable. Give the engine its own small store interface.
- Don't put transcripts into `TrackRecord.metadata`. `PATCH` caps metadata at 64 KB,
  and `list_tracks(query=...)` searches only title, artist, filename and tags.
- There is no delete hook. `delete_track()` in `fingerprint/api/routes/tracks.py` and
  the bulk delete call the storage directly, so the transcript index has to be cleaned
  up there too. The alternative is to tolerate orphans and prune them in a job.
- Word timestamps are what make a per-call timeline possible. Whichever ASR you pick,
  store `start_sec` and `end_sec` per segment or per word, so text hits can be merged
  with fingerprint occurrences on the same time axis.

A sketch of the transcribe step with `faster-whisper`, an external project that AudioFP
doesn't depend on. The snippet is an illustration and doesn't come from this
repository:

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

- Python 3.10+, per `requires-python` in `pyproject.toml`. Modules start with
  `from __future__ import annotations`. Use `X | None` style annotations, dataclasses
  for value objects and `logging.getLogger(__name__)` for logs. Request and job ids are
  attached to log records automatically through context variables in
  `fingerprint/utils/logging.py`.
- Lint and format with ruff before pushing. CI runs exactly these:

  ```bash
  ruff check fingerprint tests run.py
  ruff format --check fingerprint tests run.py
  ```

  The configuration is in `pyproject.toml`: `line-length = 160`,
  `target-version = "py310"` and the rule sets `E, F, W, I, B, UP, C4, SIM, RUF`, with a
  short documented ignore list. `make lint` and `make format` wrap the same commands.
- Tests run with `pytest -q` and generate all audio on the fly with `synth_signal()`
  from `tests/conftest.py`. Don't add binary fixtures. The useful fixtures are
  `settings` (memory storage), `sqlite_settings`, `app` and `client` for the Flask test
  client, and `audio_dir`, plus the helpers `upload()` and `wait_for_job()`. Scale
  tests are marked `slow` and excluded by default; `pytest -q -m "slow or not slow"`
  runs everything. The storage contract suite runs against PostgreSQL when
  `AUDIOFP_TEST_POSTGRES_DSN` is set. CI covers Linux, Windows and macOS on Python
  3.10, 3.12 and 3.13. It installs ffmpeg only on Linux and runs `audiofp doctor`, so
  code paths must degrade cleanly when ffmpeg is absent.
- Errors are `AudioFPError` subclasses with a stable `code` and `http_status`. Never
  return ad-hoc error JSON from a route.
- Settings are dataclass fields on `Settings` with `help` metadata. Adding a field is
  all it takes to get an `AUDIOFP_*` variable, type coercion via `_coerce()` and a row
  in `audiofp config` and `audiofp config --describe`. Add a check to
  `Settings.validate()` when values have constraints. Mark a field `fingerprint=True`
  only if it changes the produced hashes. It then becomes part of the signature, and
  existing databases stop opening in `strict` mode.
- For docs, endpoints go in `fingerprint/api/openapi.py`, which a test enforces, and in
  `docs/API.md`. Configuration is documented from field metadata;
  `audiofp config --describe` prints the Markdown table. User-facing changes go in
  `README.md`.
- There are three version numbers to keep straight:
  - `FINGERPRINT_ALGORITHM_VERSION` in `fingerprint/config.py`: bump it whenever a code
    change makes previously stored fingerprints incompatible with freshly computed
    ones, such as a change to the hash layout, the STFT framing or the peak-picking
    rules. Changing a parameter doesn't need a bump, because parameters are already
    part of the signature. Bumping invalidates every existing database and users must
    re-index with `audiofp db reset`, so do it deliberately and say so in the release
    notes.
  - `SCHEMA_VERSION` in `fingerprint/storage/sqlite_store.py`, currently 3: bump it on
    any SQLite schema change and add an idempotent `if from_version < N:` step to
    `_migrate()`. v3 added the `tracks.flushed` column this way, as `_SCHEMA_V3`. A
    database with a newer version than the code is refused with `StorageError`.
  - The package version is declared in both `fingerprint/__init__.py` as `__version__`
    and `pyproject.toml` as `version`. Keep them identical. No test checks this, so
    review it by hand.
