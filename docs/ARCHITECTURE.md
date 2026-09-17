# Architecture

How AudioFP 2.x is put together and how the fingerprinting and matching algorithm works, using the real names, constants and defaults from the code. Companion documents: [API.md](API.md) (endpoint reference), [CONFIGURATION.md](CONFIGURATION.md) (every setting), [DEPLOYMENT.md](DEPLOYMENT.md) and [PERFORMANCE.md](PERFORMANCE.md).

The importable package is `fingerprint`; the distribution and CLI are `audiofp` (`pyproject.toml` registers `audiofp = "fingerprint.cli:main"`, and `python -m fingerprint` runs the same `main`). Settings are fields of `fingerprint.config.Settings`; each can be set with the environment variable `AUDIOFP_<FIELD_UPPER>`.

## Overview

### Components

| Package | Responsibility | Main symbols |
|---|---|---|
| `fingerprint.config` | Typed settings: defaults, profile, `AUDIOFP_*` env and `.env`; fingerprint signature | `Settings`, `Settings.load()`, `fingerprint_params()`, `fingerprint_signature()`, `FINGERPRINT_ALGORITHM_VERSION` |
| `fingerprint.formats` | The one list of accepted extensions and which of them need ffmpeg | `NATIVE_AUDIO_EXTENSIONS`, `FFMPEG_AUDIO_EXTENSIONS`, `VIDEO_EXTENSIONS`, `needs_ffmpeg()` |
| `fingerprint.core.decoder` | Streaming decode of any supported file to float32 mono at the working rate | `iter_audio_chunks()`, `select_backend()`, `ffmpeg_info()`, `load_audio()` |
| `fingerprint.core.fingerprinter` | Chunked STFT and peak picking; the high-level facade | `PeakExtractor`, `Fingerprinter`, `Fingerprint` |
| `fingerprint.core.hash_generator` | Landmark (peak-pair) hashes | `generate_hashes()`, `encode_hash()`, `decode_hash()`, `MAX_TIME_DELTA` |
| `fingerprint.core.matcher` | Offset-histogram matching in `identify` and `occurrences` mode | `Matcher`, `MatchOptions`, `MatchDiagnostics`, `TrackMatch`, `Occurrence`, `quality_label()` |
| `fingerprint.storage` | Backend contract plus memory, SQLite and PostgreSQL implementations | `StorageBackend`, `TrackRecord`, `create_storage()`, `SQLiteStore`, `MemoryStore`, `PostgresStore` |
| `fingerprint.indexing` | Folder scanning, per-file outcomes, concurrent indexing, progress | `Indexer`, `IndexOutcome`, `IndexSummary`, `find_media_files()`, `ProgressTracker` |
| `fingerprint.jobs` | Background jobs: bounded pool, JSON persistence, cancellation | `JobManager`, `Job`, `JobCancelled` |
| `fingerprint.api` | Flask app factory, the `Runtime`, routes, auth, error envelope, OpenAPI, bundled UI | `create_app()`, `Runtime`, `api_bp`, `check_request()`, `error_response()` |
| `fingerprint.cli` | The `audiofp` command | `main()`, `build_parser()` |
| `fingerprint.utils` | Exception hierarchy with codes and HTTP statuses, request/job-aware logging, file helpers | `AudioFPError`, `configure_logging()`, `request_id_var`, `job_id_var`, `sha256_file()`, `is_within()` |

```
   browser UI (/)      curl / SDK (/api/v1)                 terminal
          \                 /                                  |
           v               v                                   v
  +-------------------------------------+        +---------------------------+
  | Flask app      fingerprint/api/     |        | audiofp CLI               |
  |  app.py     request ids, API key    |        |  fingerprint/cli.py       |
  |  routes/*   thin handlers           |        |  builds the same Runtime, |
  |  errors.py  JSON error envelope     |        |  no Flask                 |
  +------------------+------------------+        +-------------+-------------+
                     | app.extensions["audiofp"]               |
                     v                                         v
  +--------------------------------------------------------------------------+
  | Runtime        fingerprint/api/runtime.py                                |
  |  settings   storage   fingerprinter   matcher   indexer   jobs           |
  +-------+---------------+-------------------+-------------------+----------+
          |               |                   |                   |
          v               v                   v                   v
  +---------------+ +---------------+ +-----------------+ +-----------------+
  | core          | | indexing      | | jobs            | | storage         |
  |  decoder      | |  Indexer      | |  JobManager     | |  MemoryStore    |
  |  PeakExtractor| |  scanner      | |  Job            | |  SQLiteStore    |
  |  hashes       | |  progress     | |  (thread pool,  | |  PostgresStore  |
  |  Matcher      | |               | |   JSON files)   | |                 |
  +---------------+ +---------------+ +-----------------+ +-----------------+

  indexing:  core (decode -> peaks -> hashes)  -> storage.add_track(record, hashes, times)
  search:    core (decode -> peaks -> hashes)  -> storage.query_hashes(unique hashes) -> Matcher
```

### Request flows

**Search (synchronous)**: `POST /api/v1/search`, multipart field `audio`.

1. `require_upload()` (`fingerprint/api/validators.py`) checks the extension against `formats.SUPPORTED_EXTENSIONS`. The upload is written to a `tempfile.mkstemp(prefix="audiofp-query-", suffix=<original extension>)` path (the extension lets the decoder pick a backend) that is removed when the request ends.
2. `Runtime.search_file()` builds `MatchOptions` from the runtime-adjustable defaults plus the form overrides `mode`, `top_k`, `min_confidence`, `min_aligned_hashes`, `min_peak_ratio` and `max_occurrences`.
3. `Fingerprinter.fingerprint_file(path, max_seconds=settings.max_query_seconds)` decodes and hashes the query. Decoding stops at `max_query_seconds` (default 3600 s); the response flags `query.truncated` when the decoded duration is within 0.5 s of that cap.
4. `Matcher.match(query, storage, options)` makes one `storage.query_hashes(unique_hashes, max_rows_per_hash=..., stats=...)` call, caps the join at `max_search_votes`, prefilters the candidate tracks in one vectorised pass and scores the survivors one by one; what it did is left in `Matcher.last_diagnostics` (`MatchDiagnostics`).
5. `format_search()` (`fingerprint/api/responses.py`) renders the JSON, including `last_diagnostics.to_dict()` as the `diagnostics` object.

**Upload and index (asynchronous)**: `POST /api/v1/tracks`.

1. The form fields `title`, `artist`, `tags` and `metadata` are run through `validate_track_changes()` (the same rules as `PATCH`: title/artist trimmed to 500 characters, tags normalised to at most 50, metadata an object of at most 64 KB) *before* anything is written, so a `400` never leaves an orphaned file. Then the file is saved under `settings.upload_dir_resolved` (`<data_dir>/uploads`) as `<8 hex chars>_<safe_filename(original name)>`; `safe_filename()` keeps the extension, which the decoder needs to pick a backend.
2. `Runtime.start_upload_job()` submits a job of type `upload`; the response is `202` with the job record.
3. On the job thread, `Indexer.index_file()` de-duplicates, fingerprints and calls `storage.add_track()`. A duplicate upload is deleted from disk; a failed one is deleted unless `keep_failed_uploads` is true. The runner then calls `storage.flush()`, so a single upload is durable at once even with SQLite's batched writes.
4. The client polls `GET /api/v1/jobs/{job_id}`.

**Index a server-side folder**: `POST /api/v1/tracks/index-directory` with `directory_path`, `recursive`, `tags`.

