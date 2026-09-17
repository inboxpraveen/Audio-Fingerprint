# Architecture

How AudioFP 2.x is put together and how the fingerprinting and matching works. The names, constants and defaults below are the ones in the code. The other docs cover the rest: [API.md](API.md) for the endpoints, [CONFIGURATION.md](CONFIGURATION.md) for every setting, [DEPLOYMENT.md](DEPLOYMENT.md) and [PERFORMANCE.md](PERFORMANCE.md).

The package you import is `fingerprint`. The distribution and the CLI are called `audiofp`: `pyproject.toml` registers `audiofp = "fingerprint.cli:main"`, and `python -m fingerprint` runs the same `main`. Settings are fields of `fingerprint.config.Settings`, and each one can be set through the environment variable `AUDIOFP_<FIELD_UPPER>`.

## Overview

### Components

| Package | Responsibility | Main symbols |
|---|---|---|
| `fingerprint.config` | Typed settings from defaults, profile, `AUDIOFP_*` env and `.env`, plus the fingerprint signature | `Settings`, `Settings.load()`, `fingerprint_params()`, `fingerprint_signature()`, `FINGERPRINT_ALGORITHM_VERSION` |
| `fingerprint.formats` | All accepted extensions and which of them need ffmpeg | `NATIVE_AUDIO_EXTENSIONS`, `FFMPEG_AUDIO_EXTENSIONS`, `VIDEO_EXTENSIONS`, `needs_ffmpeg()` |
| `fingerprint.core.decoder` | Streaming decode of any supported file to float32 mono at the working rate | `iter_audio_chunks()`, `select_backend()`, `ffmpeg_info()`, `load_audio()` |
| `fingerprint.core.fingerprinter` | Chunked STFT and peak picking, plus the high-level facade | `PeakExtractor`, `Fingerprinter`, `Fingerprint` |
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

Search is synchronous: `POST /api/v1/search` with the file in the multipart field `audio`.

1. `require_upload()` in `fingerprint/api/validators.py` checks the extension against `formats.SUPPORTED_EXTENSIONS`. The upload goes to a `tempfile.mkstemp(prefix="audiofp-query-", suffix=<original extension>)` path, which is removed when the request ends. The suffix matters because the decoder picks its backend from the extension.
2. `Runtime.search_file()` builds `MatchOptions` from the runtime-adjustable defaults plus the form overrides `mode`, `top_k`, `min_confidence`, `min_aligned_hashes`, `min_peak_ratio` and `max_occurrences`.
3. `Fingerprinter.fingerprint_file(path, max_seconds=settings.max_query_seconds)` decodes and hashes the query. Decoding stops at `max_query_seconds` (default 3600 s). When the decoded duration lands within 0.5 s of that cap the response sets `query.truncated`.
4. `Matcher.match(query, storage, options)` makes one `storage.query_hashes(unique_hashes, max_rows_per_hash=..., stats=...)` call, caps the join at `max_search_votes`, prefilters the candidate tracks in one vectorised pass and then scores the survivors one at a time. What it did is left in `Matcher.last_diagnostics`, a `MatchDiagnostics`.
5. `format_search()` in `fingerprint/api/responses.py` renders the JSON. `last_diagnostics.to_dict()` goes in as the `diagnostics` object.

Upload and index is asynchronous: `POST /api/v1/tracks`.

1. The form fields `title`, `artist`, `tags` and `metadata` go through `validate_track_changes()` before anything is written, so a `400` never leaves an orphaned file behind. The rules are the same as for `PATCH`: title and artist are trimmed to 500 characters, tags are normalised and capped at 50, and metadata must be an object of at most 64 KB. Then the file is saved under `settings.upload_dir_resolved`, which is `<data_dir>/uploads`, as `<8 hex chars>_<safe_filename(original name)>`. `safe_filename()` keeps the extension because the decoder needs it to pick a backend.
2. `Runtime.start_upload_job()` submits a job of type `upload`. The response is `202` with the job record.
3. On the job thread, `Indexer.index_file()` de-duplicates, fingerprints and calls `storage.add_track()`. A duplicate upload is deleted from disk. A failed one is deleted too, unless `keep_failed_uploads` is true. The runner then calls `storage.flush()`, so a single upload is durable straight away even with SQLite's batched writes.
4. The client polls `GET /api/v1/jobs/{job_id}`.

Indexing a server-side folder: `POST /api/v1/tracks/index-directory` with `directory_path`, `recursive` and `tags`.

1. `clean_directory_path()` normalises the path without touching the filesystem. A missing or empty `directory_path` is a `400`. `Runtime.check_directory_allowed()` then enforces `allow_directory_indexing` and `index_roots`; with the production profile, `index_roots` must be set or the request gets `403 forbidden`. Only after that does the route check that the path is a directory, answering `400` if it isn't. So a forbidden path is never told whether it exists.
2. `find_media_files()` lists the supported files, skipping `SKIP_DIRS` and dot-folders. `start_directory_job()` submits a job of type `directory`, or answers `429` when `JobManager.active_count()` has reached `max_concurrent_jobs * 4`.
3. `Indexer.index_paths()` fingerprints the files concurrently on the shared index pool, reports progress into the `Job` and calls `storage.flush()` at the end of the run. A `StorageError` from any file stops the run and is re-raised as `StorageError("Indexing stopped after N file(s): ...")`, so the job ends as `failed` with that message.

The CLI commands `audiofp index`, `audiofp search`, `audiofp tracks` and `audiofp stats` build the same `Runtime` and call `Indexer` and `search_file()` directly. `audiofp db` and `audiofp doctor` open the store through `create_storage()` with no `Runtime` at all. There are three other entry points. `python run.py` translates `--env` to `--profile` and runs `serve`. `fingerprint.api.wsgi:app` is for gunicorn or waitress. `create_app()` is for embedding.

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

`Indexer.index_file()` wraps this for one file. A bad file never raises. The call returns an `IndexOutcome`. Its `status` is `indexed`, `duplicate` or `failed` (the constants are `OUTCOME_INDEXED`, `OUTCOME_DUPLICATE` and `OUTCOME_FAILED`). Its `error_code` is something like `file_not_found`, `unsupported_format`, `empty_fingerprint` or `out_of_memory`, or the code of whatever `AudioFPError` was raised. Only `StorageError` propagates, because if storage is gone, retrying the next file won't help either.

The steps, in order:

1. Check that the file exists and that `formats.is_supported()` accepts it.
2. De-duplicate according to `dedupe`. With `content`, hash the file with `sha256_file()` and look it up with `find_by_content_hash()`. With `path`, call `find_by_filepath()`. With `none`, skip this step.
3. `fingerprint_file()`.
4. `metadata_from_filename()`. A filename made of the artist, a spaced hyphen, then the title fills both fields. Anything else becomes the title.
5. Build the `TrackRecord`.
6. Reject a fingerprint with zero hashes as `empty_fingerprint`.
7. Under the per-indexer `_store_lock`, repeat the duplicate check right before `add_track()`. Two byte-identical files fingerprinted concurrently in one batch, on different workers, therefore end up as one track and one `duplicate` outcome.

