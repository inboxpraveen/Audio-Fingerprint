# Tuning matching and fingerprinting

AudioFP has two layers of knobs. They behave very differently, so keep them apart:

| Layer | Settings | When they can change | What a change does |
|---|---|---|---|
| Matching thresholds | `min_aligned_hashes`, `min_confidence`, `min_peak_ratio` (plus `top_k`, `offset_tolerance_frames`, `max_occurrences_per_track`, and the cost guards `max_rows_per_hash`, `max_search_votes`) | Any time: per request, at runtime through the API/UI, or via environment | Changes which candidates are reported. Nothing stored is affected. |
| Fingerprint parameters | `sample_rate`, `n_fft`, `hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`, `min_hash_time_delta`, `max_hash_time_delta` | Before indexing only | Changes the fingerprints themselves. The database must be rebuilt (`audiofp db reset`, re-index). |

Start with the thresholds. They are free to try and solve most problems (false positives, missed noisy clips). Touch the fingerprint parameters only when the [calibration](#calibration-recipe) shows that no threshold can separate good matches from bad ones.

Every setting is a field of `Settings` in `fingerprint/config.py`; its environment variable is `AUDIOFP_<FIELD_UPPER>` (a `.env` file in the working directory is read too). `audiofp config --describe` prints the full reference table and marks the fingerprint parameters.

## How a match is scored

1. **Decode.** The audio is downmixed to mono and resampled to `sample_rate`. WAV, FLAC, OGG, Opus and MP3 are decoded by libsndfile (with `soxr` resampling); M4A/AAC/WMA and video containers go through ffmpeg, which is optional and only needed for those (and as a fallback when libsndfile rejects a file before producing any audio, e.g. an MP3 on a build without MP3 support). The result is peak-normalised, for the library and for queries alike: every threshold below is applied as if the audio had been scaled so its loudest sample is 1.0 (`PeakExtractor` tracks the peak sample in the same pass and thresholds against `min_amplitude * peak`; the audio itself is never rescaled and is decoded only once, with a result identical to fingerprinting the scaled signal).
2. **Peaks.** An STFT (Hann window of `n_fft` samples, step `hop_length`) gives a magnitude spectrogram. A cell is a peak when it is the local maximum of a `peak_neighborhood_size` x `peak_neighborhood_size` window (frames x frequency bins) and its magnitude exceeds `min_amplitude` (times the file's peak sample, see above).
3. **Hashes.** Each peak (the *anchor*) is paired with the next `fan_value` peaks that lie at least `min_hash_time_delta` frames later; pairs further apart than `max_hash_time_delta` are dropped (not replaced by later peaks), so a peak yields *up to* `fan_value` hashes. A pair becomes one 36-bit hash, `(f_anchor << 24) | (f_target << 12) | delta_t` (12 bits each), stored together with the anchor's frame index.
4. **Lookup.** Every distinct query hash is looked up in the inverted index, except hashes that occur more than `max_rows_per_hash` times in the library (default 2000): those are "stop words" - hold-music loops, test tones, sustained notes - that would multiply the vote count while saying nothing about *which* track matches, so they are skipped and counted as `diagnostics.skipped_common_hashes`. Each remaining hit is one vote for the offset `track_frame - query_frame` on that track. If the total would exceed `max_search_votes` (default 5,000,000), the most common hashes are dropped first until it fits (`diagnostics.dropped_for_vote_cap`, plus a warning in the server log).
5. **Scoring.** Per track, the votes are binned by offset (bins within +/- `offset_tolerance_frames` are merged) and every spike in that histogram is scored with the three numbers below. Genuine audio lines up at one offset and produces a sharp spike; unrelated audio produces a flat, noisy histogram. `identify` keeps the best spike per track, `occurrences` keeps every spike that passes the thresholds.

Everything is measured in frames. At the defaults (`hop_length=512`, `sample_rate=11025`) one frame is 512/11025 s, about 46.4 ms. That is the resolution of every reported offset and the unit of every `*_frames` setting.

### The three scores

All three are defined in `fingerprint/core/matcher.py` and returned for every match and every occurrence.

**`aligned_hashes`** - the number of *distinct* query hash values that vote for the best offset (votes within +/- `offset_tolerance_frames` count). Distinct, not raw votes: a sustained tone repeats one hash for many frames and would otherwise fake an alignment. Two extra rules apply: the aligned hashes must come from at least `MIN_ALIGNED_FRAMES = 3` distinct query frames (a hard-coded constant, not a setting: one shared chord or click can never be a match), and a track is rejected early when the merged raw vote count of its best bin is below `min_aligned_hashes`.

**`confidence`** - `aligned_hashes / (distinct query hashes whose anchor lies inside the matched span)`, clamped to [0, 1] and rounded to 4 decimals. The denominator is the *matched region* of the query, not the whole query, so the score means the same thing for a 5 s clip against a 3 min song, for a 3 s jingle indexed and found inside an hour-long call, and for two long recordings that share a segment. Read it as "how much of the overlapping audio does the alignment explain".

**`peak_ratio`** - `aligned_hashes / background`, rounded to 2 decimals. *Background* is the mean raw vote count over the non-empty offset bins that lie more than `2 * offset_tolerance_frames + 2` frames away from any potential spike (a bin with at least `SPIKE_VOTES = 10` merged votes, or the best bin), floored at 1.0. It is the sharpness of the spike relative to the floor of coincidental collisions. The matcher's own comment gives the expected ranges - chance matches sit around 3-9 regardless of library size, real matches are typically well above 10 - but measure yours (see [calibration](#calibration-recipe)).

Consequences worth knowing:

- Background is computed per track from that track's own votes, so `peak_ratio` does not drop as the library grows. How it moves with *track length* is not verified (the code averages over *non-empty* bins, so a longer track also spreads its coincidental votes over more offsets). Calibrate with realistic track lengths.
- When a track's histogram is clean (background at the floor of 1.0), `peak_ratio == aligned_hashes`. With the defaults that makes `min_peak_ratio = 12` the binding minimum on aligned hashes, not `min_aligned_hashes = 10`.
- `aligned_hashes` and `peak_ratio` grow with clip length; `confidence` largely does not. A longer clip is the cheapest way to lift a borderline match.
- Results are ranked by `(aligned_hashes, peak_ratio)` descending and then cut to `top_k`. `confidence` plays no part in the ranking.

### Quality labels

`quality_label(confidence, peak_ratio)` in `matcher.py` turns the two normalised scores into the `quality` field shown by the UI and the CLI:

| Label | Condition |
|---|---|
| `strong` | `confidence >= 0.15` and `peak_ratio >= 30` |
| `likely` | `confidence >= 0.05` and `peak_ratio >= 18` |
| `weak` | anything else that passed the thresholds |

The label is display only. Thresholds decide what is returned; `quality` never filters. A client that wants only strong results filters on the field itself, or raises the thresholds to the same values.

### Where the numbers appear

`POST /api/v1/search` and `audiofp search --json` return the same JSON (built by `format_search` in `fingerprint/api/responses.py`):

- `matches[]`: `aligned_hashes`, `confidence`, `peak_ratio`, `quality`, `matched_rows` (all votes the track received, before binning), `offset_sec`, `track_offset_sec`, `query_offset_sec`, `query_start_sec`, `query_end_sec`, `track_start_sec`, `track_end_sec`, `occurrences[]`. The top-level scores and offsets mirror the best occurrence.
- `occurrences[]`: the same score and span fields per occurrence.
- `query`: `duration_sec`, `num_peaks`, `num_hashes`, `truncated`.
- `thresholds`: the `min_confidence`, `min_aligned_hashes`, `min_peak_ratio` and `top_k` that were actually applied after merging request, runtime and environment values. Look here first when a threshold "does not seem to work".
- `diagnostics` (`Matcher.last_diagnostics`, always present): `query_hashes`, `db_rows` (index rows returned), `votes` (after the join), `candidate_tracks` (tracks with any vote), `scored_tracks` (tracks that passed the cheap prefilter and were scored), `skipped_common_hashes` (stop words skipped because of `max_rows_per_hash`) and `dropped_for_vote_cap` (rows dropped because of `max_search_votes`). Look here second: a miss with a large `skipped_common_hashes` means the pattern itself is repetitive enough to be treated as noise, and `scored_tracks: 0` with many `candidate_tracks` means nothing came near `min_aligned_hashes`.

## The three thresholds

| Setting | Environment variable | Default | Rejects a spike when | Request field | CLI flag | Runtime key |
|---|---|---|---|---|---|---|
| `min_aligned_hashes` | `AUDIOFP_MIN_ALIGNED_HASHES` | `10` | `aligned_hashes` is below it | `min_aligned_hashes` | `--min-aligned` | `min_aligned_hashes` |
| `min_confidence` | `AUDIOFP_MIN_CONFIDENCE` | `0.02` | `confidence` is below it | `min_confidence` | `--min-confidence` | `min_confidence` |
| `min_peak_ratio` | `AUDIOFP_MIN_PEAK_RATIO` | `12.0` | `peak_ratio` is below it | `min_peak_ratio` | `--min-peak-ratio` | `min_peak_ratio` |

`MatchOptions.from_settings` clamps every value: `min_aligned_hashes >= 1`, `0 <= min_confidence <= 1`, `min_peak_ratio >= 0`.

How they interact:

- `min_aligned_hashes` is an absolute floor. It is the gate that matters for very short clips, which simply have few hashes.
- `min_peak_ratio` is relative to the noise floor. Internally a bin is only examined when its merged votes reach `max(min_aligned_hashes, ceil(min_peak_ratio * background))`: with a noisy histogram (background 3) the default 12 demands 36 aligned hashes, with a clean one (background 1) it demands 12. This is the knob that separates real matches from chance matches, and the first one to move in either direction.
- `min_confidence` is a quality floor on the alignment itself. The default 0.02 is deliberately permissive: degraded audio (phone lines, room microphones) legitimately aligns only a few percent of its hashes, and the quality labels already raise the bar for humans without hiding matches from machines. Raise it when you only want near-verbatim copies.

Other matching settings:

| Setting | Environment variable | Default | Notes |
|---|---|---|---|
| `top_k` | `AUDIOFP_TOP_K` | `5` | Tracks returned. Request field `top_k`, CLI `--top-k`, runtime key `top_k`. Bounded by `max_top_k`: a request above it is rejected (400), the CLI and the runtime layer clamp. |
| `max_top_k` | `AUDIOFP_MAX_TOP_K` | `50` | Upper bound a client may ask for. Environment only. |
| `offset_tolerance_frames` | `AUDIOFP_OFFSET_TOLERANCE_FRAMES` | `1` | Offset bins within +/- this many frames are merged when scoring; absorbs clip-start jitter (+/- 46 ms at the defaults). Environment only. |
| `max_occurrences_per_track` | `AUDIOFP_MAX_OCCURRENCES_PER_TRACK` | `25` | Cap on occurrences reported per track in `occurrences` mode. Environment, or per request through the `max_occurrences` form field (1-500). No CLI flag, not a runtime key. |
| `max_rows_per_hash` | `AUDIOFP_MAX_ROWS_PER_HASH` | `2000` | Query hashes stored more than this many times in the library are skipped as stop words (`diagnostics.skipped_common_hashes`). `0` disables the cap. Environment only; `Settings.validate` requires `>= 0`. |
| `max_search_votes` | `AUDIOFP_MAX_SEARCH_VOTES` | `5000000` | Upper bound on offset votes examined per search; when a query would exceed it, the rows of the most common hashes are dropped first (`diagnostics.dropped_for_vote_cap`) and the server logs `Search vote cap hit`. Environment only; must be `>= 10000`. |

## Where to set thresholds

Four layers, each overriding the one before it:

| Layer | Set with | Scope | Keys |
|---|---|---|---|
| 1. Built-in defaults | the field defaults of `Settings` | - | all (`mode` is hard-coded to `identify` in `Runtime`, not a `Settings` field) |
| 2. Environment | `AUDIOFP_*` variables, `.env` | process start | all but `mode` |
| 3. Runtime defaults | `PUT /api/v1/settings` (`PATCH` is an alias); the UI's *Settings -> Server search defaults* panel uses it | persisted in `<data_dir>/runtime-settings.json`, survives restarts | `top_k`, `min_confidence`, `min_aligned_hashes`, `min_peak_ratio`, `mode` (`RUNTIME_SETTING_KEYS` in `fingerprint/api/runtime.py`) |
| 4. Per request | `POST /api/v1/search` form fields; `audiofp search` flags | one search | `mode`, `top_k`, `min_confidence`, `min_aligned_hashes`, `min_peak_ratio`, `max_occurrences` (API only) |

```bash
# Environment: deployment-wide defaults
AUDIOFP_MIN_PEAK_RATIO=15 AUDIOFP_MIN_CONFIDENCE=0.03 audiofp serve

# Runtime defaults: persisted, apply to every client including the UI
curl -s -X PUT http://localhost:5000/api/v1/settings \
  -H "Content-Type: application/json" \
  -d '{"min_peak_ratio": 15, "min_confidence": 0.03, "mode": "occurrences"}'
curl -s http://localhost:5000/api/v1/settings

# One request
curl -s -X POST http://localhost:5000/api/v1/search \
  -F audio=@clip.wav -F mode=occurrences -F min_peak_ratio=20 -F max_occurrences=100

# One CLI call
audiofp search clip.wav --mode occurrences --min-peak-ratio 20 --min-aligned 8 --json
```

Add `-H "X-API-Key: ..."` when `AUDIOFP_API_KEY` is set.

Rules of the runtime layer (`Runtime._load_runtime_settings` and `Runtime.update_runtime_settings`):

- Unknown keys are rejected with HTTP 400 and the error's `details.allowed` lists the accepted ones. Values are clamped like `MatchOptions` (`top_k` to `1..max_top_k`, `min_confidence` to `0..1`, ...); `mode` must be `identify` or `occurrences`.
- The file wins over the environment for every key it contains, and every save writes all five keys, not only the changed one. Once someone has saved `min_peak_ratio` through the UI, changing `AUDIOFP_MIN_PEAK_RATIO` (or the variable behind any other runtime key) no longer moves the effective default. To hand control back to the environment, delete `<data_dir>/runtime-settings.json` and restart, or `PUT` the value you want.
- The CLI builds the same `Runtime`, so `audiofp search` with the same `--data-dir` honours the saved defaults as well. A different `--data-dir` starts from the environment.
- `GET /api/v1/info` shows the effective defaults under `defaults`, together with `frame_seconds` and `limits.max_occurrences_per_track`.

## Symptom -> knob

| Symptom | What is happening | Do this |
|---|---|---|
| Unrelated tracks show up as `weak` matches | Coincidental collisions form a flat histogram with a small bump that barely clears the thresholds; `peak_ratio` is low | Raise `min_peak_ratio` (12 -> 15-20) and leave `min_aligned_hashes` alone. Remember that `min_peak_ratio` is also an aligned-hash floor when the background is 1, so check that your shortest genuine clips still clear it (see the short-jingle row). Or filter on `quality` in the client. |
| False positives on repetitive audio (hold music, loops, alarms, synthetic tones) | Few distinct hashes repeated many times; simultaneous-peak (chord) hashes are shared between unrelated recordings with a similar timbre | Raise `min_peak_ratio` first, then `min_aligned_hashes` (10 -> 20). If that is not enough, `min_hash_time_delta=1` stops hashing simultaneous peaks - a fingerprint parameter, so re-index. |
| A genuinely repetitive pattern (a tone, a loop) is missed, and `diagnostics.skipped_common_hashes` is large or `dropped_for_vote_cap` is non-zero | Its hashes occur so often in the library that the stop-word cap (`max_rows_per_hash`) or the vote budget (`max_search_votes`) discards them before scoring | Raise `AUDIOFP_MAX_ROWS_PER_HASH` (2000 -> 10000, or `0` to disable) and, if the log shows `Search vote cap hit`, `AUDIOFP_MAX_SEARCH_VOTES`; restart. Both cost memory and time on every search, so keep the library of such patterns small or give it its own database. |
| Searches on long recordings are slow or memory-hungry | Common hashes multiply the votes (see `diagnostics.db_rows` and `votes`) | Lower `AUDIOFP_MAX_ROWS_PER_HASH` (2000 -> 500) or `AUDIOFP_MAX_SEARCH_VOTES`; shorten queries with `AUDIOFP_MAX_QUERY_SECONDS`. No re-index. |
| Noisy phone clips are missed (`found: false`) | Noise moves or removes peaks, only a few percent of the hashes survive, the spike is small | Use longer clips first (10-15 s instead of 3-5 s: `aligned_hashes` and `peak_ratio` grow with length). Then lower `min_confidence` (0.02 -> 0.01) and `min_peak_ratio` (12 -> 8). Last resort: denser fingerprints (`fan_value` 10 -> 15, `peak_neighborhood_size` 20 -> 15) and re-index. |
| Short jingles or stings (1-3 s) are missed | A 2 s clip yields few hashes; the alignment must span 3 frames and clear both `min_aligned_hashes` and `min_peak_ratio` (which acts as a floor on aligned hashes when the background is 1) | Lower `min_aligned_hashes` (10 -> 5) *and* `min_peak_ratio` (12 -> 6-8). Index the jingle as a track and search with the recording (see [occurrences mode](#occurrences-mode)). If still missed: smaller `peak_neighborhood_size`, higher `fan_value`, re-index. |
| A quiet pattern under loud speech is not found (background music, a faint beep) | The file is peak-normalised to its loudest sample, so the quiet part's peaks fall below `min_amplitude` | Lower `min_amplitude` (10 -> 5) and re-index. Expect more hashes and storage. |
| The right track is found but ranked below a near-duplicate | Ranking is by `aligned_hashes`, then `peak_ratio`; different masters of the same audio score alike | Raise `top_k` to see the runner-ups; deduplicate the library (`dedupe=content` only catches byte-identical files). |
| In `occurrences` mode the same event appears twice, a few frames apart | Jitter larger than the merge window (`2 * offset_tolerance_frames + 1` frames, i.e. 3 frames, about 139 ms, at the defaults) | Set `AUDIOFP_OFFSET_TOLERANCE_FRAMES=2` and restart. Do not go far: a loop whose period is shorter than the window would collapse into one occurrence. |
| The occurrences list stops at 25 | `max_occurrences_per_track` | Send `max_occurrences` (up to 500) with the request, or raise `AUDIOFP_MAX_OCCURRENCES_PER_TRACK`. |
| The reported span ends before the pattern really ends | Spans are anchor-based (see below) | Not a knob. Use `query_start_sec + known pattern length`. |
| Offsets look about 50 ms off | One frame is 46.4 ms at the defaults | Lower `hop_length` (512 -> 256) and re-index. Doubles frames, peaks and hashes. |
| Long recordings used as queries come back with `query.truncated: true` | `max_query_seconds` (3600) | Raise `AUDIOFP_MAX_QUERY_SECONDS`. Not a fingerprint parameter, no re-index. |

## Occurrences mode

`mode=occurrences` returns every spike per track that passes the thresholds instead of only the best one. It is the building block for pattern search: index the patterns (jingles, disclaimers, hold music, IVR prompts) as tracks, then search with each whole recording.

**Signed offsets.** `offset_frames = track_frame - query_frame`, reported as `offset_sec`. Positive: the query clip starts `offset_sec` into the track (`track_offset_sec`). Negative: the track starts *after* the query does, i.e. the track's content sits inside the query at `-offset_sec` (`query_offset_sec`). Since `track_offset_sec = max(0, offset_sec)` and `query_offset_sec = max(0, -offset_sec)`, you can always read the field for whichever side is the long one.

**Spans.** Every occurrence carries `query_start_sec`/`query_end_sec` and `track_start_sec`/`track_end_sec` (the query span shifted by the offset, clamped at 0 in the response). They are the first and last *anchor frame* that contributed an aligned hash; with more than 20 votes the 2nd-98th percentile is used so stray coincidences do not stretch the span. Because a hash is stored at its anchor, and an anchor near the end of a pattern pairs with peaks *beyond* the pattern - which differ between the indexed pattern and the recording - the last stretch of a pattern produces no aligned hashes and the span ends early. Treat `query_end_sec` as a lower bound. When the pattern length is known, compute the end as `query_start_sec + length`; the start is usually within a few frames of the truth.

**Which side to index.** Both directions work: `confidence` is normalised by the matched span and offsets are signed. For QA on call recordings, indexing the patterns and querying with the recording is the cheap direction: the database stays tiny, nothing is stored per call, and one search reports every pattern with every position. Indexing the recordings and querying with a pattern answers "which of the 10 000 calls contains this jingle", at the cost of storing every call's fingerprints.

**Duplicate suppression.** Spikes are examined strongest first. A candidate is dropped when its offset lies within `2 * offset_tolerance_frames + 1` frames of an occurrence already kept (jitter of the same event), or when it re-matches the same audio: its query span *and* its track span each overlap an existing occurrence's spans by more than half of the shorter span (`_same_region`). Everything else is a separate occurrence - including the periodic repeats of a loop, which are genuine alignments.

**Caps and order.** At most `max_occurrences_per_track` occurrences are kept per track (default 25; `max_occurrences` per request up to 500). Only bins whose merged vote count could pass the thresholds are examined, at most `MAX_CANDIDATE_BINS = 400` of them, strongest first, so very repetitive material stays bounded and real matches are examined before the cap bites. In the response the strongest occurrence comes first (it is also mirrored in the top-level match fields) and the rest are sorted by offset. In `identify` mode the list has exactly one entry.

**Long queries.** Queries longer than `max_query_seconds` (3600 by default) are truncated and flagged with `query.truncated`. Decoding and peak extraction run in `chunk_seconds` chunks, so an hour-long recording never has to sit in memory at once.

## Fingerprint parameters

These eight fields are marked `fingerprint=True` in `Settings` and, together with `FINGERPRINT_ALGORITHM_VERSION = 2`, form the *fingerprint signature* (`Settings.fingerprint_signature()`). `chunk_seconds` and `max_query_seconds` are not part of it: they change how audio is streamed, not what the fingerprints look like.

> **Changing any of them changes the fingerprints.** Query and library must be fingerprinted with identical parameters; a mismatch does not fail loudly, it silently stops matching. The signature is therefore stamped into the database when it is created, and on every start `create_storage` compares it with the current settings according to `fingerprint_compat`: `strict` (default) refuses to start, `warn` logs and continues, `ignore` skips the check. After changing a parameter run `audiofp db reset --yes` (it opens the database without the compatibility check, so it works on a mismatched one) and re-index everything, or point `AUDIOFP_SQLITE_PATH` / `AUDIOFP_POSTGRES_DSN` at a new database. `audiofp db check` prints the signature stored in the database; `audiofp doctor`, `GET /api/v1/health` and `GET /api/v1/info` print the one computed from the current settings.

At the defaults: one frame is 512/11025 s (about 46.4 ms), one frequency bin is 11025/2048 Hz (about 5.4 Hz), there are 1025 bins up to the 5.5 kHz Nyquist limit, and a hash may span up to 200 frames (about 9.3 s).

| Parameter | Environment variable | Default | What it controls | Trade-off | Limits (`Settings.validate`) |
|---|---|---|---|---|---|
| `sample_rate` | `AUDIOFP_SAMPLE_RATE` | `11025` | Working sample rate; all audio is resampled to it. Covers 0-5.5 kHz, ample for speech (telephone audio stops near 3.4 kHz) and enough for music identification. | Raising it (22050) widens the band, doubles the CPU cost and, with a fixed `n_fft`, coarsens frequency bins and shortens frames. Lowering it (8000) for phone-only material saves CPU and storage; scale `n_fft` and `hop_length` down with it to keep the frame timing. | `>= 4000` |
| `n_fft` | `AUDIOFP_N_FFT` | `2048` | STFT window length (Hann). Bin width is `sample_rate / n_fft`. | Larger: finer pitch resolution, blurrier timing, more bins per frame. Smaller: the opposite. | positive, even, `<= 8190` (bins must fit in 12 bits) |
| `hop_length` | `AUDIOFP_HOP_LENGTH` | `512` | Frame step. Frame duration `hop_length / sample_rate` is the offset resolution and the unit of every `*_frames` setting. | Smaller: finer timing but proportionally more frames, peaks and hashes (storage, CPU), and `max_hash_time_delta` / `offset_tolerance_frames` now mean less time. | `1..n_fft` |
| `peak_neighborhood_size` | `AUDIOFP_PEAK_NEIGHBORHOOD_SIZE` | `20` | Side of the square local-maximum window, in frames x bins: 20 frames is about 0.93 s, 20 bins about 108 Hz. | Smaller: denser constellation, more hashes, better for short or degraded clips, but more storage and more chance collisions (raise `min_peak_ratio` to compensate). Larger: sparser and cheaper. | `>= 3` |
| `min_amplitude` | `AUDIOFP_MIN_AMPLITUDE` | `10.0` | Minimum linear STFT magnitude for a peak. Applied after peak-normalisation, so it is relative to the loudest moment of the file (a full-scale sine peaks near 512 with the 2048-point Hann window). | Lower: keeps quiet passages and faint patterns, but adds noise-floor peaks that rarely survive degradation (storage, collisions). Higher: only prominent peaks. | - |
| `fan_value` | `AUDIOFP_FAN_VALUE` | `10` | Later peaks each anchor is paired with; up to `fan_value` hashes per peak. | Higher: more redundancy, so a degraded clip still shares enough pairs; storage grows linearly. | `>= 1` |
| `min_hash_time_delta` | `AUDIOFP_MIN_HASH_TIME_DELTA` | `0` | Minimum frame distance of a pair. `0` also pairs simultaneous peaks (the harmonics of a chord), which are noise-robust timbre hashes. | `1` drops them: fewer, more sequence-specific hashes; helps with very repetitive tonal material that keeps matching itself. | `0 <= min <= max` |
| `max_hash_time_delta` | `AUDIOFP_MAX_HASH_TIME_DELTA` | `200` | Maximum frame distance of a pair (about 9.3 s). | Larger: more distinctive pairs for sparse material, but a pair that reaches past the end of a short clip does not exist in that clip; for very short patterns a smaller value keeps a larger share of the hashes matchable. | `<= 4095` |

Also enforced: `chunk_seconds * sample_rate >= 4 * n_fft`. The `8190` and `4095` limits come from the 12-bit fields of the hash layout.

## Calibration recipe

Do this once per kind of material (studio music, call recordings and radio captures behave differently) and again after any fingerprint parameter change.

1. **Index a small representative set** into a separate data directory, so the experiment never touches production data:

   ```bash
   audiofp index ./calibration/library --data-dir data/calib --tags calib
   ```

   WAV, FLAC, OGG, Opus and MP3 decode natively; M4A/AAC/WMA and video files need ffmpeg on the `PATH` (`audiofp doctor` reports whether it is found). Do not use `--storage memory` here: it does not survive between the `index` and `search` processes.

2. **Prepare three query sets** of the length your users will really send (5-15 s clips, or whole recordings for pattern search):
   - *clean*: excerpts cut from indexed files;
   - *degraded*: the same excerpts as they will actually arrive - phone line, re-encoded, recorded off a speaker, with talk-over;
   - *unrelated*: audio that is not in the library, ideally of the same genre or from the same call centre.

3. **Search with the thresholds switched off**, so you see the raw scores including the chance matches that are normally hidden:

   ```bash
   for f in ./calibration/{clean,degraded,unrelated}/*; do
     audiofp search "$f" --data-dir data/calib --quiet --json \
       --min-aligned 1 --min-confidence 0 --min-peak-ratio 0 --top-k 10 \
     | jq -r --arg f "$(basename "$f")" \
         '.matches[] | [$f, .display_name, .aligned_hashes, .confidence, .peak_ratio, .quality] | @tsv'
   done
   ```

   `jq` is only for readability; the JSON has the same fields as the API. `MIN_ALIGNED_FRAMES` still applies, so single-instant coincidences are already filtered out.

4. **Look at the numbers.** For the clean and degraded sets note the scores of the *correct* track; for the unrelated set, and for the wrong tracks in the other sets, note the *highest* scores. You want a gap between the lowest correct `peak_ratio` and the highest wrong one, and likewise for `aligned_hashes`. As a reference point, the test-suite asserts `confidence > 0.2` and `peak_ratio > 15` for a clean 4-second excerpt of a synthetic 24-second track, and no match at all for unrelated synthetic audio.

5. **Pick thresholds** inside the gap, with margin: `min_peak_ratio` a little above the highest chance ratio, `min_aligned_hashes` below the lowest correct count for your shortest realistic clip, `min_confidence` below the lowest correct confidence in the degraded set. If there is no gap, lengthen the clips or move to the fingerprint parameters (denser fingerprints for missed matches, `min_hash_time_delta=1` for self-similar material) and repeat from step 1.

6. **Apply and verify.** Set the `AUDIOFP_*` variables for the deployment or `PUT /api/v1/settings`, then re-run the loop without the overrides and check the `thresholds` object in each response and that `found` flips the way you expect. `audiofp search` exits with status 1 when nothing is found, which makes the check scriptable.

## Limits

What the method does not do, so nobody has to find out in production:

- **Time-stretching, pitch shifting, speed changes.** A hash is an exact pair of frequency bins and an exact frame distance. A few percent of tempo or pitch change moves peaks to other bins and frames, and nothing aligns. There is no tolerant mode.
- **Different performances.** This is recording identification, not melody matching: covers, re-recordings, live versions and re-reads of a script do not match.
- **Very short clips.** Below roughly a second there are only a handful of peaks, the alignment must span 3 distinct frames, and the anchor-based span loses the tail of the pattern. Lower thresholds and denser fingerprints help, but reliability drops sharply with length.
- **Extremely noisy or narrow-band clips.** Below some signal-to-noise ratio too few peaks survive for any threshold to help. Length is the main lever; when even long clips fail, the clip is not fingerprintable with these parameters.
- **Very repetitive tonal material.** Loops, hold music and synthetic tones alias with themselves: every period is a legitimate alignment, and different recordings with the same timbre share simultaneous-peak hashes. `occurrences` reports up to `max_occurrences_per_track` of them, bounded by `MAX_CANDIDATE_BINS`. Sustained pure tones produce few distinct hashes (distinct counting was chosen precisely so they cannot fake alignments) and may not match at all, and hashes that the library repeats more than `max_rows_per_hash` times are skipped before scoring (`diagnostics.skipped_common_hashes`). `min_hash_time_delta=1` and higher thresholds reduce the effect but do not remove it.
- **Talk-over and mixing.** A pattern under loud speech degrades like noise, and a pattern present in only one stereo channel is averaged into mono before fingerprinting.
- **Scores are not probabilities.** They depend on clip length, track length and material. Calibrate per deployment instead of reusing numbers from elsewhere - including the ones in this document.