1. `clean_directory_path()` normalises the path without touching the filesystem (a missing or empty `directory_path` is a `400`). `Runtime.check_directory_allowed()` then enforces `allow_directory_indexing` and `index_roots` (with the production profile, `index_roots` must be set or the request is refused with `403 forbidden`), and only after that the route checks that the path is a directory (`400`): a forbidden path is never told whether it exists.
2. `find_media_files()` lists supported files (skipping `SKIP_DIRS` and dot-folders); `start_directory_job()` submits a job of type `directory`, or answers `429` when `JobManager.active_count()` has reached `max_concurrent_jobs * 4`.
3. `Indexer.index_paths()` fingerprints the files concurrently on the shared index pool, reports progress into the `Job` and calls `storage.flush()` at the end of the run. A `StorageError` from any file stops the run and is re-raised as `StorageError("Indexing stopped after N file(s): ...")`, so the job ends as `failed` with that message.

The CLI (`audiofp index`, `audiofp search`, `audiofp tracks`, `audiofp stats`) constructs the same `Runtime` and calls `Indexer` and `search_file()` directly; `audiofp db` and `audiofp doctor` open the store through `create_storage()` without a `Runtime`. Other entry points: `python run.py` (translates `--env` to `--profile` and runs `serve`), `fingerprint.api.wsgi:app` for gunicorn or waitress, and `create_app()` for embedding.

## Pipeline

### Indexing: decode, peaks, hashes, store

```
file --iter_audio_chunks()-----> float32 mono chunks at sample_rate (about chunk_seconds each)
     --PeakExtractor.push()----> magnitude STFT columns -> local maxima: (peak_times, peak_freqs)
     --generate_hashes()-------> (hashes int64, hash_times int32), sorted by anchor time
     --storage.add_track()-----> one tracks row + one fingerprints row per hash
                                 (hash_value, track_ref, time_offset = anchor frame);
                                 SQLite buffers the rows and writes them in sorted batches
                                 (storage.flush(), see the storage model)
```

`Indexer.index_file()` wraps this for one file and never raises for a bad file: it returns an `IndexOutcome` with `status` `indexed`, `duplicate` or `failed` (constants `OUTCOME_INDEXED`, `OUTCOME_DUPLICATE`, `OUTCOME_FAILED`) and an `error_code` such as `file_not_found`, `unsupported_format`, `empty_fingerprint`, `out_of_memory` or the code of the `AudioFPError` that was raised. Only `StorageError` propagates, because retrying the next file would not help. The steps are: existence and `formats.is_supported()` check; de-duplication according to `dedupe` (`content` = `sha256_file()` then `find_by_content_hash()`, `path` = `find_by_filepath()`, `none`); `fingerprint_file()`; `metadata_from_filename()` (an `Artist - Title.ext` name fills both fields, anything else becomes the title); building the `TrackRecord`; rejecting a fingerprint with zero hashes (`empty_fingerprint`); then, under the per-indexer `_store_lock`, the duplicate check is repeated right before `add_track()`, so two byte-identical files fingerprinted concurrently in one batch (several workers, same content) end up as one track and one `duplicate` outcome.

Default parameters and what they mean at the working rate:

| Setting | Default | Derived value |
|---|---|---|
| `sample_rate` | 11025 | Nyquist 5512.5 Hz; everything is resampled to this first |
| `n_fft` | 2048 | 1025 frequency bins, 5.38 Hz apart |
| `hop_length` | 512 | one frame = 512 / 11025 = 46.4 ms (`Runtime.info()["frame_seconds"]`) |
| `chunk_seconds` | 30.0 | decode and STFT block size; not part of the fingerprint signature |
| `peak_neighborhood_size` | 20 | 20 frames x 20 bins local-maximum window (about 0.93 s x 108 Hz) |
| `min_amplitude` | 10.0 | linear STFT magnitude, measured after peak normalisation |
| `fan_value` | 10 | at most 10 hashes per anchor peak |
| `min_hash_time_delta` | 0 | simultaneous peaks are paired |
| `max_hash_time_delta` | 200 | frames, about 9.3 s; the hash layout caps it at 4095 frames (about 190 s) |

The result of the pipeline is a `Fingerprint` dataclass: `peak_times`, `peak_freqs` (int32), `hashes` (int64), `hash_times` (int32 anchor frame per hash), `num_samples`, `sample_rate`, `hop_length`, the `gain` that normalisation implied (`1 / peak sample`, or 1.0) and a free-form `extra` dict. `frames_to_seconds()` on it converts frames back to seconds.

### Search

```
clip --fingerprint_file(max_seconds=max_query_seconds)--> query Fingerprint
     --np.unique(query.hashes)--> storage.query_hashes(unique, max_rows_per_hash, stats)
                                 -> (hash_value, track_ref, time_offset) rows; hashes with more than
                                    max_rows_per_hash rows in the library are skipped ("stop words")
     --join on hash_value------> one vote per (DB row x query anchor with that hash):
                                 offset = time_offset - query anchor frame
                                 (capped at max_search_votes: the most common hashes are dropped first)
     --group by track_ref------> vectorised prefilter, then Matcher._score_track() per surviving track
                                 -> TrackMatch with a list of Occurrence
     --sort by (aligned_hashes, peak_ratio) desc, keep top_k--> get_tracks_by_ref() -> format_search()
```

The join is vectorised: the query hashes are sorted once and every returned row is mapped to its query anchors with `np.searchsorted`, so a row whose hash occurs several times in the query produces several votes. The storage call is a single batch lookup regardless of how many hashes the query has. `MatchOptions` carries the two cost guards, `max_rows_per_hash` (default 2000, `0` disables) and `max_search_votes` (default 5 000 000), and `Matcher.last_diagnostics` records the counts (`query_hashes`, `db_rows`, `votes`, `candidate_tracks`, `scored_tracks`, `skipped_common_hashes`, `dropped_for_vote_cap`) that the API returns as `diagnostics`.

## Algorithm details

### Streaming decoder

`iter_audio_chunks(path, sample_rate, chunk_seconds, max_seconds=..., backend="auto", ffmpeg_binary=..., ffmpeg_timeout=3600.0, display_name=...)` yields float32 mono arrays already at the working sample rate, so an hour-long recording costs the same memory as a ten-second clip. `select_backend()` decides by extension:

| Extensions (`fingerprint/formats.py`) | Backend | Notes |
|---|---|---|
| `NATIVE_AUDIO_EXTENSIONS`: wav, wave, flac, ogg, oga, opus, mp3, aiff, aif, aifc, au, caf, w64 | `soundfile` (libsndfile) | No external binary. MP3 needs libsndfile >= 1.1.0, bundled with soundfile >= 0.12. |
| `FFMPEG_AUDIO_EXTENSIONS`: m4a, aac, wma, amr, ac3, dts, mka, weba | `ffmpeg` | `FFmpegNotFoundError` (`ffmpeg_not_found`, HTTP 422) if ffmpeg is not on PATH. |
| `VIDEO_EXTENSIONS`: mp4, mkv, avi, mov, wmv, flv, webm, m4v, mpeg, mpg, ts, mts, 3gp, vob | `ffmpeg` | Audio track only (`-vn -sn -dn`); `source_type` becomes `video`. |
| no extension | `soundfile`, then ffmpeg | libsndfile sniffs the content; ffmpeg is tried if that fails. |
| anything else | rejected | `UnsupportedFormatError` (`unsupported_format`, HTTP 415). |

*soundfile path* (`_iter_soundfile`): `sf.SoundFile(path).blocks(blocksize=max(chunk_seconds * src_rate, 4096), dtype="float32", always_2d=True)`; channels are averaged to mono; if the source rate differs, a `soxr.ResampleStream(src_rate, sample_rate, 1, dtype="float32", quality="HQ")` resamples each block with `resample_chunk(..., last=False)` and is flushed with `last=True` at the end. Streaming resampling matches resampling the whole signal at once to within float tolerance (`tests/test_core.py::test_soundfile_streaming_equals_whole_resample` asserts a maximum difference below 1e-4).