Default parameters and what they mean at the working rate:

| Setting | Default | Derived value |
|---|---|---|
| `sample_rate` | 11025 | Nyquist 5512.5 Hz. Everything is resampled to this first |
| `n_fft` | 2048 | 1025 frequency bins, 5.38 Hz apart |
| `hop_length` | 512 | one frame = 512 / 11025 = 46.4 ms (`Runtime.info()["frame_seconds"]`) |
| `chunk_seconds` | 30.0 | decode and STFT block size. Not part of the fingerprint signature |
| `peak_neighborhood_size` | 20 | 20 frames x 20 bins local-maximum window (about 0.93 s x 108 Hz) |
| `min_amplitude` | 10.0 | linear STFT magnitude, measured after peak normalisation |
| `fan_value` | 10 | at most 10 hashes per anchor peak |
| `min_hash_time_delta` | 0 | simultaneous peaks are paired |
| `max_hash_time_delta` | 200 | frames, about 9.3 s. The hash layout caps it at 4095 frames (about 190 s) |

The pipeline ends in a `Fingerprint` dataclass. It holds `peak_times` and `peak_freqs` as int32, `hashes` as int64, `hash_times` as the int32 anchor frame of each hash, `num_samples`, `sample_rate`, `hop_length`, the `gain` that normalisation implied (`1 / peak sample`, or 1.0) and a free-form `extra` dict. Its `frames_to_seconds()` converts frames back to seconds.

### Search

```
clip --fingerprint_file(max_seconds=max_query_seconds)--> query Fingerprint
     --np.unique(query.hashes)--> storage.query_hashes(unique, max_rows_per_hash, stats)
                                 -> (hash_value, track_ref, time_offset) rows; hashes with more than
                                    max_rows_per_hash rows in the library are skipped ("stop words")
     --join on hash_value------> one vote per (DB row x query anchor with that hash):
                                 offset = time_offset minus the query anchor frame
                                 (capped at max_search_votes: the most common hashes are dropped first)
     --group by track_ref------> vectorised prefilter, then Matcher._score_track() per surviving track
                                 -> TrackMatch with a list of Occurrence
     --sort by (aligned_hashes, peak_ratio) desc, keep top_k--> get_tracks_by_ref() -> format_search()
```

The join is vectorised. The query hashes are sorted once and every returned row is mapped to its query anchors with `np.searchsorted`, so a row whose hash occurs several times in the query produces several votes. The storage call is a single batch lookup however many hashes the query has. `MatchOptions` carries the two cost guards. `max_rows_per_hash` defaults to 2000 and `0` disables it. `max_search_votes` defaults to 5 000 000. `Matcher.last_diagnostics` records the counts `query_hashes`, `db_rows`, `votes`, `candidate_tracks`, `scored_tracks`, `skipped_common_hashes` and `dropped_for_vote_cap`. The API returns them as `diagnostics`.

## Algorithm details

### Streaming decoder

`iter_audio_chunks(path, sample_rate, chunk_seconds, max_seconds=..., backend="auto", ffmpeg_binary=..., ffmpeg_timeout=3600.0, display_name=...)` yields float32 mono arrays that are already at the working sample rate, so an hour-long recording costs the same memory as a ten-second clip. `select_backend()` picks the backend by extension:

| Extensions (`fingerprint/formats.py`) | Backend | Notes |
|---|---|---|
| `NATIVE_AUDIO_EXTENSIONS`: wav, wave, flac, ogg, oga, opus, mp3, aiff, aif, aifc, au, caf, w64 | `soundfile` (libsndfile) | No external binary. MP3 needs libsndfile >= 1.1.0, bundled with soundfile >= 0.12. |
| `FFMPEG_AUDIO_EXTENSIONS`: m4a, aac, wma, amr, ac3, dts, mka, weba | `ffmpeg` | `FFmpegNotFoundError` (`ffmpeg_not_found`, HTTP 422) if ffmpeg is not on PATH. |
| `VIDEO_EXTENSIONS`: mp4, mkv, avi, mov, wmv, flv, webm, m4v, mpeg, mpg, ts, mts, 3gp, vob | `ffmpeg` | Audio track only (`-vn -sn -dn`). `source_type` becomes `video`. |
| no extension | `soundfile`, then ffmpeg | libsndfile sniffs the content, and ffmpeg is tried if that fails. |
| anything else | rejected | `UnsupportedFormatError` (`unsupported_format`, HTTP 415). |

The soundfile path is `_iter_soundfile()`. It reads `sf.SoundFile(path).blocks(blocksize=max(chunk_seconds * src_rate, 4096), dtype="float32", always_2d=True)` and averages the channels to mono. If the source rate differs from the working rate, a `soxr.ResampleStream(src_rate, sample_rate, 1, dtype="float32", quality="HQ")` resamples each block with `resample_chunk(..., last=False)` and is flushed with `last=True` at the end. Streaming resampling matches resampling the whole signal in one go to within float tolerance. `tests/test_core.py::test_soundfile_streaming_equals_whole_resample` asserts a maximum difference below 1e-4.

The ffmpeg path is `_iter_ffmpeg()`. It runs `ffmpeg -nostdin -hide_banner -loglevel error -i <path> -vn -sn -dn -ac 1 -ar <sample_rate> -f s16le -acodec pcm_s16le pipe:1`, with `-t` added when `max_seconds` is set so ffmpeg stops early and doesn't decode audio we'd throw away. It reads `chunk_seconds * sample_rate * 2` bytes at a time from stdout, at least 4096 samples' worth, and converts int16 to float32 by dividing by 32768. A daemon thread drains stderr into a 20-line ring buffer so a chatty decoder can't deadlock the pipe, and the last lines go into the `AudioDecodeError`. A `threading.Timer` kills ffmpeg after `ffmpeg_timeout` seconds. If the consumer stops early because of truncation, ffmpeg is killed too. ffmpeg does the resampling itself (`-ar`), so for the same audio its output is not sample-identical to the soundfile path. The binary name comes from the `AUDIOFP_FFMPEG_BINARY` environment variable, which `decoder.py` reads directly as `DEFAULT_FFMPEG`. It is not a `Settings` field.

