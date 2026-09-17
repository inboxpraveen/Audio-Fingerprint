# AudioFP REST API

Complete reference for the HTTP API of AudioFP 2.x. Everything below is served
under the base URL

```
http://<host>:<port>/api/v1
```

(`audiofp serve` binds `AUDIOFP_HOST`/`AUDIOFP_PORT`, default `127.0.0.1:5000`;
the `production` profile defaults the host to `0.0.0.0`).
Paths in this document are relative to that base unless written in full.

- **Interactive docs**: `/docs` serves Swagger UI on top of the live
  specification at `/api/v1/openapi.json`. The page loads Swagger UI from a CDN;
  without internet access it falls back to a plain rendering of the same spec.
- **Discovery**: `GET /api/v1` (and `GET /api`) returns
  `{"name": "AudioFP", "version": "2.0.0", "openapi": "/api/v1/openapi.json", "docs": "/docs", "health": "/api/v1/health"}`.
- The bundled web UI at `/` uses only the endpoints described here, so anything
  the UI does can be scripted.
- The OpenAPI document is hand-written in `fingerprint/api/openapi.py`;
  `tests/test_api.py::test_openapi_covers_every_route` fails if a route is added
  without documenting it there.

Contents

1. [Conventions](#conventions) - auth, request IDs, uploads, formats
2. [Errors](#errors) - the envelope and every error code
3. [Search](#search) - `POST /search`, identify and occurrences modes
4. [Tracks](#tracks) - list, upload, index a folder, get, edit, delete, stream, tags
5. [Jobs](#jobs) - background indexing jobs
6. [System](#system) - health, info, stats, runtime settings, OpenAPI
7. [Deprecated 1.x aliases](#deprecated-1x-aliases-and-migration)
8. [Polling a job from Python](#polling-a-job-from-python)

---

## Conventions

### Authentication

Authentication is optional and switched on by setting `AUDIOFP_API_KEY` on the
server (`Settings.api_key`). When it is set, every request under `/api/` must
present the key in one of these ways:

| Method | Example | Accepted on |
|---|---|---|
| Header | `X-API-Key: <key>` | every endpoint |
| Bearer token | `Authorization: Bearer <key>` | every endpoint |
| Stream token | `?token=<token>` from `GET /tracks/{id}/stream-token` | **only** `GET .../audio` and `GET .../play` (browsers cannot attach headers to `<audio src>`). Tokens are HMAC-signed with the key, scoped to one track and expire after an hour; the key itself is never accepted in a URL. |

Two paths are always public: `/api/v1/health` and `/api/v1/openapi.json`
(`fingerprint/api/auth.py::PUBLIC_PATHS`). CORS pre-flight (`OPTIONS`) requests
are never challenged. The UI shell (`/`, `/docs`, `/static/...`) is outside
`/api/` and is served without a key; the UI asks for the key once and keeps it
in the browser's local storage.

A missing key returns `401` with code `unauthorized` and a `details.hint`; a
wrong key returns `401 unauthorized` as well. Comparison is constant-time. This
is a single shared secret - for per-user keys, SSO or rate limiting put AudioFP
behind a reverse proxy (see `docs/DEPLOYMENT.md`).

```bash
curl -H "X-API-Key: $AUDIOFP_API_KEY" http://127.0.0.1:5000/api/v1/stats
```

### Request IDs and headers

- Every response carries `X-Request-ID`. Send your own `X-Request-ID` to have
  it echoed back (only letters, digits and `._:-` are accepted, values longer
  than 64 characters are truncated, and anything else is replaced); otherwise
  the server generates a 12-hex id. The same id appears as `request_id` in error bodies and in the server log
  lines for that request, which is how you find a `500` in the logs.
- API responses are sent with `Cache-Control: no-store` and
  `X-Content-Type-Options: nosniff`.
- CORS is controlled by `AUDIOFP_CORS_ORIGINS` (comma-separated, `*` for any).
  No profile enables it by default - the bundled UI is same-origin, and `*` on a
  server without an API key would let any website drive the API.
  `X-Request-ID` is exposed to browser scripts.

### Request and response formats

- Uploads (`POST /search`, `POST /tracks`) are `multipart/form-data`; every
  other body is `application/json`. All responses are JSON except audio
  streaming.
- Boolean query/form/JSON fields accept `1/true/yes/on` and `0/false/no/off`.
- Timestamps (`indexed_at`, `created_at`, ...) are Unix epoch seconds as floats.
  Durations and offsets are seconds.
- Time positions are quantised to one STFT frame,
  `hop_length / sample_rate` = 512 / 11025 = ~0.046 s at the defaults. `GET /info`
  reports the exact value as `frame_seconds`.
- The request body size is capped by `AUDIOFP_MAX_UPLOAD_MB` (default 2048);
  anything larger is rejected with `413 payload_too_large`. JSON bodies have a
  separate, fixed cap of 1 MiB (`MAX_JSON_BODY_BYTES` in
  `fingerprint/api/validators.py`, enforced by `check_json_body_size()`):
  `PATCH`/`PUT /tracks/{id}`, `POST /tracks/index-directory`,
  `POST /tracks/bulk-delete` and `PUT`/`PATCH /settings` answer
  `413 payload_too_large` (`JSON body too large (max 1024 KB)`) for anything
  bigger, and `411 length_required` when a JSON body is sent with
  `Transfer-Encoding: chunked` and no `Content-Length`, because the size
  cannot be checked before reading it.

### Supported formats and ffmpeg

The file **extension** decides how a file is decoded
(`fingerprint/formats.py`); a file without an extension is rejected with
`400`, an unknown extension with `415 unsupported_format`.

| Group | Extensions | Decoder |
|---|---|---|
| `native_audio` | wav, wave, flac, ogg, oga, opus, mp3, aiff, aif, aifc, au, caf, w64 | libsndfile (the `soundfile` package) - always available |
| `ffmpeg_audio` | m4a, aac, wma, amr, ac3, dts, mka, weba | ffmpeg |
| `video` | mp4, mkv, avi, mov, wmv, flv, webm, m4v, mpeg, mpg, ts, mts, 3gp, vob | ffmpeg (audio track extracted) |

ffmpeg is optional at runtime. Without it, an `ffmpeg_audio` or `video` file
sent to `POST /search` fails with `422 ffmpeg_not_found`, and the same file in
an indexing job becomes a per-file error with `error_code: "ffmpeg_not_found"`.
`GET /info` returns the exact lists plus `ffmpeg.available`, so a client can
check before uploading.

---

## Errors

Every error - raised by AudioFP, by Flask, or unexpected - is a JSON object
with the same shape (`fingerprint/api/errors.py`):

```json
{
  "error": "'top_k' must be <= 50",
  "code": "validation_error",
  "status": 400,
  "details": {"field": "top_k", "value": 500},
  "request_id": "a1b2c3d4e5f6"
}
```

| Field | Notes |
|---|---|
| `error` | Human-readable message. Always a plain string (1.x clients parsed it as one). |
| `code` | Stable machine code - branch on this, not on the message. |
| `status` | The HTTP status, repeated in the body. |
| `details` | Optional object with context (`field`, `value`, `allowed`, `track_id`, `allowed_roots`, ...). Omitted when empty. |
| `request_id` | Correlates with the `X-Request-ID` header and the server log. |

### Error codes

Application error codes (the exception classes live in
`fingerprint/utils/exceptions.py`; `file_missing` is set by the audio-stream
route, `length_required` by the JSON-body check, and `payload_too_large` comes
either from Flask's request-size limit or from that same 1 MiB JSON-body
check):

| `code` | HTTP | Raised when |
|---|---|---|
| `validation_error` | 400 | A parameter is missing, malformed or out of range; no file was uploaded; the file has no extension; a JSON body is not an object; an unknown field is sent to `PATCH /tracks/{id}` or `PUT /settings`. |
| `job_error` | 400 | Cancelling a job that already finished, or deleting one that is still queued/running. |
| `job_error` | 429 | `POST /tracks/index-directory` while `4 x AUDIOFP_MAX_CONCURRENT_JOBS` or more jobs are already queued or running. |
| `unauthorized` | 401 | Missing or invalid API key. |
| `forbidden` | 403 | Directory indexing is disabled (`AUDIOFP_ALLOW_DIRECTORY_INDEXING=false`), the path is outside `AUDIOFP_INDEX_ROOTS`, or the server runs the `production` profile without `AUDIOFP_INDEX_ROOTS`. |
| `not_found` | 404 | Unknown `track_id` or `job_id` (also an unknown URL). |
| `file_missing` | 404 | `GET /tracks/{id}/audio` when the original file is no longer on disk (the fingerprint still works for searching). |
| `length_required` | 411 | A JSON body sent to one of the JSON endpoints below with `Transfer-Encoding: chunked` and no `Content-Length`; the server refuses to read a body of unknown size. |
| `payload_too_large` | 413 | Request body exceeds `AUDIOFP_MAX_UPLOAD_MB`, or a JSON body sent to `PATCH`/`PUT /tracks/{id}`, `POST /tracks/index-directory`, `POST /tracks/bulk-delete` or `PUT`/`PATCH /settings` exceeds 1 MiB (`MAX_JSON_BODY_BYTES`). |
| `unsupported_format` | 415 | The extension is not in the supported list. |
| `audio_decode_error` | 422 | libsndfile or ffmpeg rejected the file (corrupt, empty, no audio stream, decode timeout). |
| `empty_audio` | 422 | The file decoded to zero samples (empty or truncated audio). |
| `audio_processing_error` | 422 | Any other fingerprinting failure (out of memory while fingerprinting). |
| `ffmpeg_not_found` | 422 | The file needs ffmpeg and ffmpeg is not installed / not on `PATH`. |
| `matching_error` | 500 | Matching failed for a reason other than bad input. |
| `configuration_error` | 500 | Invalid server configuration. |
| `internal_error` | 500 | Unexpected exception. The message contains no detail; look the `request_id` up in the server log. |
| `storage_error` | 503 | The SQLite/PostgreSQL backend failed. |
| `fingerprint_incompatible` | 503 | The database was built with different fingerprint parameters (see `AUDIOFP_FINGERPRINT_COMPAT`). |

`conflict` (409) exists in the exception hierarchy but no endpoint currently
raises it. `configuration_error` and `fingerprint_incompatible` are raised
while the server starts (bad settings, incompatible database) and abort it
rather than reaching a client, and `matching_error` is only ever raised with
its code overridden to `validation_error`, so none of these three is returned
by an endpoint either.

Errors generated by Flask/Werkzeug rather than AudioFP use the same envelope
with these codes: `bad_request` (400), `unauthorized` (401), `forbidden` (403),
`not_found` (404), `method_not_allowed` (405), `conflict` (409),
`payload_too_large` (413), `unsupported_media_type` (415), `unprocessable` (422),
`too_many_requests` (429), `internal_error` (500), `service_unavailable` (503).
In practice you will meet `not_found` for a wrong URL, `method_not_allowed` for
a wrong verb and `payload_too_large` for oversized uploads.

**Per-file error codes inside jobs** are a separate vocabulary (they describe
one file, not the request): `audio_decode_error`, `ffmpeg_not_found`,
`unsupported_format`, `file_not_found`, `empty_audio`, `empty_fingerprint`
(silent, extremely short or noise-only audio), `audio_processing_error`,
`out_of_memory`, `internal_error`. They appear in a job's `errors[].error_code`
and in an upload job's `result.error_code`.

---

## Search

### `POST /search`

Upload a clip and match it against the library. Content type
`multipart/form-data`.

| Field | Type | Default | Description |
|---|---|---|---|
| `audio` | file | required | The clip, in any supported format. Audio longer than `AUDIOFP_MAX_QUERY_SECONDS` (default 3600) is truncated and `query.truncated` is set. |
| `mode` | `identify` \| `occurrences` | runtime setting (`identify`) | See below. |
| `top_k` | int, 1..`max_top_k` (50) | runtime setting (5) | Maximum number of tracks returned. |
| `min_confidence` | float, 0..1 | runtime setting (0.02) | Minimum `confidence` for an alignment to count. |
| `min_aligned_hashes` | int >= 1 | runtime setting (10) | Minimum distinct query hashes aligned at one offset. |
| `min_peak_ratio` | float >= 0 | runtime setting (12.0) | Minimum spike sharpness. |
| `max_occurrences` | int, 1..500 | `AUDIOFP_MAX_OCCURRENCES_PER_TRACK` (25) | Cap on occurrences reported per track. Ignored in `identify` mode. |

"Runtime setting" means the value from `GET /settings` (initially the
`AUDIOFP_TOP_K`, `AUDIOFP_MIN_CONFIDENCE`, `AUDIOFP_MIN_ALIGNED_HASHES`,
`AUDIOFP_MIN_PEAK_RATIO` server settings and `mode=identify`), which an
operator can change with `PUT /settings`. Form fields override it per request.
The effective values are echoed back in `thresholds`.

**Modes**

- `identify` - "what is this clip?". One alignment per track (the best one);
  `occurrences` contains exactly that one entry. Index whole recordings, search
  with short clips.
- `occurrences` - "where does this pattern appear?". Every alignment above the
  thresholds is returned per track, each with the time span it covers. Index
  short patterns (a jingle, a compliance disclaimer, hold music), search with a
  long recording, and read `occurrences[].query_offset_sec` to get every place
  the pattern plays. Two spikes within `2 x offset_tolerance_frames + 1` frames
  of each other, or covering the same query and track region, are treated as
  jitter of one occurrence rather than two.

Offsets are signed in both modes, so both directions work: a short query inside
a long track gives a positive `offset_sec`; a short indexed track found inside a
long query gives a negative one.

Two server-side guards bound the cost of a search and are reported in the
`diagnostics` object of the response: query hashes that occur more than
`AUDIOFP_MAX_ROWS_PER_HASH` (default 2000) times in the library are skipped as
"stop words" (`skipped_common_hashes`), and when the join between the returned
rows and the query would exceed `AUDIOFP_MAX_SEARCH_VOTES` (default 5,000,000)
votes, the most common hashes are dropped first (`dropped_for_vote_cap`, with a
warning in the server log). Neither can be changed per request.

**Responses**: `200` with a `SearchResponse`; `400 validation_error`;
`413 payload_too_large`; `415 unsupported_format`; `422 audio_decode_error`,
`422 ffmpeg_not_found` or `422 empty_audio`.

#### Example: identify a clip

```bash
curl -X POST http://127.0.0.1:5000/api/v1/search \
  -H "X-API-Key: $AUDIOFP_API_KEY" \
  -F "audio=@clip.wav" \
  -F "top_k=3"
```

```json
{
  "found": true,
  "mode": "identify",
  "matches": [
    {
      "track_id": "6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42",
      "title": "Second Song",
      "artist": "Beta",
      "filename": "Beta - Second Song.wav",
      "duration": 30.0,
      "source_type": "audio",
      "tags": ["music", "demo"],
      "num_hashes": 18342,
      "confidence": 0.4137,
      "aligned_hashes": 612,
      "peak_ratio": 87.43,
      "quality": "strong",
      "matched_rows": 1493,
      "offset_sec": 12.028,
      "track_offset_sec": 12.028,
      "query_offset_sec": 0.0,
      "query_start_sec": 0.046,
      "query_end_sec": 3.947,
      "track_start_sec": 12.074,
      "track_end_sec": 15.975,
      "match_offset_sec": 12.028,
      "occurrences": [
        {
          "offset_sec": 12.028,
          "track_offset_sec": 12.028,
          "query_offset_sec": 0.0,
          "query_start_sec": 0.046,
          "query_end_sec": 3.947,
          "track_start_sec": 12.074,
          "track_end_sec": 15.975,
          "aligned_hashes": 612,
          "confidence": 0.4137,
          "peak_ratio": 87.43,
          "quality": "strong"
        }
      ],
      "display_name": "Second Song"
    }
  ],
  "query": {
    "filename": "clip.wav",
    "duration_sec": 4.0,
    "num_peaks": 1210,
    "num_hashes": 1480,
    "truncated": false
  },
  "thresholds": {
    "min_confidence": 0.02,
    "min_aligned_hashes": 10,
    "min_peak_ratio": 12.0,
    "top_k": 3
  },
  "processing_time_ms": 41.7,
  "diagnostics": {
    "query_hashes": 1480,
    "db_rows": 1610,
    "votes": 1652,
    "candidate_tracks": 3,
    "scored_tracks": 1,
    "skipped_common_hashes": 0,
    "dropped_for_vote_cap": 0
  },
  "query_duration_sec": 4.0
}
```

Reading: the 4 s clip starts 12.03 s into "Second Song". When nothing passes the
thresholds the response is `{"found": false, "matches": [], ...}` with status
`200` - a miss is not an error.

#### Example: find every occurrence of a pattern in a recording

A 3 s jingle was indexed as a track ("Compliance jingle"); a 45 s call
recording is the query.

```bash
curl -X POST http://127.0.0.1:5000/api/v1/search \
  -H "X-API-Key: $AUDIOFP_API_KEY" \
  -F "audio=@call-2026-09-17.mp3" \
  -F "mode=occurrences" \
  -F "max_occurrences=50"
```

```json
{
  "found": true,
  "mode": "occurrences",
  "matches": [
    {
      "track_id": "0b7d3e51-2c44-4f0a-8a9e-1d6c5f2b9e37",
      "title": "Compliance jingle",
      "artist": "",
      "filename": "jingle.wav",
      "duration": 3.0,
      "source_type": "audio",
      "tags": ["qa"],
      "num_hashes": 1104,
      "confidence": 0.3871,
      "aligned_hashes": 402,
      "peak_ratio": 64.2,
      "quality": "strong",
      "matched_rows": 1877,
      "offset_sec": -5.991,
      "track_offset_sec": 0.0,
      "query_offset_sec": 5.991,
      "query_start_sec": 6.037,
      "query_end_sec": 8.916,
      "track_start_sec": 0.046,
      "track_end_sec": 2.926,
      "match_offset_sec": 0.0,
      "occurrences": [
        {
          "offset_sec": -5.991,
          "track_offset_sec": 0.0,
          "query_offset_sec": 5.991,
          "query_start_sec": 6.037,
          "query_end_sec": 8.916,
          "track_start_sec": 0.046,
          "track_end_sec": 2.926,
          "aligned_hashes": 402,
          "confidence": 0.3871,
          "peak_ratio": 64.2,
          "quality": "strong"
        },
        {
          "offset_sec": -20.991,
          "track_offset_sec": 0.0,
          "query_offset_sec": 20.991,
          "query_start_sec": 21.037,
          "query_end_sec": 23.917,
          "track_start_sec": 0.046,
          "track_end_sec": 2.926,
          "aligned_hashes": 388,
          "confidence": 0.3652,
          "peak_ratio": 61.9,
          "quality": "strong"
        }
      ],
      "display_name": "Compliance jingle"
    }
  ],
  "query": {
    "filename": "call-2026-09-17.mp3",
    "duration_sec": 45.0,
    "num_peaks": 13650,
    "num_hashes": 16920,
    "truncated": false
  },
  "thresholds": {
    "min_confidence": 0.02,
    "min_aligned_hashes": 10,
    "min_peak_ratio": 12.0,
    "top_k": 5
  },
  "processing_time_ms": 188.3,
  "diagnostics": {
    "query_hashes": 16920,
    "db_rows": 2140,
    "votes": 2231,
    "candidate_tracks": 4,
    "scored_tracks": 1,
    "skipped_common_hashes": 0,
    "dropped_for_vote_cap": 0
  },
  "query_duration_sec": 45.0
}
```

Reading: the jingle plays twice in the call, at 6.0 s and at 21.0 s; each
occurrence covers roughly 6.0-8.9 s and 21.0-23.9 s of the recording.

#### The match object

Every entry in `matches` describes one track. The scoring fields at the top
level (`confidence`, `aligned_hashes`, `peak_ratio`, `quality` and the seven
offset fields) are copied from the **strongest** occurrence, which is always
`occurrences[0]`; the remaining occurrences are sorted by offset.

| Field | Description |
|---|---|
| `track_id`, `title`, `artist`, `filename`, `duration`, `source_type`, `tags`, `num_hashes`, `display_name` | Track metadata (see [Tracks](#tracks)). `display_name` is the title, or the filename when there is no title. |
| `aligned_hashes` | Number of **distinct** query hashes that vote for this time offset (neighbouring offsets within `+/- offset_tolerance_frames` are merged, because clips rarely start exactly on a frame boundary). Distinct hashes, not raw votes, so a sustained tone cannot fake an alignment. |
| `confidence` | `aligned_hashes / distinct query hashes inside the matched span`, clamped to 0..1 and rounded to 4 decimals. It is normalised to the *matched region*, not the whole query, so a 3 s jingle inside a 45 s call can still score high. |
| `peak_ratio` | `aligned_hashes / mean votes per non-empty offset bin away from the spikes` (2 decimals): how sharp the spike is. Coincidental collisions sit around 3-9 regardless of library size; real matches are usually well above 10, hence the default `min_peak_ratio` of 12. |
| `quality` | Human-friendly bucket derived from the two scores (`quality_label` in `fingerprint/core/matcher.py`): `strong` when `confidence >= 0.15` and `peak_ratio >= 30`; `likely` when `confidence >= 0.05` and `peak_ratio >= 18`; otherwise `weak`. It is a label for people, not a threshold. |
| `matched_rows` | Raw number of fingerprint rows this track shared with the query before alignment. Diagnostic only. |
| `offset_sec` | Signed alignment, `track time - query time`. Positive: the query starts `offset_sec` into the track. Negative: the track starts `-offset_sec` into the query. |
| `track_offset_sec` | `max(0, offset_sec)`: where the start of the query lands in the track. `0` when the track's content sits inside the query. |
| `query_offset_sec` | `max(0, -offset_sec)`: where the start of the track lands in the query. `0` when the query sits inside the track. In `occurrences` mode this is "the pattern starts here in the recording". |
| `query_start_sec`, `query_end_sec` | Span of the **query** covered by the aligned hashes (2nd-98th percentile of the aligned anchor frames when there are more than 20, so a stray coincidence does not stretch it). |
| `track_start_sec`, `track_end_sec` | The same span in the **track's** timeline (`query span + offset_sec`, clamped at 0). |
| `match_offset_sec` | Deprecated alias of `track_offset_sec`, kept for 1.x clients. |
| `occurrences` | One `Occurrence` per alignment (offset fields + `aligned_hashes`, `confidence`, `peak_ratio`, `quality`). Exactly one entry in `identify` mode, up to `max_occurrences` in `occurrences` mode. |

Tracks are ranked by `(aligned_hashes, peak_ratio)` descending before the
`top_k` cut. All offsets and spans are rounded to 3 decimals and are multiples
of one frame (`frame_seconds`).

Top-level fields: `found` (`matches` is non-empty), `mode`, `matches`,
`query` (`filename`, `duration_sec`, `num_peaks`, `num_hashes`, `truncated`),
`thresholds` (`min_confidence`, `min_aligned_hashes`, `min_peak_ratio`, `top_k`
- the values actually used), `processing_time_ms`, `diagnostics`, and the
deprecated alias `query_duration_sec` (2 decimals).

`diagnostics` (`MatchDiagnostics` in `fingerprint/core/matcher.py`, always
present, integers) says what the matcher did for this query: `query_hashes`
(same as `query.num_hashes`), `db_rows` (fingerprint rows returned by the
storage lookup), `votes` (rows x query anchors after the join),
`candidate_tracks` (tracks with at least one vote), `scored_tracks` (tracks
that survived the vectorised prefilter and were scored one by one),
`skipped_common_hashes` (query hashes ignored because they occur more than
`AUDIOFP_MAX_ROWS_PER_HASH` times in the library) and `dropped_for_vote_cap`
(rows dropped to stay within `AUDIOFP_MAX_SEARCH_VOTES`). A miss with a large
`skipped_common_hashes` or `dropped_for_vote_cap` means the query was too
repetitive for the current caps (see `docs/TUNING.md`).

---

## Tracks

A *track* is any indexed piece of audio: a song, a call recording, a jingle.

### The track object

| Field | Type | Notes |
|---|---|---|
| `track_id` | string | Opaque UUID, assigned at indexing time. |
| `title`, `artist` | string | From the upload form or `PATCH`; otherwise parsed from an `Artist - Title.ext` filename (a filename without ` - ` becomes the title with an empty artist). |
| `filename` | string | Original filename as uploaded / found on disk. |
| `filepath` | string | Absolute path on the server. Only in `GET /tracks/{id}` and inside a job's `result.track`; omitted from lists and `PATCH` responses. |
| `content_hash` | string | SHA-256 of the file bytes (used for `AUDIOFP_DEDUPE=content`); empty for other dedupe modes. |
| `duration` | number | Seconds. |
| `num_peaks`, `num_hashes` | integer | Fingerprint size. |
| `source_type` | `audio` \| `video` | Derived from the extension. |
| `file_size` | integer | Bytes. |
| `indexed_at` | number | Epoch seconds. |
| `tags` | string[] | Normalised: trimmed, lower-cased, de-duplicated, at most 50. |
| `metadata` | object | Free-form JSON, max 64 KB (enforced on upload and on `PATCH`). Indexing always sets `metadata.source` (`upload`, `directory`, `cli` or `file`, depending on how the track came in). |
| `display_name` | string | `title`, else `filename`, else `track_id`. |
| `file_exists` | boolean | `GET /tracks/{id}` only: whether `filepath` is still on disk. |

### `GET /tracks` - list

| Query parameter | Default | Description |
|---|---|---|
| `q` | - | Case-insensitive substring search across `title`, `artist`, `filename` and `tags`. |
| `sort` | `indexed_at` | One of `indexed_at`, `title`, `artist`, `duration`, `filename`, `num_hashes` (`SORTABLE_FIELDS`). |
| `order` | `desc` | `asc` or `desc`. |
| `source_type` | - | `audio` or `video`. |
| `tag` | - | Only tracks carrying this tag (compared in normalised, lower-case form). |
| `page` | 1 | 1-based page number. |
| `per_page` | 50 | 1..200. |

Invalid values (`sort=evil`, `per_page=0`, ...) return `400 validation_error`.

```bash
curl "http://127.0.0.1:5000/api/v1/tracks?q=song&sort=title&order=asc&tag=demo&per_page=2"
```

```json
{
  "items": [
    {
      "track_id": "6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42",
      "title": "Second Song",
      "artist": "Beta",
      "filename": "Beta - Second Song.wav",
      "content_hash": "9c56cc51b374c3ba189210d5b6d4bf57790d351c96c47c02190ecf1e430635ab",
      "duration": 30.0,
      "num_peaks": 14980,
      "num_hashes": 18342,
      "source_type": "audio",
      "file_size": 1323044,
      "indexed_at": 1758100512.417,
      "tags": ["music", "demo"],
      "metadata": {"source": "upload"},
      "display_name": "Second Song"
    }
  ],
  "total": 1,
  "page": 1,
  "per_page": 2,
  "pages": 1,
  "songs": [],
  "count": 1
}
```

`songs` and `count` are deprecated aliases of `items` and `total` (`songs`
repeats the full `items` array; it is shortened here).

### `POST /tracks` - upload and index

`multipart/form-data`. Returns immediately with `202` and a job; the file is
fingerprinted in the background.

| Field | Required | Description |
|---|---|---|
| `audio` | yes | The file. If exactly one file is sent under another field name it is accepted too. |
| `title`, `artist` | no | Override the values parsed from the filename (trimmed, at most 500 characters). |
| `tags` | no | Comma-separated, e.g. `music, demo` (lower-cased, de-duplicated, at most 50). |
| `metadata` | no | A JSON **object encoded as a string**, e.g. `{"agent": "A1"}` (max 64 KB). Merged with `{"source": "upload"}`. Invalid values are rejected with `400` *before* the file is stored. |

```bash
curl -X POST http://127.0.0.1:5000/api/v1/tracks \
  -H "X-API-Key: $AUDIOFP_API_KEY" \
  -F "audio=@Beta - Second Song.wav" \
  -F "tags=music, demo" \
  -F 'metadata={"campaign": "spring"}'
```

```json
{
  "job_id": "3c9f1e2ab7d84f0e9a6b2c1d4e5f6a7b",
  "job": {
    "job_id": "3c9f1e2ab7d84f0e9a6b2c1d4e5f6a7b",
    "type": "upload",
    "label": "Beta - Second Song.wav",
    "status": "pending",
    "created_at": 1758100511.902,
    "started_at": null,
    "finished_at": null,
    "total": 1,
    "completed": 0,
    "succeeded": 0,
    "failed": 0,
    "skipped": 0,
    "current_item": null,
    "percent": 0.0,
    "elapsed_sec": 0.0,
    "eta_sec": null,
    "rate_per_sec": 0.0,
    "error_count": 0,
    "cancel_requested": false,
    "result": null,
    "error": null,
    "meta": {"filename": "Beta - Second Song.wav"},
    "errors": []
  },
  "filename": "Beta - Second Song.wav",
  "message": "Indexing started"
}
```

What happens next (poll `GET /jobs/{job_id}`, see [Jobs](#jobs)):

- The file is stored in `AUDIOFP_UPLOAD_DIR` (default `<data_dir>/uploads`) as
  `<8 hex chars>_<sanitised original name>`; the extension is preserved because
  the decoder relies on it.
- Success: `status: "completed"`, `succeeded: 1`, `result.status: "indexed"`,
  `result.track_id` and the full `result.track`.
- Duplicate (`AUDIOFP_DEDUPE=content`, the default, compares SHA-256 of the
  bytes; `path` compares the path; `none` disables): `skipped: 1`,
  `result.status: "duplicate"`, `result.duplicate_of` = the existing
  `track_id`. The uploaded copy is discarded.
- Bad file: the job still reaches `status: "completed"` but with `failed: 1`,
  an entry in `errors` and `result.status: "failed"` with `result.error` /
  `result.error_code`. The upload is discarded unless
  `AUDIOFP_KEEP_FAILED_UPLOADS=true`. A job `status` of `failed` means the
  runner itself crashed, which is a server problem, not a bad file.
- With the SQLite backend the upload job flushes the fingerprint write buffer
  (`storage.flush()`) as soon as the file is indexed, so a single upload is on
  disk right away; folder jobs flush once at the end of the run.

Errors: `400 validation_error` (no file, no extension, bad `metadata`),
`413 payload_too_large`, `415 unsupported_format`. The form fields are checked
before the file is written to the upload folder, so a `400` never leaves an
orphaned file behind.

### `POST /tracks/index-directory` - index a server-side folder

Index every supported file below a directory **on the server**. Files are
referenced in place (`filepath` points at the original), not copied.

```json
{"directory_path": "/srv/recordings/2026-09", "recursive": true, "tags": ["calls", "sept"]}
```

| Field | Default | Description |
|---|---|---|
| `directory_path` | required | Path on the server (`~` is expanded; a relative path is resolved against the server's working directory); must be an existing directory. Existence is checked only after the access-control rules below have passed. |
| `recursive` | `true` | Descend into sub-folders. |
| `tags` | `[]` | Array (or comma-separated string) applied to every indexed file. |

```bash
curl -X POST http://127.0.0.1:5000/api/v1/tracks/index-directory \
  -H "X-API-Key: $AUDIOFP_API_KEY" -H "Content-Type: application/json" \
  -d '{"directory_path": "/srv/recordings/2026-09", "tags": ["calls"]}'
```

```json
{
  "job_id": "7d2e9a4c1f0b4e8d9c3a5b6f7e8d9c0a",
  "job": { "...Job object, type \"directory\", total 1284..." },
  "total_files": 1284,
  "message": "Indexing started for 1284 files"
}
```

Access control, because this endpoint reads the server's filesystem:

- `AUDIOFP_ALLOW_DIRECTORY_INDEXING=false` disables it entirely (`403 forbidden`).
- `AUDIOFP_INDEX_ROOTS` (comma-separated) restricts it to those folders; a path
  outside them returns `403 forbidden` with `details.allowed_roots`.
- With the `production` profile, `AUDIOFP_INDEX_ROOTS` is mandatory - without
  it every request is refused with `403`. In `development` any path is allowed.
- `GET /info` reports the resulting `features.directory_indexing` flag.

Other errors: `400 validation_error` when `directory_path` is missing or
empty, when the path is not a directory or when it contains no supported media
files (`details.supported` lists the formats); `413 payload_too_large` for a
JSON body over 1 MiB; `429 job_error` when `4 x AUDIOFP_MAX_CONCURRENT_JOBS`
or more jobs are already queued or running. The checks run in that order:
missing path (400), then authorisation (403), then existence (400), so a
non-existent path outside the allowed roots gets `403`, not `400` - the server
never reveals whether a forbidden path exists.

The finished job's `result` is the indexing summary:

```json
{
  "total": 1284, "indexed": 1201, "duplicates": 80, "failed": 3, "cancelled": false,
  "elapsed_sec": 611.42,
  "track_ids": ["..."],
  "errors": [{"file": "/srv/recordings/2026-09/broken.wav", "error": "Could not decode 'broken.wav': ...", "error_code": "audio_decode_error"}],
  "duplicates_of": [{"file": "/srv/recordings/2026-09/copy.wav", "duplicate_of": "6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42"}],
  "directory": "/srv/recordings/2026-09"
}
```

### `GET /tracks/{track_id}` - get one

Returns the track object including `filepath` and `file_exists`. `404 not_found`
with `details.track_id` when unknown.

```bash
curl http://127.0.0.1:5000/api/v1/tracks/6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42
```

### `PATCH /tracks/{track_id}` - edit (`PUT` is an alias)

JSON body with any of `title`, `artist`, `tags`, `metadata`. Anything else
returns `400 validation_error` with `details.allowed`; an empty body is `400`
too, and a body over 1 MiB is `413 payload_too_large`.

- `title` / `artist`: trimmed, at most 500 characters; `null` clears.
- `tags`: array or comma-separated string, normalised as above.
- `metadata`: **replaces** the whole object (it is not merged); max 64 KB.

```bash
curl -X PATCH http://127.0.0.1:5000/api/v1/tracks/6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42 \
  -H "Content-Type: application/json" \
  -d '{"title": "Renamed", "tags": ["QA"], "metadata": {"source": "upload", "agent": "A1"}}'
```

Returns the updated track (`tags` comes back as `["qa"]`).

### `DELETE /tracks/{track_id}` - delete

Removes the track and all its fingerprints.

| Query parameter | Default | Description |
|---|---|---|
| `delete_file` | `false` | Also delete the file behind the track. Only files inside the upload folder are ever removed; files indexed from a directory are left alone and `file_removed` is `false`. |

```bash
curl -X DELETE "http://127.0.0.1:5000/api/v1/tracks/6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42?delete_file=true"
```

```json
{"deleted": true, "track_id": "6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42", "file_removed": true, "message": "Track deleted"}
```

`404 not_found` when the track does not exist (a second delete is a 404).

### `POST /tracks/bulk-delete`

```json
{"track_ids": ["6f1c2a9e-...", "0b7d3e51-..."], "delete_files": false}
```

`track_ids` must be a non-empty list of at most 1000 strings. Unknown ids are
skipped silently, so `deleted` can be smaller than `requested`.

```json
{"deleted": 2, "requested": 3, "files_removed": 0}
```

### `GET /tracks/{track_id}/audio` - stream the original file

Streams the file from disk with a `Content-Type` guessed from the filename
(`application/octet-stream` if unknown). HTTP `Range` requests are honoured
(`206 Partial Content` with `Content-Range`), so browser players can seek.

| Query parameter | Default | Description |
|---|---|---|
| `download` | `false` | Send `Content-Disposition: attachment` with the original filename. |
| `token` | - | A stream token (see below) - the only way to authenticate this endpoint without headers. |

```bash
curl -H "Range: bytes=0-1023" -o head.bin \
  -H "X-API-Key: $AUDIOFP_API_KEY"   "http://127.0.0.1:5000/api/v1/tracks/6f1c2a9e-8b3d-4e7a-9c21-5d0f3b7a1e42/audio"
```

#### `GET /tracks/{track_id}/stream-token` - token for `<audio src>`

Browsers cannot add headers to media elements, so when the server requires an
API key the UI asks for a short-lived token first and puts *that* in the URL:

```json
{"token": "MTc4OTU5...", "expires_at": 1789590000, "url": "/api/v1/tracks/6f1c2a9e-.../audio?token=MTc4OTU5...", "auth_required": true}
```

The token is an HMAC of the track id and expiry signed with the API key
(`fingerprint/api/auth.py::make_stream_token`), valid for `STREAM_TOKEN_TTL`
(3600 s) and only for that track; a token that leaks into a proxy log is
useless soon after and never reveals the key. Without an API key configured the
endpoint returns `"token": null` and the plain URL. The access log redacts
`token=` and `api_key=` query values.

`404 not_found` for an unknown track; `404 file_missing` when the track exists
but its file has been removed from disk (searching still works, only playback
is gone). `GET /tracks/{id}/play` and `GET /songs/{id}/play` are aliases.

### `GET /tags` - distinct tags with counts

```bash
curl "http://127.0.0.1:5000/api/v1/tags"
```

```json
{"tags": [{"tag": "calls", "count": 1201}, {"tag": "qa", "count": 3}]}
```

Sorted by count descending, then name. Counts come from a bounded scan of the
most recently indexed tracks (`scan_limit`, default 2000, 1..20000), so on a
very large library the numbers are approximate.

---

## Jobs

Uploads and directory scans run as background jobs on a thread pool of
`AUDIOFP_MAX_CONCURRENT_JOBS` (default 2) workers. Jobs are kept in memory and,
with `AUDIOFP_PERSIST_JOBS=true` (default), written to `<data_dir>/jobs/<job_id>.json`
so history survives restarts. The last `AUDIOFP_JOB_HISTORY_LIMIT` (default 200)
finished jobs are retained; each job keeps at most `AUDIOFP_JOB_MAX_ERRORS`
(default 500) per-file errors.

### The job object

| Field | Description |
|---|---|
| `job_id` | 32-hex id. |
| `type` | `upload` or `directory`. |
| `label` | The original filename (upload) or the directory path. |
| `status` | See the table below. |
| `created_at`, `started_at`, `finished_at` | Epoch seconds; `started_at`/`finished_at` are `null` until they happen. |
| `total`, `completed`, `succeeded`, `failed`, `skipped` | File counters. `skipped` counts duplicates. `completed = succeeded + failed + skipped`. |
| `current_item` | Filename being processed, or `null`. |
| `percent` | `100 * completed / total` (1 decimal). A completed job with `total` 0 reports 100. |
| `elapsed_sec` | Seconds from `started_at` (or `created_at` while still pending) to `finished_at`, or to now while the job is unfinished. Computed when you read it. |
| `eta_sec` | Estimated seconds remaining; `null` unless the job is `running` and has made progress. |
| `rate_per_sec` | Files per second so far. |
| `error_count` | Number of per-file errors recorded. |
| `errors` | `[{"file", "error", "error_code"}]`. Always present on `GET /jobs/{id}`; on `GET /jobs` only with `include_errors=true`. |
| `result` | `null` until finished. Upload: the file outcome (`file`, `filename`, `status`, `elapsed_sec`, `track_id`, `track`, `duplicate_of`, `error`, `error_code` as applicable). Directory: the summary shown under `index-directory`. |
| `error` | Message when the job itself failed or was interrupted; otherwise `null`. |
| `cancel_requested` | `true` once `POST /jobs/{id}/cancel` was called. |
| `meta` | `{"filename"}` for uploads, `{"directory", "recursive"}` for directory jobs. |

| `status` | Meaning |
|---|---|
| `pending` | Queued, not started. |
| `running` | In progress; `current_item`, `percent` and `eta_sec` are live. |
| `completed` | Finished normally. Note that per-file problems do **not** change this status: check `failed`, `skipped` and `errors`. |
| `failed` | The job runner raised (storage down, a bug); see `error`. Files indexed before the failure are kept. A directory job whose storage fails mid-run stops submitting work, waits for in-flight files and then fails with an error such as `Indexing stopped after 12 file(s): ...` (`error_code` `storage_error`), so an operator never mistakes a broken run for a finished one. |
| `cancelled` | Stopped by `POST /jobs/{id}/cancel`. Files already indexed are kept; a directory job that had started reports `result.cancelled: true` (a job cancelled while still `pending` has `result: null`). |
| `interrupted` | The server restarted while the job was `pending` or `running` (only possible with persisted jobs). `error` explains it; re-run the job to index the rest - duplicates are skipped, so re-running is cheap. |

Terminal statuses: `completed`, `failed`, `cancelled`, `interrupted`.

### `GET /jobs` - list

| Query parameter | Default | Description |
|---|---|---|
| `status` | all | `active` (= `pending,running`) or a comma-separated list of statuses. Unknown values return `400` with `details.allowed`. |
| `type` | all | `upload` or `directory`. |
| `limit` | 100 | 1..1000, newest first. |
| `include_errors` | `false` | Include each job's `errors` array. |

```bash
curl "http://127.0.0.1:5000/api/v1/jobs?status=active"
```

```json
{
  "items": [
    {
      "job_id": "7d2e9a4c1f0b4e8d9c3a5b6f7e8d9c0a",
      "type": "directory",
      "label": "/srv/recordings/2026-09",
      "status": "running",
      "created_at": 1758100600.118,
      "started_at": 1758100600.121,
      "finished_at": null,
      "total": 1284,
      "completed": 412,
      "succeeded": 390,
      "failed": 2,
      "skipped": 20,
      "current_item": "call-000413.mp3",
      "percent": 32.1,
      "elapsed_sec": 196.44,
      "eta_sec": 415.8,
      "rate_per_sec": 2.097,
      "error_count": 2,
      "cancel_requested": false,
      "result": null,
      "error": null,
      "meta": {"directory": "/srv/recordings/2026-09", "recursive": true}
    }
  ],
  "active": 1
}
```

`active` is the number of `pending` + `running` jobs across the whole server
(the same number is exposed as `jobs_active` in `GET /stats`).

### `GET /jobs/{job_id}` - status and progress

Returns the job object including `errors`. `404 not_found` when unknown (also
after the job was removed or aged out of history).

### `POST /jobs/{job_id}/cancel`

Requests cancellation and returns the job. A `pending` job becomes `cancelled`
immediately. A `running` job keeps `status: "running"` with
`cancel_requested: true` until the runner notices (at the next file boundary)
and finishes as `cancelled`; files already indexed stay indexed.
`400 job_error` if the job is already in a terminal status.

### `DELETE /jobs/{job_id}`

Removes a finished job from history (and its JSON file). `400 job_error` for a
`pending`/`running` job - cancel it first. Returns
`{"deleted": true, "job_id": "..."}`.

---

## System

### `GET /health` - liveness/readiness (no auth)

```bash
curl http://127.0.0.1:5000/api/v1/health
```

```json
{
  "status": "ok",
  "version": "2.0.0",
  "uptime_sec": 8123.4,
  "storage": {"type": "sqlite", "ok": true},
  "ffmpeg": {"available": true, "version": "7.1"},
  "jobs": {"active": 1},
  "fingerprint": {"algorithm_version": 2, "signature": "1f3a9c2e7b6d4a58"},
  "timestamp": 1758100800.002
}
```

Returns `200` with `status: "ok"`, or `503` with `status: "degraded"` when the
storage backend does not answer. Use it for container health checks. `signature`
is a hash of the fingerprint parameters; a database only matches fingerprints
computed with the same signature.

### `GET /info` - capabilities, formats, limits, defaults

```json
{
  "version": "2.0.0",
  "profile": "production",
  "storage_type": "sqlite",
  "ffmpeg": {"available": true, "version": "7.1", "path": "/usr/bin/ffmpeg"},
  "formats": {
    "native_audio": ["aif", "aifc", "aiff", "au", "caf", "flac", "mp3", "oga", "ogg", "opus", "w64", "wav", "wave"],
    "ffmpeg_audio": ["aac", "ac3", "amr", "dts", "m4a", "mka", "weba", "wma"],
    "video": ["3gp", "avi", "flv", "m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "mts", "ts", "vob", "webm", "wmv"]
  },
  "limits": {"max_upload_mb": 2048, "max_query_seconds": 3600.0, "max_top_k": 50, "max_occurrences_per_track": 25},
  "features": {"directory_indexing": true, "index_roots": ["/srv/recordings"], "auth_required": true, "dedupe": "content"},
  "fingerprint": {
    "sample_rate": 11025, "n_fft": 2048, "hop_length": 512,
    "peak_neighborhood_size": 20, "min_amplitude": 10.0, "fan_value": 10,
    "min_hash_time_delta": 0, "max_hash_time_delta": 200,
    "algorithm_version": 2, "signature": "1f3a9c2e7b6d4a58"
  },
  "defaults": {"top_k": 5, "min_confidence": 0.02, "min_aligned_hashes": 10, "min_peak_ratio": 12.0, "mode": "identify"},
  "frame_seconds": 0.046439909297052155
}
```

Clients should read `formats` and `limits` from here instead of hard-coding
them. `defaults` is the same object as `GET /settings`.

### `GET /stats` - library statistics

Cheap (never scans the fingerprint table), safe to poll.

```json
{
  "storage_type": "sqlite",
  "total_tracks": 1204,
  "total_hashes": 22093311,
  "total_duration_sec": 391220.5,
  "db_path": "/srv/audiofp/data/fingerprints.db",
  "db_size_bytes": 734003200,
  "pending_rows": 0,
  "persistent": true,
  "total_songs": 1204,
  "jobs_active": 1
}
```

`db_path` and `pending_rows` are SQLite-only and `db_size_bytes` is absent for
the `memory` backend. `pending_rows` is the number of fingerprint rows still in
the SQLite write buffer (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`); those tracks are
already searchable, the rows just have not been written to disk yet.
`total_songs` is a deprecated alias of `total_tracks`.

### `GET /settings` and `PUT /settings` - runtime search defaults

The five search defaults can be changed at runtime without restarting. They
apply to every `POST /search` that does not override them, to the CLI and to
the UI, and are persisted to `<data_dir>/runtime-settings.json`
(`Settings.runtime_settings_path`) so they survive restarts.

```bash
curl http://127.0.0.1:5000/api/v1/settings
```

```json
{"top_k": 5, "min_confidence": 0.02, "min_aligned_hashes": 10, "min_peak_ratio": 12.0, "mode": "identify"}
```

`PUT` (or `PATCH`) accepts a JSON object with any subset of those keys:

```bash
curl -X PUT http://127.0.0.1:5000/api/v1/settings \
  -H "Content-Type: application/json" \
  -d '{"mode": "occurrences", "min_peak_ratio": 15}'
```

```json
{"message": "Settings updated", "top_k": 5, "min_confidence": 0.02, "min_aligned_hashes": 10, "min_peak_ratio": 15.0, "mode": "occurrences"}
```

Numeric values are **clamped** rather than rejected: `top_k` to
`1..max_top_k`, `min_confidence` to `0..1`, `min_aligned_hashes` to `>= 1`,
`min_peak_ratio` to `>= 0`. `mode` must be `identify` or `occurrences`. An
unknown key (`details.allowed` lists the keys), a non-numeric value, an empty
body or a non-object body returns `400 validation_error`; a body over 1 MiB
returns `413 payload_too_large`.

### `GET /openapi.json`

The OpenAPI 3.0.3 document for this server (public, no key needed). Its
`security` section is empty when `AUDIOFP_API_KEY` is not set, and lists the
`ApiKeyHeader` / `BearerToken` schemes when it is.

---

## Deprecated 1.x aliases and migration

The 1.x paths still work so existing clients keep running, but they are marked
`deprecated` in the OpenAPI document and return the 2.x response shapes.

| Deprecated | Use instead |
|---|---|
| `POST /upload` | `POST /tracks` |
| `POST /index` | `POST /tracks/index-directory` |
| `GET /songs` | `GET /tracks` |
| `GET /songs/{track_id}` | `GET /tracks/{track_id}` |
| `DELETE /songs/{track_id}` | `DELETE /tracks/{track_id}` |
| `GET /songs/{track_id}/play`, `GET /tracks/{track_id}/play` | `GET /tracks/{track_id}/audio` |

Method aliases (not deprecated, same handler): `PUT /tracks/{track_id}` is
identical to `PATCH /tracks/{track_id}`, and `PATCH /settings` is identical to
`PUT /settings`.

Response-level aliases kept for 1.x clients: `songs` and `count` in the track
list (same data as `items` and `total`), `total_songs` in `/stats`,
`match_offset_sec` on a match (equal to `track_offset_sec`) and
`query_duration_sec` on the search response.

What changed for a 1.x client:

- **`song_id` is now `track_id`** everywhere: in track objects, in matches and
  in the `DELETE` response. There is no `song_id` field any more.
- `match_offset_sec` is still sent but is never negative; read `offset_sec`
  (signed) or `query_offset_sec` for patterns found inside the query.
- Matches gained `aligned_hashes`, `peak_ratio`, `quality`, the span fields and
  `occurrences`; `confidence` is now normalised to the matched span instead of
  the whole query, so absolute values differ from 1.x.
- `GET /songs` returns a paginated page (`items`, `total`, `page`, `per_page`,
  `pages`) with the old `songs`/`count` keys alongside; the default page size
  is 50, so clients that expected the whole library must paginate.
- `DELETE` returns `{"deleted": true, "track_id": ..., "file_removed": ..., "message": "Track deleted"}`
  instead of `{"message": "Song deleted", "song_id": ...}`.
- Job objects were renamed and extended: `current_file` -> `current_item`,
  `success` -> `succeeded`, `completed_at` -> `finished_at`; new counters
  (`skipped`, `percent`, `eta_sec`, `rate_per_sec`, `error_count`), new statuses
  (`cancelled`, `interrupted`), and per-file errors carry an `error_code`. The
  upload job's `result` is the file outcome (`status`, `track_id`, `track`,
  `duplicate_of`) rather than a `success` count.
- `/health` reports `"status": "ok"` (was `"healthy"`) and returns `503` when
  storage is down.
- `/stats` no longer includes `unique_hashes` (it required a full index scan);
  `total_tracks`, `total_duration_sec`, `db_size_bytes`, `persistent` and
  `jobs_active` were added.
- Error bodies gained `code`, `status` and `request_id`; `error` is still a
  string.
- Unsupported formats now return `415` (was `400`) and undecodable audio `422`
  (was `500`).

---

## Polling a job from Python

Uploads and directory scans return `202` straight away; poll the job until it
reaches a terminal status.

```python
import time

import requests

BASE = "http://127.0.0.1:5000/api/v1"
HEADERS = {"X-API-Key": "change-me"}  # drop this when AUDIOFP_API_KEY is not set
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

with open("Beta - Second Song.wav", "rb") as fh:
    r = requests.post(f"{BASE}/tracks", headers=HEADERS, files={"audio": fh}, data={"tags": "music,demo"})
r.raise_for_status()  # 202
job_id = r.json()["job_id"]

while True:
    r = requests.get(f"{BASE}/jobs/{job_id}", headers=HEADERS)
    r.raise_for_status()  # 404 if the job aged out of history
    job = r.json()
    print(f"{job['status']:<11} {job['percent']:5.1f}%  eta={job['eta_sec']}  current={job['current_item']}")
    if job["status"] in TERMINAL:
        break
    time.sleep(1)

if job["status"] != "completed":
    raise SystemExit(f"job ended as {job['status']}: {job['error']}")

result = job["result"]
if result["status"] == "indexed":
    track = result["track"]
    print("indexed as", result["track_id"], "-", track["title"] or track["filename"])
elif result["status"] == "duplicate":
    print("already indexed as", result["duplicate_of"])
else:  # "failed": a problem with this file, reported inside a completed job
    print("not indexed:", result["error_code"], "-", result["error"])
```

The same loop works for `POST /tracks/index-directory`; there `result` is the
summary object (`indexed`, `duplicates`, `failed`, `errors`, ...) and
`job["errors"]` lists every file that could not be indexed.