*ffmpeg path* (`_iter_ffmpeg`): runs `ffmpeg -nostdin -hide_banner -loglevel error -i <path> -vn -sn -dn -ac 1 -ar <sample_rate> -f s16le -acodec pcm_s16le pipe:1` (plus `-t` when `max_seconds` is set, so ffmpeg stops early instead of decoding audio that would be discarded), reads `chunk_seconds * sample_rate * 2` bytes (at least 4096 samples) at a time from stdout and converts int16 to float32 by dividing by 32768. A daemon thread drains stderr into a 20-line ring buffer so a chatty decoder cannot deadlock the pipe; the last lines are included in the `AudioDecodeError`. A `threading.Timer` kills ffmpeg after `ffmpeg_timeout` seconds. If the consumer stops early (truncation), ffmpeg is killed rather than left running. Because ffmpeg performs the resampling itself (`-ar`), its output is not sample-identical to the soundfile path for the same audio. The binary name comes from the `AUDIOFP_FFMPEG_BINARY` environment variable, read directly in `decoder.py` (`DEFAULT_FFMPEG`); it is not a `Settings` field.

*Fallback rule*: with `backend="auto"`, if libsndfile fails before the first chunk was yielded and ffmpeg is available, the file is retried through ffmpeg (libsndfile builds differ, notably in MP3 support). A failure after chunks were already yielded is raised as-is, because replaying from the start through ffmpeg would duplicate audio. `ffmpeg_info()` locates and version-checks the binary once per process (`lru_cache`); `audiofp doctor` and `GET /api/v1/health` report it.

### Chunked STFT with center-style padding

`PeakExtractor(n_fft, hop_length, neighborhood, min_amplitude, normalize=False)` computes the magnitude spectrogram incrementally with numpy:

- Window: periodic Hann, `scipy.signal.get_window("hann", n_fft, fftbins=True)`, float32.
- Padding: `n_fft // 2` zeros are placed in the sample buffer before the first chunk (`__init__`) and appended after the last one (`finish()`), so frame `i` is centred on original sample `i * hop_length`. This is the same frame grid as a `center=True` whole-signal STFT.
- Framing: `sliding_window_view(samples, n_fft)[::hop]`, `np.fft.rfft` along the window axis, `np.abs`, transposed to `(bins, frames)` float32. `_consume_buffer()` frames every complete window in the buffer and keeps only the samples still needed by the next frame, so the buffer never grows beyond a chunk plus one window.
- Normalisation, single pass: `push(chunk)` takes no gain argument. With `normalize=True` (what `Fingerprinter.fingerprint_chunks(chunks, normalize=True, extra=None)` and therefore `fingerprint_file(normalize=True)`, the default, use) the extractor tracks the running peak sample of the audio seen so far, keeps every candidate peak together with its magnitude against the *running* threshold `min_amplitude * running peak` (a superset of the final set, because the threshold can only rise), and applies the exact final threshold `min_amplitude * peak` in `finish()`. The audio itself is never rescaled and the file is decoded once; the result is bit-identical to fingerprinting the peak-normalised signal (`test_gain_changes_threshold_not_positions` asserts equality with an explicitly scaled copy). `PeakExtractor.gain` reports the implied gain `1 / peak`, stored on the `Fingerprint`. This is why `min_amplitude` is meaningful across files recorded at different levels; normalisation only moves peaks across the threshold, never their positions.

**Exact equivalence to whole-signal processing.** Peak picking needs context on both sides of a column. `_process()` concatenates `[ctx, pending, new columns]`, runs the maximum filter over that block, and emits only columns `[n_ctx, total - margin)`, where `margin = max(neighborhood, 1)`. The last `margin` emitted columns are kept as `_ctx` for the next block and the last `margin` columns of the block stay `_pending` until more audio arrives; on `finish()` everything pending is emitted. scipy's `maximum_filter(size=k)` looks `k // 2` columns back and `(k - 1) // 2` ahead, so every emitted column sees exactly the neighbours it would see in a whole-signal spectrogram; the `mode="reflect"` boundary only ever applies at the true start and end of the signal, where a whole-signal computation reflects too. `test_chunked_peaks_identical_to_whole_signal` asserts `np.array_equal` against an independent explicit-loop implementation for chunk sizes 10**9, 50000, 4096, 2048 and 777 samples. This guarantee is what allows `chunk_seconds` to be excluded from the fingerprint signature: changing it never changes a fingerprint.

### Peak picking

`_pick()` marks a spectrogram cell as a peak when `maximum_filter(block, size=peak_neighborhood_size, mode="reflect") == block` (it is the maximum of its `size x size` neighbourhood over frames and bins) **and** `block > threshold` (strictly greater, linear magnitude), where the threshold is `min_amplitude` with `normalize=False` and `min_amplitude * running peak sample` with `normalize=True`. `finish()` re-applies the exact final threshold `min_amplitude * peak` to the kept candidates in the normalised case, then returns `(peak_times, peak_freqs, num_samples)` sorted by `np.lexsort((f, t))`, i.e. by time then frequency, which makes the downstream hash order deterministic. A smaller `peak_neighborhood_size` gives denser constellations (more hashes, more storage); a higher `min_amplitude` drops quiet peaks.

### Hash layout

`generate_hashes(peak_times, peak_freqs, fan_value, min_time_delta, max_time_delta)` pairs each peak (the *anchor*) with later peaks (the *targets*) and encodes each pair as one 36-bit value inside an int64 (`encode_hash`):

```
hash = (f_anchor << 24) | (f_target << 12) | delta_t
        12 bits            12 bits           12 bits      FREQ_BITS = DELTA_BITS = 12
```

`FREQ_MASK = DELTA_MASK = 4095`, so frequency bins and frame deltas up to 4095 fit. Two checks enforce this: `generate_hashes` clamps `max_time_delta` to `MAX_TIME_DELTA` (4095) with `min()`, and `Settings.validate()` refuses `n_fft > 8190` (bin index `n_fft // 2` must fit in 12 bits) and requires `0 <= min_hash_time_delta <= max_hash_time_delta <= 4095`. `decode_hash()` inverts the packing for debugging. Hash values depend only on audio content, never on the track, which is what makes the inverted index possible. Each hash is stored with its anchor's absolute frame index (`hash_times`), the value the matcher votes with.

### Fan-out

Peaks are sorted by `(time, frequency)`. For anchor `i`, the candidate targets are the next `fan_value` peaks in that order starting at `first_target[i]`:

- `min_hash_time_delta == 0` (default): `first_target = i + 1`, so peaks at the *same* frame (the harmonics of one chord) are paired with `delta_t = 0`. Such pairs describe timbre rather than sequence; they are kept because strong harmonics survive noise well, and the matcher separately requires an alignment to span several distinct frames (`MIN_ALIGNED_FRAMES`, below) so a single shared chord can never pass as a match.
- `min_hash_time_delta >= 1`: `first_target = searchsorted(t, t + min_time_delta)`, the first peak at least that many frames later. Use `1` for very repetitive tonal material.

Pairs with `delta_t > max_time_delta` are dropped, not replaced, so `fan_value` is an upper bound per anchor. The loop over `k in range(fan_value)` is vectorised across all anchors at once; `test_vectorised_hashes_equal_naive` checks it against a naive reference. Output arrays are sorted by anchor time (stable).

### Offset-histogram voting

For each candidate track, `_Votes` holds one vote per shared hash: `offsets = track_frame - query_frame`, plus the query anchor frame (`qtimes`) and the hash value (`qhashes`). Audio that is genuinely the same lines up at one offset and produces a sharp spike in the histogram of offsets; unrelated audio with coincidental hash collisions produces a flat, noisy histogram.