There is one fallback rule. With `backend="auto"`, if libsndfile fails before the first chunk was yielded and ffmpeg is available, the file is retried through ffmpeg. libsndfile builds differ, notably in MP3 support. A failure after chunks were already yielded is raised as-is, because replaying from the start through ffmpeg would duplicate audio. `ffmpeg_info()` locates and version-checks the binary once per process (it's an `lru_cache`). `audiofp doctor` and `GET /api/v1/health` report what it found.

### Chunked STFT with center-style padding

`PeakExtractor(n_fft, hop_length, neighborhood, min_amplitude, normalize=False)` computes the magnitude spectrogram incrementally with numpy.

- The window is a periodic Hann, `scipy.signal.get_window("hann", n_fft, fftbins=True)`, in float32.
- `n_fft // 2` zeros go into the sample buffer before the first chunk, in `__init__`, and after the last one, in `finish()`, so frame `i` is centred on original sample `i * hop_length`. That is the same frame grid as a `center=True` whole-signal STFT.
- Framing is `sliding_window_view(samples, n_fft)[::hop]`, then `np.fft.rfft` along the window axis, `np.abs`, and a transpose to `(bins, frames)` float32. `_consume_buffer()` frames every complete window in the buffer and keeps only the samples the next frame still needs, so the buffer never grows beyond a chunk plus one window.
- Normalisation happens in a single pass, and `push(chunk)` takes no gain argument. `Fingerprinter.fingerprint_chunks(chunks, normalize=True, extra=None)` defaults to `normalize=True`, and `fingerprint_file(normalize=True)` goes through it, so that is the mode you get unless you ask otherwise. In that mode the extractor tracks the running peak sample of the audio seen so far and keeps every candidate peak, with its magnitude, that clears the running threshold `min_amplitude * running peak`. That set is a superset of the final one because the threshold can only rise. `finish()` then applies the exact final threshold `min_amplitude * peak`. The audio itself is never rescaled and the file is decoded once. The result is bit-identical to fingerprinting the peak-normalised signal, and `test_gain_changes_threshold_not_positions` asserts equality with an explicitly scaled copy. `PeakExtractor.gain` reports the implied gain `1 / peak`, which is stored on the `Fingerprint`. This is why `min_amplitude` means the same thing across files recorded at different levels: normalisation only moves peaks across the threshold and never moves their positions.

The chunked computation gives exactly the same peaks as a whole-signal one. Peak picking needs context on both sides of a column. `_process()` concatenates `[ctx, pending, new columns]`, runs the maximum filter over that block and emits only the columns `[n_ctx, total-margin)`, where `margin = max(neighborhood, 1)`. The last `margin` emitted columns are kept as `_ctx` for the next block. The last `margin` columns of the block stay `_pending` until more audio arrives, and `finish()` emits whatever is still pending. scipy's `maximum_filter(size=k)` looks `k // 2` columns back and `(k-1) // 2` ahead, so every emitted column sees exactly the neighbours it would see in a whole-signal spectrogram. The `mode="reflect"` boundary only ever applies at the true start and end of the signal, where a whole-signal computation reflects too. `test_chunked_peaks_identical_to_whole_signal` asserts `np.array_equal` against an independent explicit-loop implementation for chunk sizes of 10**9, 50000, 4096, 2048 and 777 samples. This is what lets `chunk_seconds` stay out of the fingerprint signature: changing it never changes a fingerprint.

### Peak picking

`_pick()` marks a spectrogram cell as a peak when two things hold. First, `maximum_filter(block, size=peak_neighborhood_size, mode="reflect") == block`, meaning the cell is the maximum of its `size x size` neighbourhood over frames and bins. Second, `block > threshold`, strictly greater, in linear magnitude. The threshold is `min_amplitude` with `normalize=False` and `min_amplitude * running peak sample` with `normalize=True`. In the normalised case `finish()` re-applies the exact final threshold `min_amplitude * peak` to the kept candidates. It then returns `(peak_times, peak_freqs, num_samples)` sorted by `np.lexsort((f, t))`, so by time and then frequency, which makes the downstream hash order deterministic. A smaller `peak_neighborhood_size` gives denser constellations, so more hashes and more storage. A higher `min_amplitude` drops quiet peaks.

### Hash layout

`generate_hashes(peak_times, peak_freqs, fan_value, min_time_delta, max_time_delta)` pairs each peak, the anchor, with later peaks, the targets. `encode_hash` packs each pair into one 36-bit value inside an int64:

```
hash = (f_anchor << 24) | (f_target << 12) | delta_t
        12 bits            12 bits           12 bits      FREQ_BITS = DELTA_BITS = 12
```

`FREQ_MASK = DELTA_MASK = 4095`, so frequency bins and frame deltas up to 4095 fit. Two checks keep it that way. `generate_hashes` clamps `max_time_delta` to `MAX_TIME_DELTA` (4095) with `min()`. `Settings.validate()` refuses `n_fft > 8190`, because the bin index `n_fft // 2` has to fit in 12 bits, and requires `0 <= min_hash_time_delta <= max_hash_time_delta <= 4095`. `decode_hash()` inverts the packing for debugging. A hash value depends only on the audio content and carries nothing about the track, which is what makes the inverted index possible. Each hash is stored with the absolute frame index of its anchor, `hash_times`. That is the value the matcher votes with.

### Fan-out

Peaks are sorted by `(time, frequency)`. For anchor `i`, the candidate targets are the next `fan_value` peaks in that order, starting at `first_target[i]`:

- With `min_hash_time_delta == 0`, the default, `first_target = i + 1`. Peaks in the same frame, such as the harmonics of one chord, get paired with `delta_t = 0`. Such pairs describe timbre more than sequence. We keep them because strong harmonics survive noise well. The matcher separately requires an alignment to span several distinct frames (`MIN_ALIGNED_FRAMES`, below), so a single shared chord can never pass as a match.
- With `min_hash_time_delta >= 1`, `first_target = searchsorted(t, t + min_time_delta)`, the first peak at least that many frames later. Use `1` for very repetitive tonal material.

Pairs with `delta_t > max_time_delta` are dropped and nothing takes their place, so `fan_value` is an upper bound per anchor. The loop over `k in range(fan_value)` is vectorised across all anchors at once, and `test_vectorised_hashes_equal_naive` checks it against a naive reference. The output arrays are sorted by anchor time, with a stable sort.

### Offset-histogram voting

For each candidate track, `_Votes` holds one vote per shared hash: `offsets`, the track frame minus the query frame, plus the query anchor frame in `qtimes` and the hash value in `qhashes`. Audio that really is the same lines up at one offset and gives a sharp spike in the histogram of offsets. Unrelated audio with coincidental hash collisions gives a flat, noisy histogram.

The histogram is smoothed first, in `_smoothed_histogram()`. `np.unique(offsets, return_counts=True)` gives the raw histogram. The smoothed count of a bin adds the raw counts of every existing bin within `+/- offset_tolerance_frames`, default 1. Clips rarely start on a frame boundary and noise shifts a peak by a frame, so the votes of a true alignment straddle neighbouring bins.

`_score_track()` then takes `best_i = argmax(smoothed)`. If `smoothed[best_i] < min_aligned_hashes` the track is dropped before any further work, because raw votes are an upper bound on the distinct-hash count computed later.

For a candidate offset, the votes within `[offset-tol, offset+tol]` are read from an offset-sorted view as a `searchsorted` slice, so no vote outside the window is touched. Then `aligned_hashes = _count_distinct(qhashes[slice])`, the number of distinct query hashes voting there. `_count_distinct` is a sort-plus-`np.diff` helper, several times faster than `np.unique` on large arrays, and `_QueryIndex.distinct_in_span()` uses it too. Counting distinct hashes neutralises sustained tones: a held note repeats one hash for many frames and would otherwise fake an alignment.

Before any per-track work, three vectorised guards bound the cost of a query.

1. The storage lookup skips query hashes that occur more than `max_rows_per_hash` times in the library. They are counted in `stats["skipped_hashes"]` and reported as `skipped_common_hashes`. Such hashes carry almost no information but would multiply the vote count.
2. If the join would still exceed `max_search_votes` votes, `_cap_votes()` groups the returned rows by hash, sorts the hashes by the votes they contribute and drops the heaviest ones until the total fits the budget. The number of rows dropped is `dropped_for_vote_cap`, and a warning naming `AUDIOFP_MAX_SEARCH_VOTES` is logged.
3. The votes are bucketed by `(track_ref, offset)` in one `np.unique` call. A track is only scored when its best raw offset bin times `2 * offset_tolerance_frames + 1` reaches `min_aligned_hashes`, since smoothing can add at most that many neighbouring bins. So a large library full of chance coincidences costs a few numpy calls and no Python loop per track.

`candidate_tracks` counts the tracks with any vote, `scored_tracks` the ones that reached `_score_track()`.

`_background()` computes the background: the mean raw vote count over the non-empty offset bins that sit away from every possible alignment, with a floor of 1.0.

- A bin is strong when `smoothed >= SPIKE_VOTES`, a module constant set to 10. The best bin counts as strong too.
- A bin is away when its distance to the nearest strong bin exceeds `2 * tol + 2`, which covers the spike and its jitter votes.
- If no bin is away, the background is 1.0.

`peak_ratio = aligned_hashes / background`. The `matcher.py` module docstring reports that chance matches sit around 3 to 9 whatever the library size, while real matches are typically well above 10. The `min_peak_ratio` default of 12 sits just above that range. Excluding every strong bin, not just the best one, matters for the occurrences use case. A pattern that appears five times in a recording produces five spikes, and a plain mean over all bins would be dominated by the matches themselves and hide them. `SPIKE_VOTES` is a constant on purpose. If it were tied to `min_aligned_hashes`, lowering the match threshold would silently change how sharpness is measured. `MIN_ALIGNED_FRAMES` (3) is the other constant guard: the aligned votes must come from at least three distinct query frames. Otherwise one instant, a chord or a click, that produced many simultaneous hash hits would get through.

Confidence is measured over the matched region only. `_robust_span(aligned_times)` gives the query frame span covered by the aligned votes: the min and max when there are at most 20 votes, otherwise the 2nd to 98th percentile, which trims stray coincidences. Then

```
confidence = min(1.0, aligned_hashes / max(distinct query hashes with an anchor inside [q_start, q_end], 1))
```

with the denominator from `_QueryIndex.distinct_in_span()`. Normalising by the matched region keeps the score meaningful for a short clip against a long track, for a short indexed pattern found inside a long recording, and for two long recordings that share one segment. In the `Occurrence`, `confidence` is rounded to 4 decimals and `peak_ratio` to 2.

### Occurrence detection

`_find_occurrences()` examines the candidate bins strongest first and returns up to `limit` occurrences. In `occurrences` mode the limit is `max_occurrences_per_track` (default 25). The API form field for it is `max_occurrences`, capped at 500. In `identify` mode the limit is exactly 1.

1. A cheap floor comes first. Only bins with `smoothed >= max(min_aligned_hashes, ceil(min_peak_ratio * background))` can pass, because smoothed raw votes bound the distinct-hash count. They are sorted by smoothed count, highest first, and cut to `MAX_CANDIDATE_BINS` (400). Very repetitive material such as hold music or a looped tone can put thousands of bins above the floor. Taking the strongest 400 keeps the cost bounded without losing real matches to the cap. `test_occurrences_on_highly_repetitive_audio_is_bounded` requires a 5-minute loop to score in under 5 s.
2. For each candidate, compute `aligned_hashes` and `peak_ratio`, and reject when `aligned_hashes` is below `min_aligned_hashes` or `peak_ratio` is below `min_peak_ratio`. Reject if fewer than `MIN_ALIGNED_FRAMES` distinct query frames voted. Then compute the span and `confidence`, and reject if that is below `min_confidence`.
3. Span-based suppression comes next. A surviving candidate is dropped when it is the same event as an occurrence already kept. That means its offset is within `2 * tol + 1` frames of a kept offset, or `_same_region()` holds. The latter is true when its query span and its track span both overlap the kept ones by more than half of the shorter span, tested by `_overlaps`. Offset jitter therefore never produces a second occurrence, while the same pattern at two different places in the recording does.
4. The strongest occurrence, by `aligned_hashes` and then `peak_ratio`, goes first and the rest follow by ascending offset. `TrackMatch.best` is `occurrences[0]`, and the track-level `aligned_hashes`, `confidence`, `peak_ratio` and `offset_frames` are copied from it. `matched_rows` is the total number of votes the track received.

Tracks are then ranked by `(aligned_hashes, peak_ratio)` descending and cut to `top_k`. Confidence plays no part in the ranking.

### Signed offsets

`Occurrence.offset_frames` is the track frame minus the query frame. Each occurrence also carries `query_start_frames` and `query_end_frames`, the span used for confidence above, and derives `track_start_frames = query_start_frames + offset_frames`, likewise for the end.

- A positive offset means the query begins `offset` frames into the track. The classic case: a 10 s clip identified at 1:23 of a song.
- A negative offset means the indexed track starts after the query does, so the track's content is found inside the query at `-offset`. This is how pattern search works. Index the short pattern, say a jingle, a compliance disclaimer or hold music, as a track. Then search with the long call recording in `occurrences` mode, and each occurrence says where in the recording the pattern occurs. `test_occurrences_mode_finds_pattern_twice_and_negative_offsets` covers this.

`format_match()` and `_occurrence()` in `fingerprint/api/responses.py` expose both readings. `offset_sec` is signed. `track_offset_sec = max(0, offset_sec)` and `query_offset_sec = max(0, -offset_sec)`. `query_start_sec`, `query_end_sec`, `track_start_sec` and `track_end_sec` give the spans, with the track values clamped at 0. `match_offset_sec` is a 1.x alias of `track_offset_sec`. Span ends are anchor-based, so they run slightly short of the true end of a pattern, because anchors near the end pair with peaks outside it.

### Thresholds and quality labels

`MatchOptions.from_settings(settings, **overrides)` clamps every threshold: `top_k` to `[1, max_top_k]`, `min_aligned_hashes >= 1`, `min_confidence` to `[0, 1]`, `min_peak_ratio >= 0`, `offset_tolerance_frames >= 0` and `max_occurrences_per_track >= 1`. An unknown `mode` raises `MatchingError`, which comes out as a 400 `validation_error`. It also copies the two cost guards `max_rows_per_hash` and `max_search_votes` from `Settings`, where they are validated as `>= 0` and `>= 10000`. They are not request fields. `Runtime.match_options()` puts the runtime-adjustable defaults under the per-request overrides. Those defaults are `RUNTIME_SETTING_KEYS = top_k, min_confidence, min_aligned_hashes, min_peak_ratio, mode`, persisted in `<data_dir>/runtime-settings.json` and edited through `PUT` or `PATCH /api/v1/settings`. The search response echoes the effective values in `thresholds`.

`quality_label(confidence, peak_ratio)` buckets a result for the UI and CLI: `strong` when `confidence >= 0.15 and peak_ratio >= 30`, `likely` when `confidence >= 0.05 and peak_ratio >= 18`, otherwise `weak`.

## Storage model

### Contract

`fingerprint/storage/base.py` defines `StorageBackend`, the abstract class every backend implements. `tests/test_storage.py` runs the same contract tests against each backend: memory and SQLite always, PostgreSQL when `AUDIOFP_TEST_POSTGRES_DSN` is set. A backend stores tracks and the inverted index from hash to (track, frame):

| Method | Purpose |
|---|---|
| `initialize(signature, params, compat)` | Fingerprint-parameter compatibility check (below) |
| `get_meta(key)` / `set_meta(key, value)` | Small key-value table |
| `add_track(record, hashes, times)` | Persist a track and its fingerprints. Adding an existing `track_id` replaces it atomically |
| `get_track`, `get_tracks_by_ref`, `find_by_content_hash`, `find_by_filepath`, `list_tracks`, `update_track`, `delete_track`, `delete_tracks`, `count_tracks` | Track metadata |
| `query_hashes(hash_values, max_rows_per_hash=None, stats=None)` | Batch lookup returning three aligned int64 arrays `(hash_value, track_ref, time_offset)`. Hashes with more than `max_rows_per_hash` rows are skipped and counted in `stats["skipped_hashes"]` |
| `flush()` | Write any buffered fingerprints and return the number of rows written. The base class version is a no-op that returns 0, and `SQLiteStore` overrides it. Called by `Indexer.index_paths()` at the end of every run, by the upload job runner, and by `SQLiteStore` itself on `close()` |
| `get_stats()` | Cheap statistics that never scan the fingerprint table |
| `clear()` | Delete every track and fingerprint, keep meta |
| `close()`, `health_check()` | Lifecycle |

`TrackRecord` carries `track_id`, `ref`, `title`, `artist`, `filename`, `filepath`, `content_hash`, `duration`, `num_peaks`, `num_hashes`, `source_type`, `file_size`, `indexed_at`, `tags` and `metadata`. `track_id` is a UUID4 string and the public identifier. `ref` is a small integer the backend assigns and never exposes: `to_dict()` drops it. `source_type` is `audio` or `video`. Fingerprint rows store the integer `ref`, which keeps the by far largest table compact. Clients may edit only `title`, `artist`, `tags` and `metadata`, checked by `validate_track_changes()`. `normalize_tags()` lower-cases, strips, de-duplicates and keeps at most 50 tags. Metadata must serialise to at most 64 KB of JSON. `SORTABLE_FIELDS` limits `list_tracks(sort=...)` to `indexed_at`, `title`, `artist`, `duration`, `filename` and `num_hashes`.

`create_storage(settings, check_compat=True)` picks the backend from `storage_type`, one of `memory`, `sqlite` or `postgres`, and runs `initialize()`. If that fails it closes the store and re-raises.

### Fingerprint signature and compatibility check

Hash values encode frequency bins and frame deltas, so a database built with one `sample_rate`, `n_fft`, `hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`, `min_hash_time_delta` or `max_hash_time_delta` is silently useless with another. `Settings.fingerprint_params()` collects exactly those fields, the ones whose metadata has `fingerprint=True`, plus `algorithm_version = FINGERPRINT_ALGORITHM_VERSION`. That version is currently 2 and gets bumped when a code change alters the hash layout, the STFT framing or the peak rules. `Settings.fingerprint_signature()` is the first 16 hex characters of the SHA-256 of that dict serialised as compact, key-sorted JSON.

`StorageBackend.initialize()` compares the signature with the meta keys `META_SIGNATURE = "fingerprint_signature"` and `META_PARAMS = "fingerprint_params"`:

- If there is no stored signature and zero tracks, it stamps `fingerprint_signature`, `fingerprint_params` and `created_at`.
- If there is no stored signature but tracks exist, it returns without stamping or raising.
- An equal signature, or `fingerprint_compat = ignore`, means carry on.
- A different signature depends on `fingerprint_compat`. `warn` logs and carries on. `strict`, the default, raises `FingerprintCompatibilityError` (code `fingerprint_incompatible`, HTTP 503), which names both signatures and the stored parameters.

The way out is to restore the original `AUDIOFP_*` fingerprint settings, or point `AUDIOFP_SQLITE_PATH` or `AUDIOFP_POSTGRES_DSN` at a new database, or run `audiofp db reset --yes`, which clears the store and re-stamps it with the current parameters. `audiofp db check` opens the store with the check enabled and reports the stored signature. `GET /api/v1/health` and `/api/v1/info` show the running signature.

### SQLite (`SQLiteStore`, the default)

`sqlite_path` defaults to `<data_dir>/fingerprints.db`. Everything below lives in `fingerprint/storage/sqlite_store.py`.

- There is one `sqlite3` connection per thread, kept in a `threading.local` and opened with `isolation_level=None`, so transactions are explicit `BEGIN` and `COMMIT`. A process-wide `_write_lock`, an `RLock`, makes concurrent indexing workers queue for writes so they don't fight over `SQLITE_BUSY`. Every connection is recorded with its owning thread. Connections of threads that have exited are closed lazily by `_prune_dead_connections()`, which runs when a connection is opened and from the `open_connections` property, so short-lived worker threads don't leak file handles. A `db_path` of `:memory:` is refused with a `StorageError`, since such a database is per connection and couldn't be shared between threads. Use `storage_type=memory` for that. Per-connection pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `cache_size=-<sqlite_cache_mb * 1024>` (KiB), `temp_store=MEMORY`, `mmap_size=<sqlite_mmap_mb> MB`, `busy_timeout=30000`, `foreign_keys=ON`. The constructor also takes `write_batch_rows` and `track_index`. They come from the settings `sqlite_write_batch_rows`, default 2 000 000, and `sqlite_track_index`, off by default.
- The schema is `_SCHEMA_V2` plus the v3 step `_SCHEMA_V3 = ("ALTER TABLE tracks ADD COLUMN flushed INTEGER NOT NULL DEFAULT 1",)`:

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

  `fingerprints` is a `WITHOUT ROWID` table. Its primary key `(hash_value, track_ref, time_offset)` is the clustered B-tree, so the table is both the storage and the lookup index and there is no redundant secondary index. The 1.x layout was a rowid table with a TEXT `song_id` plus a covering index over the same three columns, which stored every fingerprint twice. Rows are always inserted in key order. `_insert_rows()` sorts them with `np.lexsort((times, refs, hashes))` and converts them to Python tuples in slices of `INSERT_SLICE` rows (200 000) for `INSERT OR IGNORE`. That way the B-tree fills sequentially and the transient memory stays bounded. The trade-off is that by default there is no index on `track_ref` and no foreign key, so `DELETE FROM fingerprints WHERE track_ref=?` scans the clustered index. That is the replace-on-re-add path in `add_track()` and the start-up repair. `delete_tracks()` deletes the fingerprints of a whole batch with `WHERE track_ref IN (...)`, 500 refs per statement, so a bulk delete costs one scan per 500 tracks. With `sqlite_track_index=true` the store creates `idx_fp_track ON fingerprints (track_ref)` at start-up, which makes deletes indexed and roughly doubles the disk. `tags` and `metadata` are JSON text, and `list_tracks(tag=...)` matches with an escaped `LIKE`.
- Writes are batched. Inserting one track's hashes touches a random leaf page per hash, so on a large library every track would rewrite hundreds of MB of B-tree. `add_track()` therefore inserts the `tracks` row straight away with `flushed=0` and appends the `(ref, hashes, times)` arrays to an in-memory buffer, `_pending`, at about 16 bytes per row. `flush()` writes everything buffered in one sorted transaction: `_insert_rows()` followed by `UPDATE tracks SET flushed=1`. It runs when the buffer reaches `write_batch_rows`, at the end of every indexing run in `Indexer.index_paths()`, after every upload job, on `close()`, and before `vacuum()`, `checkpoint()` and `unique_hash_count()`. Buffered tracks are searchable, because `query_hashes()` also scans the pending buffer through `_pending_matches()`. If the flush transaction fails the rows stay buffered for a retry and a `StorageError` is raised. At start-up `_repair_unflushed()` deletes every track still marked `flushed=0`, which means the process died before a flush, and logs a warning listing the first names. Those files just get re-indexed on the next run, and the library never contains a track that can't be matched. `write_batch_rows=0` restores immediate per-track writes. `get_stats()` reports the buffer size as `pending_rows`.
- Lookups are batched too. `query_hashes()` creates `TEMP TABLE query_hashes (hash_value INTEGER PRIMARY KEY)` and fills it inside one transaction, because in autocommit mode each row would be its own commit. When `max_rows_per_hash` is set it first runs a `GROUP BY q.hash_value HAVING COUNT(*) > ?` pre-query over the join and deletes the heavy hashes from the temp table. Their number goes to `stats["skipped_hashes"]`. It then runs `SELECT f.hash_value, f.track_ref, f.time_offset FROM query_hashes AS q CROSS JOIN fingerprints AS f ON f.hash_value = q.hash_value` and appends the matching rows from the pending buffer. That is one statement whatever the query size, with no chunking around the SQLite bound-variable limit.
- `PRAGMA user_version` holds `SCHEMA_VERSION = 3`. `_init_schema()` treats a database with `user_version == 0` that has a `songs` table and no `meta` table as an AudioFP 1.x file and rejects it with `FingerprintCompatibilityError`. The hash layout changed and can't be upgraded in place, so point `AUDIOFP_SQLITE_PATH` at a new file and re-index. A `user_version` greater than 3 raises `StorageError` asking you to upgrade AudioFP. Anything lower runs `_migrate()`, which applies the missing steps in one `BEGIN IMMEDIATE` transaction and sets the version: `_SCHEMA_V2` below version 2, `_SCHEMA_V3` below version 3. A v2 file from an earlier 2.0 build is upgraded in place automatically and its existing rows get `flushed=1`.
- `get_stats()` reads `COUNT(*)`, `SUM(num_hashes)` and `SUM(duration)` from `tracks`, plus the on-disk size of the `.db` and `-wal` files and the buffer's `pending_rows`, so the UI can poll `GET /api/v1/stats` as often as it likes. `unique_hash_count()` is a full index scan and exists only for `audiofp stats --full`. `vacuum()` and `checkpoint()` (`PRAGMA wal_checkpoint(TRUNCATE)`) back `audiofp db vacuum`. All three flush the buffer first.

### Memory (`MemoryStore`)

For tests, demos and the `testing` profile. Nothing survives a restart. The inverted index is a list of sorted segments, one `(hashes sorted, refs, times)` int64 triple per added track, so adding a track never re-sorts the whole library. Once there are more than `MAX_SEGMENTS` segments (64), `_compact()` merges them into one. Deleted tracks are only masked until the next compaction: their refs go into `_deleted_refs` and are filtered out of every lookup. Compaction also runs once more than `MAX_SEGMENTS` refs are pending deletion. `query_hashes()` is a vectorised `searchsorted` per segment. It applies `max_rows_per_hash` with `np.unique(..., return_counts=True)` and fills `stats["skipped_hashes"]`. Readers and writers share one `RLock`, and records are deep-copied on the way in and out.

### PostgreSQL (`PostgresStore`)

For multi-process or multi-node deployments. It needs the optional extra, `pip install "audiofp[postgres]"`, which installs `psycopg[binary,pool]` (psycopg 3), and a `postgres_dsn`. Connections come from a `psycopg_pool.ConnectionPool(min_size=1, max_size=postgres_pool_size)`. The store waits for the pool at start, with a `connect_timeout` of 15 s, so a bad DSN fails fast with a `StorageError` whose message has the password redacted by `_redact()`. Without that wait you'd get a pool timeout deep inside the first query.

The schema mirrors SQLite with PostgreSQL types: `ref SERIAL`, `tags` and `metadata` as `JSONB`, `file_size BIGINT`. The differences: `fingerprints` is a plain heap table with `track_ref INTEGER REFERENCES tracks(ref) ON DELETE CASCADE`, a covering index `idx_fp_hash ON fingerprints (hash_value) INCLUDE (track_ref, time_offset)` serves lookups straight from the index, and `idx_fp_track ON fingerprints (track_ref)` keeps deletes cheap. Fingerprints are bulk-loaded with `COPY fingerprints (hash_value, track_ref, time_offset) FROM STDIN` in text format, written as chunks of 200 000 tab-separated rows, with no per-row `write_row()` calls. `query_hashes()` is one `SELECT ... WHERE hash_value = ANY(%s)` round trip, preceded by a `GROUP BY hash_value HAVING COUNT(*) > %s` query that removes the hashes above `max_rows_per_hash` and counts them in `stats["skipped_hashes"]`. The schema version is recorded as the meta key `schema_version`, value 2, with `ON CONFLICT (key) DO NOTHING`. There is no migration logic for PostgreSQL. `list_tracks(query=...)` escapes `%`, `_` and `\` and uses `ILIKE ... ESCAPE '\'` like SQLite. `list_tracks(tag=...)` uses the JSONB `?` operator. `clear()` is `TRUNCATE fingerprints, tracks RESTART IDENTITY`.

## Background jobs

`fingerprint/jobs/manager.py` knows nothing about storage and has no dependencies, so it can host other job types later. `Runtime` creates one `JobManager(max_concurrent_jobs, history_limit=job_history_limit, persist_dir=jobs_dir if persist_jobs else None, max_errors=job_max_errors)`.

- A `ThreadPoolExecutor(max_workers=max_concurrent_jobs, thread_name_prefix="audiofp-job")` runs `runner(job)`. `submit(job_type, label, runner, total=..., meta=...)` returns the `Job` immediately. A job starts as `pending`, moves to `running` and ends as `completed`, `failed` or `cancelled`. Any exception means `failed`, with the message in `job.error`. `TERMINAL` is the set of finished states. While a job runs, `job_id_var` is set so every log line carries the job id.
- Runners report progress with `job.update(...)`, which is thread-safe and keeps the counters `total`, `completed`, `succeeded`, `failed`, `skipped` and `current_item`. Errors go through `job.add_error(item, message, code, limit)`, which keeps at most `job_max_errors` entries (default 500). `Job.to_dict()` derives `percent`, `elapsed_sec`, `eta_sec` and `rate_per_sec`. `GET /api/v1/jobs` omits the error list unless `include_errors=true`.
- With `persist_jobs` on, the default, every job is written to `<data_dir>/jobs/<job_id>.json` atomically: temp file, then `os.replace`. State changes persist immediately. Progress updates through `JobManager.touch()` are throttled to one write per 2 s per job. `_trim_history()` keeps at most `job_history_limit` finished jobs, 200 by default, in memory and on disk.
- On start-up `_load_history()` reads the JSON files. Any job still recorded as `pending` or `running` is marked `interrupted` with the error "The server restarted before this job finished. Files already indexed were kept; re-run to index the rest." That is the only place `interrupted` is assigned.
- `cancel(job_id)`, behind `POST /api/v1/jobs/{job_id}/cancel`, sets the job's cancel event and `cancel_requested`. A job that is still `pending` is cancelled immediately when `Future.cancel()` succeeds. A running job is cancelled cooperatively. `Indexer.index_paths()` polls `job.should_cancel()`, stops submitting new files and lets the files already in flight finish, and then the summary is marked `cancelled`. A runner can also call `job.check_cancelled()`, which raises `JobCancelled`. Cancelling a terminal job is a `job_error` (400). `remove()`, behind `DELETE /api/v1/jobs/{job_id}`, is allowed only for terminal jobs and deletes the file. `shutdown()`, called from `Runtime.close()` and registered with `atexit`, sets every active job's cancel event and turns still-pending jobs into `cancelled`.
- Fingerprinting threads are separate from job threads. `Runtime` holds one `Indexer` whose `executor` is a lazily created `ThreadPoolExecutor(max_workers=settings.effective_index_workers, thread_name_prefix="audiofp-index")`. `index_workers = 0`, the default, means `min(4, CPU count)`. A `directory` job calls `Indexer.index_paths()`, which submits files onto that shared pool in a sliding window of `2 x workers`, so cancellation is prompt and memory stays bounded on 100 000-file runs. Several directory jobs therefore share the same fingerprinting threads. An `upload` job calls `Indexer.index_file()` directly on its job thread, so it takes a job slot but no index-pool slot. A `StorageError` from a worker stops the whole run, since failing every remaining file one at a time would be pointless. `index_paths()` stops submitting, waits for the files in flight, flushes what it can and then raises `StorageError("Indexing stopped after N file(s): <cause>")` with `indexed`, `failed` and `duplicates` counts in `details`. The job ends as `failed` with that message, and the files indexed before the failure are kept. Every run, whatever its outcome, ends with `storage.flush()`.

## API layer

### Application factory and `Runtime`

`create_app(settings=None, *, profile=None, storage=None, configure_logs=True)` in `fingerprint/api/app.py` loads `Settings` from env and `.env` unless one is passed in, then sets up logging, the Flask app and the `Runtime`, in that order. Logging means the `fingerprint` logger hierarchy: console output plus the rotating file named by `Settings.log_file_resolved`. `log_file=auto`, the default, means `<data_dir>/logs/audiofp.log` in the production profile and console-only elsewhere. `none` or an empty value disables the file, and an unwritable log directory logs a warning and carries on console-only. The app is `Flask("audiofp")` with `MAX_CONTENT_LENGTH = max_upload_mb` MB, optional CORS from `cors_origins` (which exposes `X-Request-ID`) and optional `ProxyFix` from `trust_proxy`. The single `Runtime` goes into `app.extensions["audiofp"]`, where route modules fetch it with `routes.runtime()`.

`Runtime.close()` is idempotent and runs on shutdown. `audiofp serve` calls it in a `finally` block after the server loop returns, and it is also registered with `atexit` as a fallback. It cancels running jobs, which stop at the next file boundary and persist a `cancelled` record, waits for them, closes the fingerprint pool and closes the storage, which flushes any buffered SQLite rows. `audiofp serve` also installs a `SIGTERM` handler, `_install_shutdown_signals()` in `fingerprint/cli.py`, that raises `SystemExit`. Without it Python's default disposition would kill the process without running either hook. waitress and the Flask development server both unwind cleanly on `SystemExit`, so `systemctl stop` and `docker stop` are clean shutdowns.

`Runtime` in `fingerprint/api/runtime.py` owns the long-lived objects: `settings`, `storage` from `create_storage()`, `fingerprinter`, `matcher`, `indexer` and `jobs`, plus the runtime-adjustable search defaults. Route handlers stay thin: parse, call a `Runtime` method, format. The CLI reuses the same object without Flask. `Runtime.health()` backs `GET /api/v1/health`, which answers `ok`, or `degraded` with HTTP 503 when `storage.health_check()` fails. `Runtime.info()` backs `GET /api/v1/info` with the formats, limits, feature flags, fingerprint parameters and signature, runtime defaults and `frame_seconds`.

### Request lifecycle, request ids and logging

`before_request` assigns `g.request_id` from the incoming `X-Request-ID` header, accepted only when it is 1 to 64 characters of `[A-Za-z0-9._:-]`, or from a fresh 12-hex-character id. It stores the id in the `request_id_var` context variable and, for any path under `/api/`, runs `check_request()`. `after_request` echoes `X-Request-ID`, sets `X-Content-Type-Options: nosniff` and sets `Cache-Control: no-store` for API responses. When `access_log` is on it also logs one line per API request with the path, query string, status and duration. The values of the `token` and `api_key` query parameters are replaced by `***` in that line; see `_loggable_path()` and `_SECRET_QUERY_KEYS`. `ContextFilter` in `fingerprint/utils/logging.py` injects `request_id` and `job_id` into every log record. The text format shows the request id in brackets and the JSON format emits both as keys, so an error's `request_id` can be found in the logs.

### Error envelope

`register_error_handlers()` in `fingerprint/api/errors.py` renders every error, whether an `AudioFPError`, a Werkzeug `HTTPException` or an unexpected exception, through `error_response()`:

```json
{"error": "human-readable message", "code": "machine_code", "status": 415, "details": {"extension": ".xyz"}, "request_id": "3f9c1a0b2d4e"}
```

`details` and `request_id` are present only when available. `error` stays a plain string for 1.x clients. The `code` and HTTP status come from the exception hierarchy in `fingerprint/utils/exceptions.py`: `validation_error` 400, `unauthorized` 401, `forbidden` 403, `not_found` 404, `conflict` 409, `payload_too_large` 413, `unsupported_format` 415, `audio_processing_error` / `audio_decode_error` / `ffmpeg_not_found` 422, `storage_error` / `fingerprint_incompatible` 503, `matching_error` / `configuration_error` / `internal_error` 500, `job_error` 400. Flask's own 413 is rewritten to mention `AUDIOFP_MAX_UPLOAD_MB`. The JSON routes raise their own `413 payload_too_large`, with the message `JSON body too large (max 1024 KB)`, for bodies over `MAX_JSON_BODY_BYTES` (1 MiB), and `411 length_required` for a chunked body, meaning `Transfer-Encoding` set and no `Content-Length`. That check is `check_json_body_size()` in `fingerprint/api/validators.py`, called by `_json_body()` in `routes/tracks.py` and by `update_settings()` in `routes/system.py`. Unexpected exceptions become a 500 `internal_error` whose message points at the request id.

### Authentication

Auth lives in `fingerprint/api/auth.py`. When `api_key` is set, every request under `/api/` must present the key as `X-API-Key: <key>` or `Authorization: Bearer <key>`. The exceptions are `PUBLIC_PATHS`, which are `/api/v1/health` and `/api/v1/openapi.json`, and `OPTIONS` preflights. `GET` requests to the audio stream paths in `STREAM_PATH` also accept `?token=`, a short-lived, track-scoped HMAC token minted by `GET /tracks/<id>/stream-token` (`make_stream_token` and `verify_stream_token`, `STREAM_TOKEN_TTL` = 3600 s). That exists because `<audio>` elements can't send headers. The key itself is never accepted in a URL. Comparison uses `hmac.compare_digest`. A missing or wrong key is a 401 `unauthorized`. There is one shared secret. Per-user keys, SSO or rate limiting belong in a reverse proxy.

### Endpoints

All of these sit under `/api/v1` on `api_bp`. The full request and response shapes are in [API.md](API.md) and in the hand-maintained OpenAPI document, built by `build_openapi()` in `fingerprint/api/openapi.py` and served at `/api/v1/openapi.json`. `tests/test_api.py::test_openapi_covers_every_route` keeps it in sync with the registered routes.

| Method and path | Module | Purpose |
|---|---|---|
| `POST /search` | `routes/search.py` | Identify a clip or find every occurrence of a pattern |
| `POST /tracks` | `routes/tracks.py` | Upload and index in the background (202 + job). Alias `POST /upload` |
| `POST /tracks/index-directory` | `routes/tracks.py` | Index a server-side folder (202 + job). Alias `POST /index` |
| `GET /tracks`, `GET /tracks/{track_id}` | `routes/tracks.py` | List (search, sort, paginate) and fetch. Aliases under `/songs` |
| `PATCH` or `PUT /tracks/{track_id}` | `routes/tracks.py` | Edit `title`, `artist`, `tags`, `metadata` |
| `DELETE /tracks/{track_id}`, `POST /tracks/bulk-delete` | `routes/tracks.py` | Delete. `delete_file` / `delete_files` remove the original only if it lives in the upload folder |
| `GET /tracks/{track_id}/audio` | `routes/tracks.py` | Stream the original with HTTP Range support. Aliases `/tracks/{id}/play` and `/songs/{id}/play`. Accepts `?token=` from the endpoint below |
| `GET /tracks/{track_id}/stream-token` | `routes/tracks.py` | Mint a short-lived, track-scoped token for `<audio src>` when an API key is required (`make_stream_token()`). Returns `token: null` when no key is configured |
| `GET /tags` | `routes/tracks.py` | Distinct tags with counts |
| `GET /jobs`, `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`, `DELETE /jobs/{job_id}` | `routes/jobs.py` | Job listing (`?status=active` = pending + running), detail, cancel, remove |
| `GET /health`, `GET /info`, `GET /stats` | `routes/system.py` | Probe (no auth), capabilities, cheap library statistics |
| `GET`, `PUT` or `PATCH /settings` | `routes/system.py` | Runtime-adjustable search defaults |
| `GET /openapi.json` | `routes/system.py` | OpenAPI 3.0 document (no auth) |

Outside the blueprint, `app.py` serves the bundled UI: `/` is `fingerprint/static/index.html`, `/docs` is `fingerprint/static/docs.html`, plus `/static/*`. `/api` and `/api/v1` return a small JSON pointer to the OpenAPI document, the docs and the health probe.

## Key files

| Path | What it holds |
|---|---|
| `fingerprint/__init__.py` | `__version__` and the package map |
| `fingerprint/config.py` | `Settings` (defaults, profiles, env coercion, validation), `fingerprint_params()`, `fingerprint_signature()`, `FINGERPRINT_ALGORITHM_VERSION`, `describe_settings()` |
| `fingerprint/formats.py` | Native, ffmpeg-only and video extension sets, plus `needs_ffmpeg()` and `source_type_of()` |
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