**Smoothing (`_smoothed_histogram`).** `np.unique(offsets, return_counts=True)` gives the raw histogram; the smoothed count of a bin adds the raw counts of every existing bin within `+/- offset_tolerance_frames` (default 1). Clips rarely start on a frame boundary and noise shifts a peak by a frame, so the votes of a true alignment straddle neighbouring bins.

**Early rejection.** `_score_track()` takes `best_i = argmax(smoothed)`; if `smoothed[best_i] < min_aligned_hashes` the track is dropped before any further work, because raw votes are an upper bound on the distinct-hash count computed later.

**Distinct-hash counting.** For a candidate offset, the votes within `[offset - tol, offset + tol]` are read from an offset-sorted view (`searchsorted` slice, not a scan of every vote), and `aligned_hashes = _count_distinct(qhashes[slice])`, the number of *distinct query hashes* voting there (`_count_distinct` is a sort-plus-`np.diff` helper, several times faster than `np.unique` on large arrays; `_QueryIndex.distinct_in_span()` uses it too). Counting distinct hashes rather than raw votes neutralises sustained tones, which repeat one hash for many frames and would otherwise fake an alignment.

**Stop words, vote cap and prefilter.** Before any per-track work, three vectorised guards bound the cost of a query. (1) The storage lookup skips query hashes that occur more than `max_rows_per_hash` times in the library (`stats["skipped_hashes"]`, reported as `skipped_common_hashes`): such hashes carry almost no information but would multiply the vote count. (2) If the join would still exceed `max_search_votes` votes, `_cap_votes()` groups the returned rows by hash, sorts the hashes by the votes they contribute and drops the heaviest ones until the total fits the budget; the number of rows dropped is `dropped_for_vote_cap` and a warning naming `AUDIOFP_MAX_SEARCH_VOTES` is logged. (3) The votes are bucketed by `(track_ref, offset)` in one `np.unique` call and a track is only scored when its best raw offset bin times `2 * offset_tolerance_frames + 1` reaches `min_aligned_hashes` (smoothing can at most add that many neighbouring bins), so a large library full of chance coincidences costs a few numpy calls rather than a Python loop per track. `candidate_tracks` counts the tracks with any vote, `scored_tracks` the ones that reached `_score_track()`.

**Background and `peak_ratio` (`_background`).** The background is the mean *raw* vote count over non-empty offset bins that are "away" from every possible alignment, with a floor of 1.0:

- "strong" bins are those with `smoothed >= SPIKE_VOTES` (a module constant, 10) together with the best bin;
- a bin counts as away when its distance to the nearest strong bin exceeds `2 * tol + 2` (the spike and its jitter votes);
- if no bin is away, the background is 1.0.

`peak_ratio = aligned_hashes / background`. The `matcher.py` module docstring reports that chance matches sit around 3-9 regardless of library size while real matches are typically well above 10; the `min_peak_ratio` default of 12 sits just above that range. Excluding every strong bin, not just the best one, matters for the occurrences use case: a pattern that appears five times in a recording produces five spikes, and a plain mean over all bins would be dominated by the matches themselves and hide them. `SPIKE_VOTES` is deliberately a constant rather than `min_aligned_hashes`, so lowering the match threshold does not silently change how sharpness is measured. `MIN_ALIGNED_FRAMES` (3) is the other constant guard: the aligned votes must come from at least three distinct query frames, otherwise one instant (a chord, a click) that produced many simultaneous hash hits is rejected.

**Confidence, measured locally.** `_robust_span(aligned_times)` gives the query frame span covered by the aligned votes: min/max when there are at most 20 votes, otherwise the 2nd to 98th percentile (trimming stray coincidences). Then

```
confidence = min(1.0, aligned_hashes / max(distinct query hashes with an anchor inside [q_start, q_end], 1))
```

using `_QueryIndex.distinct_in_span()`. Normalising by the *matched region* instead of the whole query keeps the score meaningful for a short clip against a long track, a short indexed pattern found inside a long recording, and two long recordings that share one segment. Both `confidence` (4 decimals) and `peak_ratio` (2 decimals) are rounded in the `Occurrence`.

### Occurrence detection

`_find_occurrences()` examines candidate bins strongest first and returns up to `limit` occurrences: `max_occurrences_per_track` (default 25; the API form field is `max_occurrences`, capped at 500) in `occurrences` mode, exactly 1 in `identify` mode.

1. Cheap floor: only bins with `smoothed >= max(min_aligned_hashes, ceil(min_peak_ratio * background))` can pass, because smoothed raw votes bound the distinct-hash count. They are sorted by smoothed count descending and truncated to `MAX_CANDIDATE_BINS` (400). Very repetitive material (hold music, a looped tone) can put thousands of bins above the floor; examining the strongest 400 first means real matches are never lost to the cap while the cost stays bounded (`test_occurrences_on_highly_repetitive_audio_is_bounded` requires a 5-minute loop to score in under 5 s).
2. Per candidate: compute `aligned_hashes` and `peak_ratio`; reject if below `min_aligned_hashes` or `min_peak_ratio`; reject if fewer than `MIN_ALIGNED_FRAMES` distinct query frames; compute the span and `confidence`; reject if below `min_confidence`.
3. Span-based suppression: a surviving candidate is dropped when it is the *same event* as an already-kept occurrence, meaning its offset is within `2 * tol + 1` frames of a kept offset, or `_same_region()` holds: its query span and its track span both overlap the kept ones by more than half of the shorter span (`_overlaps`). Offset jitter therefore never produces a second occurrence, while the same pattern at two different places in the recording does.
4. Ordering: the strongest occurrence (by `aligned_hashes`, then `peak_ratio`) first, the rest by ascending offset. `TrackMatch.best` is `occurrences[0]`, and the track-level `aligned_hashes`, `confidence`, `peak_ratio` and `offset_frames` are copied from it; `matched_rows` is the total number of votes the track received.

Tracks are then ranked by `(aligned_hashes, peak_ratio)` descending, not by confidence, and cut to `top_k`.

### Signed offsets

`Occurrence.offset_frames = track_frame - query_frame`. Each occurrence also carries `query_start_frames` / `query_end_frames` (the span from `_robust_span`) and derives `track_start_frames = query_start_frames + offset_frames`, likewise for the end.

- **Positive offset**: the query begins `offset` frames into the track. The classic case: a 10 s clip identified at 1:23 of a song.
- **Negative offset**: the indexed track starts *after* the query does, i.e. the track's content is found inside the query at `-offset`. This is how pattern search works: index the short pattern (a jingle, a compliance disclaimer, hold music) as a track, then search with the long call recording in `occurrences` mode; each occurrence says where in the recording the pattern occurs (`test_occurrences_mode_finds_pattern_twice_and_negative_offsets`).

`format_match()` / `_occurrence()` in `fingerprint/api/responses.py` expose both readings: `offset_sec` (signed), `track_offset_sec = max(0, offset_sec)`, `query_offset_sec = max(0, -offset_sec)`, `query_start_sec`, `query_end_sec`, `track_start_sec`, `track_end_sec` (the track values clamped at 0), and `match_offset_sec` as a 1.x alias of `track_offset_sec`. Span ends are anchor-based, so they run slightly short of the true end of a pattern (anchors near the end pair with peaks outside it).

### Thresholds and quality labels

`MatchOptions.from_settings(settings, **overrides)` clamps every threshold: `top_k` to `[1, max_top_k]`, `min_aligned_hashes >= 1`, `min_confidence` to `[0, 1]`, `min_peak_ratio >= 0`, `offset_tolerance_frames >= 0`, `max_occurrences_per_track >= 1`; an unknown `mode` raises `MatchingError` as a 400 `validation_error`. It also copies the two cost guards `max_rows_per_hash` and `max_search_votes` from `Settings` (validated there as `>= 0` and `>= 10000`; they are not request fields). `Runtime.match_options()` layers the runtime-adjustable defaults (`RUNTIME_SETTING_KEYS = top_k, min_confidence, min_aligned_hashes, min_peak_ratio, mode`, persisted in `<data_dir>/runtime-settings.json` and edited through `PUT` or `PATCH /api/v1/settings`) under the per-request overrides. The search response echoes the effective values in `thresholds`.

`quality_label(confidence, peak_ratio)` buckets a result for the UI and CLI: `strong` when `confidence >= 0.15 and peak_ratio >= 30`, `likely` when `confidence >= 0.05 and peak_ratio >= 18`, otherwise `weak`.

## Storage model

### Contract

`fingerprint/storage/base.py` defines `StorageBackend`, the abstract class every backend implements and that `tests/test_storage.py` runs against each backend (memory and SQLite always; PostgreSQL when `AUDIOFP_TEST_POSTGRES_DSN` is set). A backend stores *tracks* and the inverted index `hash -> (track, frame)`:

| Method | Purpose |
|---|---|
| `initialize(signature, params, compat)` | Fingerprint-parameter compatibility check (below) |
| `get_meta(key)` / `set_meta(key, value)` | Small key-value table |
| `add_track(record, hashes, times)` | Persist a track and its fingerprints; adding an existing `track_id` replaces it atomically |
| `get_track`, `get_tracks_by_ref`, `find_by_content_hash`, `find_by_filepath`, `list_tracks`, `update_track`, `delete_track`, `delete_tracks`, `count_tracks` | Track metadata |
| `query_hashes(hash_values, max_rows_per_hash=None, stats=None)` | Batch lookup returning three aligned int64 arrays `(hash_value, track_ref, time_offset)`; hashes with more than `max_rows_per_hash` rows are skipped and counted in `stats["skipped_hashes"]` |
| `flush()` | Write any buffered fingerprints, returning the number of rows written (a no-op returning 0 in the base class; `SQLiteStore` overrides it). Called by `Indexer.index_paths()` at the end of every run, by the upload job runner, and by `SQLiteStore` itself on `close()` |
| `get_stats()` | Cheap statistics that never scan the fingerprint table |
| `clear()` | Delete every track and fingerprint, keep meta |
| `close()`, `health_check()` | Lifecycle |

`TrackRecord` carries `track_id` (a UUID4 string, the public identifier), `ref` (a small integer assigned by the backend and never exposed: `to_dict()` drops it), `title`, `artist`, `filename`, `filepath`, `content_hash`, `duration`, `num_peaks`, `num_hashes`, `source_type` (`audio` or `video`), `file_size`, `indexed_at`, `tags` and `metadata`. Fingerprint rows reference `ref`, not the UUID, which keeps the by far largest table compact. Clients may edit only `title`, `artist`, `tags` and `metadata` (`validate_track_changes()`); `normalize_tags()` lower-cases, strips, de-duplicates and keeps at most 50 tags; metadata must serialise to at most 64 KB of JSON. `SORTABLE_FIELDS` limits `list_tracks(sort=...)` to `indexed_at`, `title`, `artist`, `duration`, `filename`, `num_hashes`.

`create_storage(settings, check_compat=True)` picks the backend from `storage_type` (`memory`, `sqlite`, `postgres`) and runs `initialize()`; on failure it closes the store and re-raises.

### Fingerprint signature and compatibility check

Hash values encode frequency bins and frame deltas, so a database built with one `sample_rate`, `n_fft`, `hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`, `min_hash_time_delta` or `max_hash_time_delta` is silently useless with another. `Settings.fingerprint_params()` collects exactly those fields (the ones whose metadata has `fingerprint=True`) plus `algorithm_version = FINGERPRINT_ALGORITHM_VERSION` (currently 2, bumped when a code change alters the hash layout, STFT framing or peak rules). `Settings.fingerprint_signature()` is the first 16 hex characters of the SHA-256 of that dict serialised as compact, key-sorted JSON.

`StorageBackend.initialize()` compares the signature with the meta keys `META_SIGNATURE = "fingerprint_signature"` and `META_PARAMS = "fingerprint_params"`:

- no stored signature and zero tracks: stamp `fingerprint_signature`, `fingerprint_params` and `created_at`;
- no stored signature but tracks exist: return without stamping or raising;
- equal signature, or `fingerprint_compat = ignore`: continue;
- different signature: `warn` logs and continues; `strict` (the default) raises `FingerprintCompatibilityError` (code `fingerprint_incompatible`, HTTP 503) that names both signatures and the stored parameters.

The way out is to restore the original `AUDIOFP_*` fingerprint settings, point `AUDIOFP_SQLITE_PATH` / `AUDIOFP_POSTGRES_DSN` at a new database, or `audiofp db reset --yes`, which clears the store and re-stamps it with the current parameters. `audiofp db check` opens the store with the check enabled and reports the stored signature; `GET /api/v1/health` and `/api/v1/info` show the running signature.

### SQLite (`SQLiteStore`, the default)

`sqlite_path` defaults to `<data_dir>/fingerprints.db`. Design points, all in `fingerprint/storage/sqlite_store.py`:

- **Connections**: one `sqlite3` connection per thread (`threading.local`), `isolation_level=None` (explicit `BEGIN` / `COMMIT`), and a process-wide `_write_lock` (`RLock`) so concurrent indexing workers queue instead of fighting over `SQLITE_BUSY`. Every connection is also recorded with its owning thread; connections of threads that have exited are closed lazily (`_prune_dead_connections()`, run when a connection is opened and by the `open_connections` property), so short-lived worker threads do not leak file handles. A `db_path` of `:memory:` is refused with a `StorageError` (such databases are per connection and could not be shared between threads; use `storage_type=memory`). Per-connection pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-<sqlite_cache_mb * 1024>` (KiB), `temp_store=MEMORY`, `mmap_size=<sqlite_mmap_mb> MB`, `busy_timeout=30000`, `foreign_keys=ON`. The constructor also takes `write_batch_rows` (`sqlite_write_batch_rows`, default 2 000 000) and `track_index` (`sqlite_track_index`, default off).
- **Schema** (`_SCHEMA_V2`, plus the v3 step `_SCHEMA_V3 = ("ALTER TABLE tracks ADD COLUMN flushed INTEGER NOT NULL DEFAULT 1",)`):

  ```sql
  CREATE TABLE meta   (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  CREATE TABLE tracks (ref INTEGER PRIMARY KEY, track_id TEXT NOT NULL UNIQUE, title, artist, filename,
                       filepath, content_hash, duration REAL, num_peaks, num_hashes, source_type,
                       file_size, indexed_at REAL NOT NULL, tags TEXT DEFAULT '[]', metadata TEXT DEFAULT '{}');
  CREATE INDEX idx_tracks_content_hash ON tracks (content_hash);
  CREATE INDEX idx_tracks_filepath     ON tracks (filepath);
  CREATE INDEX idx_tracks_indexed_at   ON tracks (indexed_at);
  CREATE TABLE fingerprints (
      hash_value  INTEGER NOT NULL,
      track_ref   INTEGER NOT NULL,
      time_offset INTEGER NOT NULL,
      PRIMARY KEY (hash_value, track_ref, time_offset)
  ) WITHOUT ROWID;
  ```

  `fingerprints` is a `WITHOUT ROWID` table whose primary key `(hash_value, track_ref, time_offset)` is the clustered B-tree: it is both the storage and the lookup index, with no redundant secondary index (the 1.x layout was a rowid table with a TEXT `song_id` plus a covering index over the same three columns, so every fingerprint was stored twice). Rows are always inserted in key order: `_insert_rows()` sorts them with `np.lexsort((times, refs, hashes))` and converts them to Python tuples `INSERT_SLICE` (200 000) rows at a time for `INSERT OR IGNORE`, so the B-tree fills sequentially and the transient memory stays bounded. The trade-off: by default there is no index on `track_ref` and no foreign key, so `DELETE FROM fingerprints WHERE track_ref=?` (the replace-on-re-add path in `add_track()` and the start-up repair) scans the clustered index; `delete_tracks()` deletes the fingerprints of a whole batch with `WHERE track_ref IN (...)`, 500 refs per statement, so a bulk delete costs one scan per 500 tracks rather than one per track. With `sqlite_track_index=true` the store creates `idx_fp_track ON fingerprints (track_ref)` at start-up, which makes deletes indexed at the price of roughly doubling the disk. `tags` and `metadata` are JSON text; `list_tracks(tag=...)` matches with an escaped `LIKE`.
- **Batched writes**: inserting one track's hashes touches a random leaf page per hash, so on a large library every track would rewrite hundreds of MB of B-tree. `add_track()` therefore inserts the `tracks` row immediately with `flushed=0` and appends the `(ref, hashes, times)` arrays to an in-memory buffer (`_pending`, about 16 bytes per row); `flush()` writes everything buffered in *one* sorted transaction (`_insert_rows()` followed by `UPDATE tracks SET flushed=1`) when the buffer reaches `write_batch_rows`, at the end of every indexing run (`Indexer.index_paths()`), after every upload job, on `close()`, and before `vacuum()`, `checkpoint()` and `unique_hash_count()`. Buffered tracks are searchable: `query_hashes()` also scans the pending buffer (`_pending_matches()`). If the flush transaction fails the rows stay buffered for a retry and a `StorageError` is raised. At start-up `_repair_unflushed()` deletes every track still marked `flushed=0` (the process died before a flush) and logs a warning listing the first names, so those files are simply re-indexed on the next run and the library never contains a track that cannot be matched. `write_batch_rows=0` restores immediate per-track writes. `get_stats()` reports the buffer size as `pending_rows`.
- **Batch lookup**: `query_hashes()` creates `TEMP TABLE query_hashes (hash_value INTEGER PRIMARY KEY)`, fills it inside one transaction (in autocommit mode each row would be its own commit), and, when `max_rows_per_hash` is set, first runs a `GROUP BY q.hash_value HAVING COUNT(*) > ?` pre-query over the join and deletes the heavy hashes from the temp table (their number goes to `stats["skipped_hashes"]`). It then runs `SELECT f.hash_value, f.track_ref, f.time_offset FROM query_hashes AS q CROSS JOIN fingerprints AS f ON f.hash_value = q.hash_value` and appends the matching rows from the pending buffer. One statement regardless of query size; no chunking around the SQLite bound-variable limit.
- **Schema version**: `PRAGMA user_version` holds `SCHEMA_VERSION = 3`. `_init_schema()` rejects a database with `user_version == 0` that has a `songs` table and no `meta` table as an AudioFP 1.x file (`FingerprintCompatibilityError`: the hash layout changed and cannot be upgraded in place; point `AUDIOFP_SQLITE_PATH` at a new file and re-index). A `user_version` greater than 3 raises `StorageError` asking to upgrade AudioFP. Anything lower runs `_migrate()`, which applies the missing steps (`_SCHEMA_V2` below version 2, `_SCHEMA_V3` below version 3) in one `BEGIN IMMEDIATE` transaction and sets the version; a v2 file from an earlier 2.0 build is upgraded in place automatically (the existing rows get `flushed=1`).
- **Stats**: `get_stats()` reads `COUNT(*)`, `SUM(num_hashes)` and `SUM(duration)` from `tracks` plus the on-disk size of the `.db` and `-wal` files and the buffer's `pending_rows`, so the UI can poll `GET /api/v1/stats` freely. `unique_hash_count()` (a full index scan) exists only for `audiofp stats --full`. `vacuum()` and `checkpoint()` (`PRAGMA wal_checkpoint(TRUNCATE)`) back `audiofp db vacuum`; all three flush the buffer first.

### Memory (`MemoryStore`)

For tests, demos and the `testing` profile; nothing survives a restart. The inverted index is a list of sorted *segments*, one `(hashes sorted, refs, times)` int64 triple per added track, so adding a track never re-sorts the whole library; once there are more than `MAX_SEGMENTS` (64) segments `_compact()` merges them into one. Deleted tracks are only masked (`_deleted_refs`, filtered out of every lookup) until the next compaction, which also runs once more than `MAX_SEGMENTS` refs are pending deletion. `query_hashes()` is a vectorised `searchsorted` per segment rather than a dict lookup per hash, applies `max_rows_per_hash` with `np.unique(..., return_counts=True)` and fills `stats["skipped_hashes"]`. Readers and writers share one `RLock`; records are deep-copied on the way in and out.

### PostgreSQL (`PostgresStore`)

For multi-process or multi-node deployments; needs the optional extra `pip install "audiofp[postgres]"` (`psycopg[binary,pool]`, psycopg 3) and `postgres_dsn`. Connections come from a `psycopg_pool.ConnectionPool(min_size=1, max_size=postgres_pool_size)` that is waited on at start (`connect_timeout`, 15 s) so a bad DSN fails fast with a `StorageError` whose message redacts the password (`_redact()`) instead of a pool timeout deep inside the first query. The schema mirrors SQLite with PostgreSQL types (`ref SERIAL`, `tags` / `metadata` as `JSONB`, `file_size BIGINT`) with these differences: `fingerprints` is a plain heap table with `track_ref INTEGER REFERENCES tracks(ref) ON DELETE CASCADE`, a covering index `idx_fp_hash ON fingerprints (hash_value) INCLUDE (track_ref, time_offset)` that serves lookups straight from the index, and `idx_fp_track ON fingerprints (track_ref)` so deletes are cheap. Fingerprints are bulk-loaded with `COPY fingerprints (hash_value, track_ref, time_offset) FROM STDIN` in text format, written as chunks of 200 000 tab-separated rows rather than one `write_row()` call per row, and `query_hashes()` is one `SELECT ... WHERE hash_value = ANY(%s)` round trip, preceded by a `GROUP BY hash_value HAVING COUNT(*) > %s` query that removes the hashes above `max_rows_per_hash` (counted in `stats["skipped_hashes"]`). The schema version is recorded as the meta key `schema_version` (2) with `ON CONFLICT (key) DO NOTHING`; there is no migration logic for PostgreSQL. `list_tracks(query=...)` escapes `%`, `_` and `\` and uses `ILIKE ... ESCAPE '\'` like SQLite; `list_tracks(tag=...)` uses the JSONB `?` operator; `clear()` is `TRUNCATE fingerprints, tracks RESTART IDENTITY`.

## Background jobs

`fingerprint/jobs/manager.py` is storage-agnostic and dependency-free so it can host other job types later. `Runtime` creates one `JobManager(max_concurrent_jobs, history_limit=job_history_limit, persist_dir=jobs_dir if persist_jobs else None, max_errors=job_max_errors)`.

- **Execution**: a `ThreadPoolExecutor(max_workers=max_concurrent_jobs, thread_name_prefix="audiofp-job")` runs `runner(job)`; `submit(job_type, label, runner, total=..., meta=...)` returns the `Job` immediately. Status transitions: `pending` -> `running` -> `completed`, `failed` (any exception; the message becomes `job.error`) or `cancelled`. `TERMINAL` is the set of finished states. While a job runs, `job_id_var` is set so every log line carries the job id.
- **Progress**: runners call `job.update(...)` (thread-safe; counters `total`, `completed`, `succeeded`, `failed`, `skipped`, `current_item`) and `job.add_error(item, message, code, limit)`, which keeps at most `job_max_errors` (default 500) entries. `Job.to_dict()` derives `percent`, `elapsed_sec`, `eta_sec` and `rate_per_sec`; `GET /api/v1/jobs` omits the error list unless `include_errors=true`.
- **Persistence**: with `persist_jobs` (default true) every job is written as `<data_dir>/jobs/<job_id>.json`, atomically (temp file then `os.replace`). State changes persist immediately; progress updates through `JobManager.touch()` are throttled to one write per 2 s per job. `_trim_history()` keeps at most `job_history_limit` (default 200) finished jobs in memory and on disk.
- **Interrupted jobs**: on start-up `_load_history()` reads the JSON files; any job still recorded as `pending` or `running` is marked `interrupted` with the error "The server restarted before this job finished. Files already indexed were kept; re-run to index the rest." `interrupted` is assigned only here.
- **Cancellation**: `cancel(job_id)` (`POST /api/v1/jobs/{job_id}/cancel`) sets the job's cancel event and `cancel_requested`. A job that is still `pending` is cancelled immediately when `Future.cancel()` succeeds. A running job is cancelled cooperatively: `Indexer.index_paths()` polls `job.should_cancel()`, stops submitting new files, and lets the files already in flight finish, then the summary is marked `cancelled`; a runner can also call `job.check_cancelled()`, which raises `JobCancelled`. Cancelling a terminal job is a `job_error` (400). `remove()` (`DELETE /api/v1/jobs/{job_id}`) is allowed only for terminal jobs and deletes the file. `shutdown()` (via `Runtime.close()`, registered with `atexit`) sets every active job's cancel event and turns still-pending jobs into `cancelled`.
- **Shared worker pool**: fingerprinting threads are separate from job threads. `Runtime` holds one `Indexer` whose `executor` is a lazily created `ThreadPoolExecutor(max_workers=settings.effective_index_workers, thread_name_prefix="audiofp-index")`; `index_workers = 0` (default) means `min(4, CPU count)`. A `directory` job calls `Indexer.index_paths()`, which submits files onto that shared pool in a sliding window of `2 x workers` so cancellation is prompt and memory stays bounded for 100 000-file runs; several directory jobs therefore share the same fingerprinting threads. An `upload` job calls `Indexer.index_file()` directly on its job thread, so it occupies a job slot but not an index-pool slot. A `StorageError` from a worker stops the whole run instead of failing every remaining file individually: `index_paths()` stops submitting, waits for the files in flight, flushes what it can and then raises `StorageError("Indexing stopped after N file(s): <cause>")` (with `indexed`, `failed` and `duplicates` counts in `details`), so the job ends as `failed` with that message rather than as `completed`; the files indexed before the failure are kept. Every run, whatever its outcome, ends with `storage.flush()`.

## API layer

### Application factory and `Runtime`

`create_app(settings=None, *, profile=None, storage=None, configure_logs=True)` in `fingerprint/api/app.py` loads `Settings` (from env and `.env` unless given), configures the `fingerprint` logger hierarchy (console plus the rotating file named by `Settings.log_file_resolved`: `log_file=auto`, the default, means `<data_dir>/logs/audiofp.log` in the production profile and console-only elsewhere; `none` or an empty value disables the file; an unwritable log directory logs a warning and continues console-only), builds `Flask("audiofp")` with `MAX_CONTENT_LENGTH = max_upload_mb` MB, optional CORS (`cors_origins`, exposing `X-Request-ID`) and optional `ProxyFix` (`trust_proxy`), then creates the single `Runtime` and stores it in `app.extensions["audiofp"]`; route modules fetch it with `routes.runtime()`. `Runtime.close()` is idempotent and runs on shutdown: `audiofp serve` calls it in a `finally` block after the server loop returns, and it is also registered with `atexit` as a fallback. It cancels running jobs (they stop at the next file boundary and persist a `cancelled` record), waits for them, closes the fingerprint pool and closes the storage, which flushes any buffered SQLite rows. `audiofp serve` also installs a `SIGTERM` handler (`_install_shutdown_signals()` in `fingerprint/cli.py`) that raises `SystemExit`, because Python's default disposition would kill the process without running either hook; waitress and the Flask development server both unwind cleanly on it, so `systemctl stop` and `docker stop` are clean shutdowns.

`Runtime` (`fingerprint/api/runtime.py`) owns the long-lived objects: `settings`, `storage` (from `create_storage()`), `fingerprinter`, `matcher`, `indexer` and `jobs`, plus the runtime-adjustable search defaults. Route handlers stay thin (parse, call a `Runtime` method, format) and the CLI reuses the object without Flask. `Runtime.health()` backs `GET /api/v1/health` (`ok` or `degraded` with HTTP 503 when `storage.health_check()` fails) and `Runtime.info()` backs `GET /api/v1/info` (formats, limits, feature flags, fingerprint parameters and signature, runtime defaults, `frame_seconds`).

### Request lifecycle, request ids and logging

`before_request` assigns `g.request_id` from the incoming `X-Request-ID` header (accepted only if it is 1-64 characters of `[A-Za-z0-9._:-]`) or a fresh 12-hex-character id, stores it in the `request_id_var` context variable, and, for any path under `/api/`, runs `check_request()`. `after_request` echoes `X-Request-ID`, sets `X-Content-Type-Options: nosniff`, sets `Cache-Control: no-store` for API responses and, when `access_log` is on, logs one line per API request with its path, query string, status and duration; the values of the `token` and `api_key` query parameters are replaced by `***` in that line (`_loggable_path()`, `_SECRET_QUERY_KEYS`). `ContextFilter` in `fingerprint/utils/logging.py` injects `request_id` and `job_id` into every log record (the text format shows the request id in brackets; the JSON format emits both as keys), so an error's `request_id` can be found in the logs.

### Error envelope

`register_error_handlers()` in `fingerprint/api/errors.py` renders every error, whether an `AudioFPError`, a Werkzeug `HTTPException` or an unexpected exception, through `error_response()`:

```json
{"error": "human-readable message", "code": "machine_code", "status": 415, "details": {"extension": ".xyz"}, "request_id": "3f9c1a0b2d4e"}
```

`details` and `request_id` are present only when available; `error` stays a plain string for 1.x clients. The `code` and HTTP status come from the exception hierarchy in `fingerprint/utils/exceptions.py`: `validation_error` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404, `conflict` 409, `payload_too_large` 413, `unsupported_format` 415, `audio_processing_error` / `audio_decode_error` / `ffmpeg_not_found` 422, `storage_error` / `fingerprint_incompatible` 503, `matching_error` / `configuration_error` / `internal_error` 500, `job_error` 400. Flask's own 413 is rewritten to mention `AUDIOFP_MAX_UPLOAD_MB`; the JSON routes raise their own `413 payload_too_large` (`JSON body too large (max 1024 KB)`) for bodies over `MAX_JSON_BODY_BYTES` (1 MiB) and `411 length_required` for a chunked body (`Transfer-Encoding` set, no `Content-Length`) (`check_json_body_size()` in `fingerprint/api/validators.py`, called by `_json_body()` in `routes/tracks.py` and by `update_settings()` in `routes/system.py`); unexpected exceptions become a 500 `internal_error` whose message points at the request id.

### Authentication

`fingerprint/api/auth.py`: when `api_key` is set, every request under `/api/` except `PUBLIC_PATHS` (`/api/v1/health`, `/api/v1/openapi.json`) and `OPTIONS` preflights must present the key as `X-API-Key: <key>` or `Authorization: Bearer <key>`. `GET` requests to the audio stream paths (`STREAM_PATH`) alternatively accept `?token=`, a short-lived, track-scoped HMAC token minted by `GET /tracks/<id>/stream-token` (`make_stream_token` / `verify_stream_token`, `STREAM_TOKEN_TTL` = 3600 s), because `<audio>` elements cannot send headers; the key itself is never accepted in a URL. Comparison uses `hmac.compare_digest`. A missing or wrong key is a 401 `unauthorized`. There is one shared secret; per-user keys, SSO or rate limiting belong in a reverse proxy.

### Endpoints

All under `/api/v1` (`api_bp`); the full request and response shapes are in [API.md](API.md) and in the hand-maintained OpenAPI document (`build_openapi()` in `fingerprint/api/openapi.py`, served at `/api/v1/openapi.json`; `tests/test_api.py::test_openapi_covers_every_route` keeps it in sync with the registered routes).

| Method and path | Module | Purpose |
|---|---|---|
| `POST /search` | `routes/search.py` | Identify a clip or find every occurrence of a pattern |
| `POST /tracks` | `routes/tracks.py` | Upload and index in the background (202 + job); alias `POST /upload` |
| `POST /tracks/index-directory` | `routes/tracks.py` | Index a server-side folder (202 + job); alias `POST /index` |
| `GET /tracks`, `GET /tracks/{track_id}` | `routes/tracks.py` | List (search, sort, paginate) and fetch; aliases under `/songs` |
| `PATCH` or `PUT /tracks/{track_id}` | `routes/tracks.py` | Edit `title`, `artist`, `tags`, `metadata` |
| `DELETE /tracks/{track_id}`, `POST /tracks/bulk-delete` | `routes/tracks.py` | Delete; `delete_file` / `delete_files` remove the original only if it lives in the upload folder |
| `GET /tracks/{track_id}/audio` | `routes/tracks.py` | Stream the original with HTTP Range support; aliases `/tracks/{id}/play`, `/songs/{id}/play`; accepts `?token=` from the endpoint below |
| `GET /tracks/{track_id}/stream-token` | `routes/tracks.py` | Mint a short-lived, track-scoped token for `<audio src>` when an API key is required (`make_stream_token()`); returns `token: null` when no key is configured |
| `GET /tags` | `routes/tracks.py` | Distinct tags with counts |
| `GET /jobs`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`, `DELETE /jobs/{job_id}` | `routes/jobs.py` | Job listing (`?status=active` = pending + running), detail, cancel, remove |
| `GET /health`, `GET /info`, `GET /stats` | `routes/system.py` | Probe (no auth), capabilities, cheap library statistics |
| `GET`, `PUT` or `PATCH /settings` | `routes/system.py` | Runtime-adjustable search defaults |
| `GET /openapi.json` | `routes/system.py` | OpenAPI 3.0 document (no auth) |

Outside the blueprint, `app.py` serves the bundled UI: `/` (`fingerprint/static/index.html`), `/docs` (`fingerprint/static/docs.html`), `/static/*`, and `/api` and `/api/v1` return a small JSON pointer to the OpenAPI document, docs and health probe.

## Key files

| Path | What it holds |
|---|---|
| `fingerprint/__init__.py` | `__version__` and the package map |
| `fingerprint/config.py` | `Settings` (defaults, profiles, env coercion, validation), `fingerprint_params()`, `fingerprint_signature()`, `FINGERPRINT_ALGORITHM_VERSION`, `describe_settings()` |
| `fingerprint/formats.py` | Native, ffmpeg-only and video extension sets; `needs_ffmpeg()`, `source_type_of()` |
| `fingerprint/core/decoder.py` | `iter_audio_chunks()`, soundfile + soxr and ffmpeg backends, `ffmpeg_info()`, `to_mono_float32()` |
| `fingerprint/core/fingerprinter.py` | `PeakExtractor` (chunked STFT, peak picking, single-pass normalisation), `Fingerprinter`, `Fingerprint` |
| `fingerprint/core/hash_generator.py` | 12/12/12-bit hash layout, `generate_hashes()`, `MAX_TIME_DELTA` |
| `fingerprint/core/matcher.py` | `Matcher`, `MatchOptions`, `MatchDiagnostics`, `Occurrence`, `TrackMatch`, `MIN_ALIGNED_FRAMES`, `SPIKE_VOTES`, `MAX_CANDIDATE_BINS`, `_cap_votes()`, `_count_distinct()`, `quality_label()` |
| `fingerprint/storage/base.py` | `StorageBackend` contract (including `flush()` and the `query_hashes()` stop-word arguments), `TrackRecord`, `initialize()` compatibility check, tag/metadata validation |
| `fingerprint/storage/sqlite_store.py` | `SQLiteStore`: WAL, per-thread connections, `WITHOUT ROWID` fingerprints, batched writes (`flush()`, `INSERT_SLICE`, `_repair_unflushed()`), temp-table lookup, `SCHEMA_VERSION` (3), 1.x rejection |
| `fingerprint/storage/memory_store.py` | `MemoryStore` with sorted index segments (`MAX_SEGMENTS`, `_compact()`) |
| `fingerprint/storage/postgres_store.py` | `PostgresStore`: psycopg 3 pool, chunked text `COPY` loading, covering index, `_redact()` |
| `fingerprint/storage/__init__.py` | `create_storage()` factory |
| `fingerprint/indexing/indexer.py` | `Indexer.index_file()` / `index_paths()`, `IndexOutcome`, `IndexSummary`, de-duplication |
| `fingerprint/indexing/scanner.py` | `iter_media_files()`, `find_media_files()`, `SKIP_DIRS`, `metadata_from_filename()` |
| `fingerprint/indexing/progress.py` | `ProgressTracker`, `ProgressSnapshot`, terminal progress bar |
| `fingerprint/jobs/manager.py` | `JobManager`, `Job`, statuses, JSON persistence, cancellation |
| `fingerprint/api/app.py` | `create_app()`, request lifecycle, access-log redaction (`_loggable_path()`), UI routes |
| `fingerprint/api/runtime.py` | `Runtime`, `SearchResult` (with `extra["diagnostics"]`), runtime settings, job runners, directory guard |
| `fingerprint/api/routes/` | `search.py`, `tracks.py` (including `_json_body()` and `stream_token()`), `jobs.py`, `system.py` on `api_bp` |
| `fingerprint/api/auth.py` | `check_request()`, `PUBLIC_PATHS`, `STREAM_PATH`, `STREAM_TOKEN_TTL`, `make_stream_token()`, `verify_stream_token()` |
| `fingerprint/api/errors.py` | `error_response()`, `register_error_handlers()` |
| `fingerprint/api/responses.py` | `format_search()`, `format_match()`, `format_track()`, `format_job()`, `format_page()` |
| `fingerprint/api/validators.py` | `require_upload()`, `parse_int()` / `parse_float()` / `parse_bool()` / `parse_choice()`, `parse_pagination()`, `clean_directory_path()`, `MAX_JSON_BODY_BYTES`, `check_json_body_size()` |
| `fingerprint/api/openapi.py` | `build_openapi()` |
| `fingerprint/api/wsgi.py` | `app = create_app()` for WSGI servers |
| `fingerprint/cli.py` | `audiofp serve`, `index`, `search`, `tracks`, `stats`, `doctor`, `config`, `db` |
| `fingerprint/utils/exceptions.py` | `AudioFPError` hierarchy with `code` and `http_status` |
| `fingerprint/utils/logging.py` | `configure_logging()`, `request_id_var`, `job_id_var`, text and JSON formatters |
| `fingerprint/utils/files.py` | `sha256_file()`, `safe_filename()`, `is_within()`, `ensure_dir()` |
| `fingerprint/static/` | Bundled web UI (`index.html`) and `/docs` page (`docs.html`) |
| `tests/test_core.py` | Chunked-vs-whole equivalence, hash reference checks, decoder and matcher behaviour |
| `tests/test_storage.py` | The backend contract, run against every backend |
