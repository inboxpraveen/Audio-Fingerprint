# Troubleshooting

Symptom-oriented guide for operators. Every section names the error `code`,
the log line or the UI message you will actually see, and the setting or
command that fixes it. Settings are fields of `fingerprint/config.py`
`Settings`; each one is also an environment variable `AUDIOFP_<FIELD_UPPER>`
(see `docs/CONFIGURATION.md`). Endpoints live under `/api/v1` (see
`docs/API.md`).

## Start with `audiofp doctor`

Before anything else:

```bash
audiofp doctor            # or: python -m fingerprint doctor
```

It runs the same settings loader as the server (profile, `.env`, environment
variables, plus `--profile`, `--data-dir`, `--storage`, `--sqlite-path` if you
pass them) and prints one line per check, grouped as **Runtime**,
**Dependencies**, **ffmpeg**, **Configuration** and **Storage**:

```text
AudioFP 2.0.0 doctor

Runtime
  [ok]   Python 3.12.4 on Windows AMD64

Dependencies
  [ok]   soundfile (libsndfile) 0.12.1, libsndfile 1.2.2
  [ok]   waitress (production server)

ffmpeg
  [warn] ffmpeg not found: video files and M4A/AAC/WMA cannot be decoded. Install it: winget install Gyan.FFmpeg | brew install ffmpeg | apt install ffmpeg
       native audio formats: aif, aifc, aiff, au, caf, flac, mp3, oga, ogg, opus, w64, wav, wave

Configuration
  [ok]   profile=development storage=sqlite data_dir=C:\srv\audiofp\data
  [ok]   fingerprint signature 7c1e0d2a9b4f6e31 (sample_rate=11025, n_fft=2048, ...)
  [ok]   data dir writable: C:\srv\audiofp\data
  [ok]   free disk space: 118.4 GB

Storage
  [ok]   sqlite reachable: 1240 tracks, 41,882,105 hashes

All checks passed with 1 warning(s).
```

(abbreviated). `[FAIL]` lines make the command exit with status 1; `[warn]`
lines do not. The checks that matter most:

| Line | What it means |
|---|---|
| `libsndfile lacks MP3, ... support; those files will need ffmpeg` | Your `soundfile` wheel bundles an old libsndfile. Upgrade `soundfile` (>= 0.12 bundles MP3 support) or install ffmpeg. |
| `waitress not installed` | `audiofp serve --profile production` will fall back to Flask's development server. `pip install waitress`. |
| `production profile without AUDIOFP_API_KEY` | Anyone who can reach the port can use the API. Set `AUDIOFP_API_KEY`. |
| `directory indexing is effectively disabled in production until AUDIOFP_INDEX_ROOTS is set` | See [403 when indexing a folder](#403-when-indexing-a-folder). |
| `data dir not writable` / `upload dir not writable` | Fix permissions, or point `AUDIOFP_DATA_DIR` / `AUDIOFP_UPLOAD_DIR` elsewhere. |
| `fingerprint_incompatible: ...` under **Storage** | See [`fingerprint_incompatible`](#fingerprint_incompatible). |
| `storage_error: ...` under **Storage** | SQLite file cannot be opened, PostgreSQL unreachable, or schema newer than this version. The message says which. |

Three more commands answer "what is the server actually using?":

```bash
audiofp config            # effective settings (secrets shown as ***), plus _fingerprint_signature
audiofp config --json
audiofp db check          # opens the database and prints track/hash counts and the stored signature
```

Settings come from three layers: field defaults, the profile
(`AUDIOFP_PROFILE` / `--profile`), then `AUDIOFP_*` environment variables.
A `.env` file in the **working directory** is read too (or the file named by
`AUDIOFP_ENV_FILE`), so "my setting is ignored" is usually a different
working directory or a `.env` you forgot about. `audiofp config` shows the
result of all three layers.

On a running server, `GET /api/v1/health` (no API key needed) reports
`status`, `storage.ok`, `ffmpeg.available` and `jobs.active`, and returns
HTTP 503 with `status: "degraded"` when the storage backend fails its health
check. `GET /api/v1/info` reports formats, limits, feature flags and the
fingerprint parameters.

## Reading an API error

Every error, including ones raised by Flask itself, is JSON:

```json
{
  "error": "'call.m4a' (.m4a) requires ffmpeg, which is not installed. Install ffmpeg and make sure it is on PATH: ...",
  "code": "ffmpeg_not_found",
  "status": 422,
  "details": {"extension": ".m4a"},
  "request_id": "3f9c2a1b7d4e"
}
```

`code` is stable and meant for branching in clients; `error` is for humans;
`details` is optional; `request_id` is also sent as the `X-Request-ID`
response header and appears in every log line written while handling that
request (see [Logs and request ids](#logs-and-request-ids)).

One distinction saves a lot of confusion:

* `POST /api/v1/search` decodes the clip synchronously, so decode problems
  come back as HTTP errors (`ffmpeg_not_found`, `audio_decode_error`, ...).
* `POST /api/v1/tracks` (upload) and `POST /api/v1/tracks/index-directory`
  return **202** immediately with a `job_id`. Decode problems for those show
  up in the job, not in the HTTP response: `GET /api/v1/jobs/{job_id}` ->
  `errors[]` with a per-file `error_code`, or `result.status: "failed"` for a
  single upload. The web UI shows the same thing in **Activity**.

### HTTP error codes

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| 400 | `validation_error` | Bad input: no file in the `audio` field, a file without extension, an out-of-range number, unknown sort field, `directory_path` that is not a directory on the server, empty `PATCH` body, ... | Read `error`; `details.field` names the offending field. |
| 400 | `job_error` | Cancelling a job that already finished, removing a job that is still running, or submitting while the server is shutting down. | Check `GET /api/v1/jobs/{id}` first; cancel before removing. |
| 401 | `unauthorized` | Missing or wrong API key. | [401 unauthorized](#401-unauthorized). |
| 403 | `forbidden` | Folder indexing not allowed by configuration. | [403 when indexing a folder](#403-when-indexing-a-folder). |
| 404 | `not_found` | Unknown track id, job id or route. | `details.track_id` / `details.job_id` echo what you asked for. |
| 404 | `file_not_found` | The decoder was given a path that does not exist. Over HTTP this is practically unreachable (uploads are saved to disk first); you will meet it as a per-file job code instead. | Re-run the index. |
| 404 | `file_missing` | The track exists but its original file is gone, so it cannot be streamed. | [Playback fails](#playback-fails-but-search-works). |
| 411 | `length_required` | A JSON body was sent to a JSON endpoint with `Transfer-Encoding: chunked` and no `Content-Length`, so its size cannot be checked before reading it. | Send `Content-Length`; most HTTP clients do unless the body is streamed. |
| 413 | `payload_too_large` | Request body larger than `AUDIOFP_MAX_UPLOAD_MB`, or a JSON body over 1 MB on a JSON endpoint (`JSON body too large (max 1024 KB)`). | [413 payload too large](#413-payload-too-large). |
| 415 | `unsupported_format` | File extension is not in the supported list (checked at upload and again at decode). | [Unsupported format](#unsupported-format). |
| 422 | `ffmpeg_not_found` | The file needs ffmpeg and ffmpeg is not installed or not on PATH. | [ffmpeg not found](#ffmpeg-not-found). |
| 422 | `audio_decode_error` | libsndfile or ffmpeg rejected the file (corrupt, wrong extension, no audio stream, 0 bytes, decode timeout). | [Decode errors](#decode-errors). |
| 422 | `empty_audio` | The file decoded to zero samples. | File is empty or truncated; re-export it. |
| 422 | `audio_processing_error` | Generic fingerprinting failure (for example out of memory). | Check the server log under the request id. |
| 429 | `job_error` | `Too many jobs queued` - a folder-indexing request arrived while active jobs were at `4 x AUDIOFP_MAX_CONCURRENT_JOBS` (uploads are not subject to this check; they queue). | Wait, or cancel jobs. |
| 500 | `internal_error` | Unhandled exception. The traceback is in the server log under the request id. | Report it with the request id. |
| 500 | `configuration_error` | Invalid configuration (bad env value, `storage_type=postgres` without a DSN, ...). Normally fatal at startup, not an HTTP response. | Fix the value; `audiofp config` shows what was loaded. |
| 503 | `storage_error` | Storage backend failed: SQLite could not open the file (or `AUDIOFP_SQLITE_PATH=:memory:`, which is refused - use `AUDIOFP_STORAGE_TYPE=memory`), `database is locked` for longer than 30 s, a batched fingerprint write failed (`Failed to write N fingerprints`), PostgreSQL unreachable (`Could not connect to PostgreSQL (...)`, password redacted), schema newer than this AudioFP. | `audiofp doctor`; check disk, DSN, versions. |
| 503 | `fingerprint_incompatible` | Database built with different fingerprint parameters, or by AudioFP 1.x. | [`fingerprint_incompatible`](#fingerprint_incompatible), [1.x database](#database-was-created-by-audiofp-1x). |

Errors raised by Flask/Werkzeug rather than by AudioFP get a generic code
derived from the status: `bad_request`, `method_not_allowed`,
`unsupported_media_type`, `unprocessable`, `too_many_requests`,
`service_unavailable`. `conflict` (409) and `matching_error` (500) exist in the
exception hierarchy (`fingerprint/utils/exceptions.py`) but no 2.0 endpoint
raises them.

### Per-file job error codes

Found in `errors[].error_code` of a job (bounded by `AUDIOFP_JOB_MAX_ERRORS`,
default 500), in `result.errors` of a finished job, and in `errors[]` of
`audiofp index --json`:

| `error_code` | Meaning |
|---|---|
| `file_not_found` | The file disappeared after the folder was scanned. |
| `unsupported_format` | Extension not supported. Folder scans skip such files silently and uploads are rejected with a 415 before a job exists, so in practice this comes from `audiofp index <file>` with an explicit file argument, which is not pre-filtered. |
| `empty_fingerprint` | Decoded fine but produced no hashes: silence, an extremely short clip, or noise-only audio. |
| `empty_audio` | Decoded to zero samples (empty or truncated file). |
| `audio_decode_error`, `ffmpeg_not_found`, `audio_processing_error` | As in the table above. |
| `out_of_memory` | `MemoryError` while processing that one file outside the fingerprint pass (content hashing, storing). A `MemoryError` inside the fingerprint pass itself (decode, STFT, peaks, hashes - one pass, normalisation included) is reported as `audio_processing_error` with the message `Out of memory while fingerprinting '<name>'`. |
| `internal_error` | Unexpected exception; the traceback is logged as `Unexpected error while indexing <name>`. |

A `storage_error` in the middle of a run is different: the run stops instead
of failing every remaining file (files already in flight still finish, and
what was indexed is kept). The job then ends as `failed` with
`error: "Indexing stopped after N file(s): <storage message>"`;
`audiofp index` prints the same as `error (storage_error): ...` and exits
with status 1.

## ffmpeg not found

**Symptoms**

* `code: "ffmpeg_not_found"` (HTTP 422) from `/api/v1/search`, or the same
  `error_code` on files in a job.
* `audiofp doctor` prints `[warn] ffmpeg not found: ...`.
* `GET /api/v1/health` -> `"ffmpeg": {"available": false}`; the UI status pill
  says `no ffmpeg` and the format badges show `ffmpeg not installed`.
* Microphone search in Chrome/Edge is refused with "This browser records
  WebM/MP4, which the server can only decode with ffmpeg". Firefox records
  OGG/Opus, which needs no ffmpeg.

**Which files need ffmpeg** (`fingerprint/formats.py`, also served by
`GET /api/v1/info` -> `formats`):

| Group | Extensions | Decoder |
|---|---|---|
| `native_audio` | wav, wave, flac, ogg, oga, opus, mp3, aiff, aif, aifc, au, caf, w64 | libsndfile via `soundfile` - no external binary |
| `ffmpeg_audio` | m4a, aac, wma, amr, ac3, dts, mka, weba | ffmpeg |
| `video` | mp4, mkv, avi, mov, wmv, flv, webm, m4v, mpeg, mpg, ts, mts, 3gp, vob | ffmpeg (audio track extracted) |

**Fix**: install ffmpeg and make sure it is on `PATH`. The one-line hint
AudioFP appends to the error names exactly these commands:

```text
Windows:        winget install Gyan.FFmpeg
macOS:          brew install ffmpeg
Debian/Ubuntu:  sudo apt install ffmpeg
```

Then:

1. Open a **new** terminal (the PATH change does not reach existing shells)
   and check `ffmpeg -version`.
2. **Restart AudioFP.** The location is looked up once per process and cached
   (`ffmpeg_info()` in `fingerprint/core/decoder.py` is `lru_cache`d), so a
   running server keeps reporting "not found" until restarted.
3. `audiofp doctor` should now print `[ok] ffmpeg <version> at <path>`.

If ffmpeg is installed but not on the service's PATH, set
`AUDIOFP_FFMPEG_BINARY=/opt/ffmpeg/bin/ffmpeg` (or a Windows path). This
variable is read straight from the environment by the decoder module when it
is imported; it is **not** a `Settings` field, so `audiofp config` does not
list it.

If `GET /api/v1/info` shows `ffmpeg.available: false` together with a non-null
`ffmpeg.path`, the binary was found but `ffmpeg -version` did not exit with
0 (broken install, missing shared libraries). Run it by hand.

## Unsupported format

`code: "unsupported_format"` (HTTP 415). Format detection is **by extension**
only, lower-cased. Two related 400s: a file with no extension is rejected with
"The uploaded file has no extension, so its format cannot be determined", and
a folder with no supported files at all fails with "No supported audio or
video files were found in that directory" (`details.supported` lists what
would count).

* Rename the file to its real extension, or convert it:
  `ffmpeg -i input.xyz output.wav`.
* Folder scans skip unsupported files silently, along with hidden
  directories (names starting with `.`) and `.git`, `__pycache__`,
  `node_modules`, `.venv`, `venv`, `.idea`, `.vscode`, `$RECYCLE.BIN`,
  `System Volume Information`. Nothing about them appears in the job.
* A file whose extension lies (AAC data named `.mp3`) is not "unsupported":
  libsndfile fails to open it and, when ffmpeg is installed, the decoder
  retries through ffmpeg automatically. Without ffmpeg it becomes an
  `audio_decode_error` whose message adds "The file may be corrupted, or in a
  format that needs ffmpeg." followed by the install hint.

## Decode errors

`code: "audio_decode_error"` (HTTP 422) or the same per-file `error_code`.
The message tells you which backend failed:

| Message | Cause |
|---|---|
| `Could not decode 'x': <reason>` | libsndfile could not open the file. `<reason>` is libsndfile's own text (for example `Format not recognised`). |
| `'x' is empty (0 bytes)` | Zero-byte file. |
| `ffmpeg could not decode 'x': <stderr tail>` (`details.ffmpeg_exit_code`) | ffmpeg exited non-zero; the last 20 lines of its stderr are included. |
| `'x' contains no decodable audio stream` | A video container without an audio track, or ffmpeg produced no PCM. |
| `ffmpeg timed out after 3600s decoding 'x'` | ffmpeg was killed after the built-in 3600 s limit of `iter_audio_chunks`. Usually a hung or absurdly long input. |
| `'x' decoded to zero samples - is the file empty or truncated?` (`code: "empty_audio"`) | The decoder returned nothing. |
| `No fingerprint could be extracted (silent, extremely short or noise-only audio)` (`error_code: "empty_fingerprint"`, indexing only) | Audio decoded but yielded no peaks/hashes. |

What to try:

1. Decode it outside AudioFP: `ffmpeg -v error -i file.m4a -f null -`. If
   ffmpeg complains, the file is the problem.
2. Run with `--log-level DEBUG` (CLI) or `AUDIOFP_LOG_LEVEL=DEBUG` (server);
   the decoder logs the exact ffmpeg command line as `ffmpeg decode: ...`.
3. With `backend="auto"` (the default) the fallback from libsndfile to ffmpeg
   only happens when libsndfile fails **before producing any audio** (normally
   at open). A failure part-way through (truncated download) is reported
   as-is, because replaying through ffmpeg would duplicate the audio already
   fingerprinted.
4. Uploads that fail to index are deleted from `<data_dir>/uploads` unless
   `AUDIOFP_KEEP_FAILED_UPLOADS=true`; set it while you investigate.

## `fingerprint_incompatible`

```text
error (fingerprint_incompatible): The fingerprint database was built with different fingerprint parameters
(stored signature 7c1e0d2a9b4f6e31, current 91ab...). Matching would silently degrade. Either restore the
original AUDIOFP_* fingerprint settings, point AUDIOFP_SQLITE_PATH / AUDIOFP_POSTGRES_DSN at a new
database, or re-index everything with `audiofp db reset`.
```

**What it means.** Fingerprints depend on `sample_rate`, `n_fft`,
`hop_length`, `peak_neighborhood_size`, `min_amplitude`, `fan_value`,
`min_hash_time_delta`, `max_hash_time_delta` and the internal
`algorithm_version`. `Settings.fingerprint_signature()` hashes those (first
16 hex chars of a SHA-256) and the value is stamped into the database's meta
table (`fingerprint_signature`, `fingerprint_params`) the first time an empty
database is opened. On every later start the stored signature is compared to
the current one. Hashes computed with different parameters do not line up
with the stored ones, so a mismatch would not crash - searches would just
silently return fewer matches, or none. That is why the default
`fingerprint_compat=strict` refuses to start. `audiofp serve` - like every CLI command - prints
`error (fingerprint_incompatible): ...` followed by `details: {...}` and exits
with status 1; under an external WSGI server the exception is raised while
the app is created.

**Find out what changed.** `details.stored_params` is the JSON of the
parameters the database was built with (`audiofp db check` prints the stored
signature); compare with `audiofp config`, which prints the current values
and `_fingerprint_signature`. A forgotten `.env` or a different working
directory (hence a different `.env`) are the usual causes; profiles do not
change fingerprint parameters.

**Pick one:**

| Option | Command | Effect |
|---|---|---|
| Restore the original settings | fix `AUDIOFP_SAMPLE_RATE`, `AUDIOFP_N_FFT`, ... and restart | Keeps the library. |
| Use a new database next to the old one | `AUDIOFP_SQLITE_PATH=data/fingerprints-v2.db` (or `--sqlite-path`), or a new `AUDIOFP_POSTGRES_DSN` | Old data untouched; re-index into the new file. |
| Wipe and re-stamp | `audiofp db reset --yes` | Deletes every track and fingerprint, writes the current signature, runs `VACUUM` on SQLite. `--yes` skips the prompt. Then `audiofp index <folder>`. |
| Continue anyway | `AUDIOFP_FINGERPRINT_COMPAT=warn` or `ignore` | Only for experiments: matching against the old fingerprints silently degrades. |

Two details worth knowing:

* Deleting every track through the API or UI does **not** clear the stamp:
  track deletion only touches the `tracks` and `fingerprints` tables, and
  nothing except `audiofp db reset` rewrites the stored signature. Use
  `db reset` when you change parameters, even on an empty library.
* `db reset` opens the store with the compatibility check disabled, so it
  works on a mismatched database. It does not work on a 1.x database (next
  section).

## Database was created by AudioFP 1.x

```text
'data/fingerprints.db' was created by AudioFP 1.x. Its fingerprints use an incompatible hash layout and
cannot be upgraded in place. Point AUDIOFP_SQLITE_PATH at a new file (or delete the old one) and re-index
your library.
```

`SQLiteStore` recognises a 1.x file by `PRAGMA user_version = 0` plus a
`songs` table and no `meta` table, and raises `fingerprint_incompatible`
(`details.db_path`, `details.schema_version`) before reading anything else
from it. There is no
migration: the hash layout changed, so the stored fingerprints cannot be
reused. Options:

* `AUDIOFP_SQLITE_PATH=<new file>` (or `--sqlite-path`) and re-index, or
* stop the server, delete (or rename) the old file together with its `-wal`
  and `-shm` companions, and re-index.

`audiofp db reset` cannot help here: the error is raised while the store is
being opened, before `reset` gets a chance to clear anything. The detection is
SQLite-only; a 2.x PostgreSQL schema is created fresh with its own
`schema_version` meta row.

The reverse case, `'...' uses schema version N, newer than this AudioFP
supports (2). Upgrade AudioFP.`, is a `storage_error`: the file was written by
a newer release.

## 401 unauthorized

When `AUDIOFP_API_KEY` is set, every request whose path starts with `/api/`
must carry the key, except `GET /api/v1/health`, `GET /api/v1/openapi.json`
and CORS `OPTIONS` preflights. Three messages:

* `This server requires an API key. Send it as the X-API-Key header or as 'Authorization: Bearer <key>'.` - nothing was sent.
* `Invalid API key.` - something was sent and it does not match (constant-time comparison).
* `The stream token is invalid or has expired; request a new one from /stream-token.` - a `GET .../audio` or `.../play` request carried a `?token=` that is expired, forged, or minted for another track (see below).

```bash
curl -H "X-API-Key: $AUDIOFP_API_KEY"            http://localhost:5000/api/v1/stats
curl -H "Authorization: Bearer $AUDIOFP_API_KEY"  http://localhost:5000/api/v1/stats
```

**How the web UI handles it.** The page itself (`/`, `/docs`, `/static/*`)
is always served; only the API calls behind it need the key, so on a locked
server the UI loads and every panel then fails. The UI:

* stores the key in the browser's `localStorage` under `audiofp:apiKey`
  (JSON-encoded string, per browser profile, never sent anywhere except this
  origin);
* sends it as `X-API-Key` on every `fetch`/upload;
* for `<audio src>` URLs, which cannot carry headers, first calls
  `GET /api/v1/tracks/{id}/stream-token` (with the key) and plays the `url`
  it returns, `.../audio?token=<token>`. The token is HMAC-signed with the
  API key, valid for one hour (`STREAM_TOKEN_TTL` in `fingerprint/api/auth.py`)
  and only for that track; the server accepts it only on `GET .../audio` and
  `.../play`. The key itself is never put in a URL - `?api_key=` is ignored
  everywhere and gets a 401;
* on any 401 opens a password prompt ("API key required"), saves the value
  and reloads. The status pill reads `API key needed` until then. The
  **Access** panel in the Settings view has **Save** and **Forget** buttons
  for the same value.

Checklist when the key "does not work":

1. Supplied keys are trimmed of surrounding whitespace; the configured value
   is compared verbatim. A trailing space or newline in `AUDIOFP_API_KEY`
   (easy to do in a systemd unit or a Windows service definition) makes every
   key `Invalid API key.`. `audiofp config` only shows `***`, so check the
   environment itself.
2. Clear a stale key: Settings -> Access -> Forget, or in the browser console
   `localStorage.removeItem("audiofp:apiKey")`.
3. If a reverse proxy strips `Authorization`, use `X-API-Key` instead.
4. Audio URLs carry a stream token (never the key) as a query string, so it
   can land in reverse-proxy access logs and browser history; a leaked token
   opens one track for at most an hour. AudioFP's own access log masks the
   `token` and `api_key` query values as `***`; `AUDIOFP_ACCESS_LOG=false`
   disables that log entirely.

## 403 when indexing a folder

`POST /api/v1/tracks/index-directory` (UI: the **Index a folder** panel in
the Library view) is gated by `Runtime.check_directory_allowed()` and can
answer:

| HTTP | Message | Fix |
|---|---|---|
| 403 | `Directory indexing is disabled on this server (AUDIOFP_ALLOW_DIRECTORY_INDEXING=false).` | Set `AUDIOFP_ALLOW_DIRECTORY_INDEXING=true` (the default) and restart. |
| 403 | `That directory is outside the folders this server may index.` (`details.allowed_roots`) | Index a path inside one of `AUDIOFP_INDEX_ROOTS`, or add the folder to the list. |
| 403 | `In production, directory indexing requires AUDIOFP_INDEX_ROOTS to list the folders the server may read.` | The production rule: with `AUDIOFP_PROFILE=production` and no roots configured, folder indexing is refused outright. Set `AUDIOFP_INDEX_ROOTS`. |
| 400 | `'X' is not a directory on the server` | The path must exist on the machine running AudioFP, not on the browser's machine. Checked only after the 403 rules above pass, so a forbidden path is never told whether it exists. |
| 400 | `No supported audio or video files were found in that directory.` | Nothing with a supported extension below that folder (recursion is on unless `recursive: false`). |
| 429 | `Too many jobs queued; wait for running jobs to finish.` | Active jobs reached `4 x AUDIOFP_MAX_CONCURRENT_JOBS`. |

`AUDIOFP_INDEX_ROOTS` is comma-separated, for example
`AUDIOFP_INDEX_ROOTS=/srv/calls,/srv/jingles` or
`AUDIOFP_INDEX_ROOTS=D:\calls,E:\jingles`. Both the roots and the requested
directory are resolved (symlinks and junctions followed) before comparison,
so a symlink that points outside a root is rejected even though its path
looks fine. `~` is expanded in the requested `directory_path` only; roots are
taken literally, so write them out in full.

In development with no roots configured any path is allowed - which is why
the `production` profile insists on an explicit list. The UI reads
`GET /api/v1/info` -> `features.directory_indexing` and `features.index_roots`
and greys out the folder panel when indexing is not available, so a disabled
panel is configuration, not a bug. The CLI (`audiofp index <folder>`) is not
subject to any of this: it runs on the server with the server's permissions.

## 413 payload too large

```json
{"error": "Upload is too large. The server accepts at most 2048 MB per request (AUDIOFP_MAX_UPLOAD_MB).", "code": "payload_too_large", "status": 413}
```

Flask's `MAX_CONTENT_LENGTH` is `AUDIOFP_MAX_UPLOAD_MB` (default 2048)
megabytes and applies to `/search` as well as `/tracks`. Raise it and restart.
The UI compares files against `GET /api/v1/info` -> `limits.max_upload_mb`
before uploading and marks oversize files `larger than the N MB limit`
without sending them.

The JSON endpoints (`PATCH`/`PUT /tracks/{id}`, `POST /tracks/index-directory`,
`POST /tracks/bulk-delete`, `PUT`/`PATCH /settings`) have a separate, fixed
1 MiB cap that no setting raises: `JSON body too large (max 1024 KB)`. Split a
very large `metadata` object or a bulk-delete list (at most 1000 ids anyway)
instead of touching the upload limit.

If you get a 413 that is **not** JSON, it did not come from Flask:

* `audiofp serve` also passes the same limit to waitress
  (`max_request_body_size`), which can reject a body before Flask sees it;
* a reverse proxy in front (nginx `client_max_body_size`, for example) has
  its own limit - see `docs/DEPLOYMENT.md`.

For search you rarely need big files: a few seconds identifies a clip, and
only the first `AUDIOFP_MAX_QUERY_SECONDS` (default 3600) of a query are
analysed anyway (`query.truncated: true` in the response when that happens).

## "No match found" checklist

A search that returns `found: false` is not an error. The response carries
what was actually used:

```json
{"found": false, "mode": "identify",
 "query": {"duration_sec": 4.2, "num_peaks": 812, "num_hashes": 3950, "truncated": false},
 "thresholds": {"min_confidence": 0.02, "min_aligned_hashes": 10, "min_peak_ratio": 12.0, "top_k": 5},
 "diagnostics": {"query_hashes": 3950, "db_rows": 412, "votes": 430, "candidate_tracks": 57, "scored_tracks": 0,
                 "skipped_common_hashes": 0, "dropped_for_vote_cap": 0}}
```

Work through these in order:

1. **Is the track really in the library?** `audiofp tracks list --q "<name>"`
   or the Library search. A file skipped as a duplicate of something else was
   not indexed twice, but the earlier copy is searchable.
2. **Did the query produce hashes?** `query.num_hashes` near 0 means silence,
   a sub-second clip or pure noise - nothing to match. Use a longer, cleaner
   clip; 5-10 s helps with noisy audio. All audio is resampled to
   `sample_rate` (11025 Hz by default), so content above ~5.5 kHz does not
   contribute.
3. **Which thresholds applied?** Defaults are `min_confidence` 0.02,
   `min_aligned_hashes` 10, `min_peak_ratio` 12.0, but three things override
   them:
   * `PUT /api/v1/settings` (UI: **Server search defaults** in the Settings
     view) persists to `<data_dir>/runtime-settings.json` and overrides the
     env values for the API **and** the CLI. `audiofp config` shows the env
     values; `GET /api/v1/settings` shows the effective ones.
   * The UI's **Thresholds** panel on the Search view keeps per-browser
     values in `localStorage` (`audiofp:searchOptions`) and sends them with
     every search. **Reset to server defaults** restores the server values.
   * Per-request form fields `min_confidence`, `min_aligned_hashes`,
     `min_peak_ratio`, `top_k`, `mode` (CLI: `--min-confidence`,
     `--min-aligned`, `--min-peak-ratio`, `--top-k`, `--mode`).
   To see weak candidates, lower them for one search:
   `audiofp search clip.wav --min-confidence 0.005 --min-peak-ratio 5 --json`.
   Results are labelled `strong` (confidence >= 0.15 and peak ratio >= 30),
   `likely` (>= 0.05 and >= 18) or `weak`; a lone `weak` hit at very low
   thresholds is probably chance.
4. **Right mode?** `identify` returns the best alignment per track.
   `occurrences` returns every alignment above the thresholds, and offsets
   may be negative (the indexed track's content found *inside* the query).
   For "find every place this jingle appears in these calls", index the
   short jingle and search with the long recording, `--mode occurrences`.
5. **Was the query truncated?** `query.truncated: true` means only the first
   `AUDIOFP_MAX_QUERY_SECONDS` were analysed; the match may lie beyond.
6. **What does `diagnostics` say?** `db_rows: 0` means no query hash exists in
   the library at all (wrong library, different fingerprint parameters, or
   audio that never overlaps). `candidate_tracks` with `scored_tracks: 0`
   means no track came within reach of `min_aligned_hashes` even before
   scoring. A large `skipped_common_hashes` means the query consists of hashes
   the library repeats more than `AUDIOFP_MAX_ROWS_PER_HASH` (2000) times -
   hold music, tones, loops - which are skipped as stop words; a non-zero
   `dropped_for_vote_cap` (the log also says `Search vote cap hit`) means
   `AUDIOFP_MAX_SEARCH_VOTES` bit. Raise those two only for libraries that
   really consist of such material (see `docs/TUNING.md`).
7. **Same fingerprint parameters?** If you run with
   `AUDIOFP_FINGERPRINT_COMPAT=warn` or `ignore`, a parameter mismatch shows
   up exactly as "nothing ever matches". Check the log for the warning, or run
   `audiofp db check` and compare the signature with `audiofp config`.
8. **Was it re-encoded heavily?** Fingerprints survive MP3/AAC compression,
   resampling and moderate noise, but not speed/pitch changes.

`audiofp search` exits with status 1 when nothing is found, so scripts can
branch on it.

## Duplicates being skipped

Indexing reports `duplicate` outcomes (UI: `Already in the library
(duplicate)`; job counter `skipped`; `duplicates_of[]` in the summary with the
existing `track_id`). Behaviour follows `AUDIOFP_DEDUPE`:

| Mode | Duplicate means | Notes |
|---|---|---|
| `content` (default) | Same SHA-256 of the file bytes as an existing track. | Byte-identical only: a re-encoded or trimmed copy is *not* a duplicate and will be indexed as a second track. Every file is hashed before decoding, so re-running a folder is cheap. |
| `path` | Same absolute path as an existing track. | Uploads are saved as `<8 hex>_<safe name>` in the upload folder, so two uploads never share a path: `path` mode never dedupes uploads. |
| `none` | Nothing is skipped. | Every file becomes a new track; identical tracks then all match with identical scores. |

Changing `AUDIOFP_DEDUPE` needs a restart. Duplicate uploads are removed from
`<data_dir>/uploads` right away (the existing track keeps its file).

To re-index a file on purpose (new parameters, corrected metadata), delete the
track first - `DELETE /api/v1/tracks/{id}`, the Library view, or
`audiofp tracks delete <id> --yes` - then index it again.

## Jobs stuck, or `interrupted` after a restart

Job statuses are `pending`, `running`, `completed`, `failed`, `cancelled`
and `interrupted`. `GET /api/v1/jobs?status=active` lists pending+running;
`GET /api/v1/jobs/{id}` includes the error list; the **Activity** view polls
every 1.5 s while something is active.

**`interrupted`.** Jobs are written to `<data_dir>/jobs/<job_id>.json`
(`AUDIOFP_PERSIST_JOBS`, default true; the `testing` profile turns it off).
When the server starts, any record still `pending` or `running` is marked
`interrupted` with the error:

> The server restarted before this job finished. Files already indexed were kept; re-run to index the rest.

That is exactly what to do: submit the same folder again. With the default
`content` dedupe the already-indexed files are skipped as duplicates and only
the remainder is fingerprinted. Progress in the file is written at most every
2 s, so the counters of an interrupted job may lag the truth slightly.

With SQLite, "already indexed" means "already flushed to disk": fingerprints
are buffered in memory (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`, default 2,000,000
rows) and written at the end of each run, when the buffer fills, and on
shutdown (`Ctrl+C`, `systemctl stop` and `docker stop` all flush). Tracks whose
rows were still in the buffer when the process was killed outright are removed
the next time the store opens, with the warning
`Removed N track(s) whose fingerprints were lost in an unclean shutdown (...);
re-index them.` - re-running the folder picks them up again because they are
no longer duplicates. Set `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0` to write every
track immediately if that trade-off is wrong for you.

**Stuck at `pending`.** Only `AUDIOFP_MAX_CONCURRENT_JOBS` (default 2) jobs
run at once; uploads and folder jobs share the pool. A third job waits. When
the active count reaches four times that number, new **folder-indexing**
requests get a 429; uploads are still accepted and queue behind the others.

**`running` but not moving.** Look at `current_item`. One very long or slow
file (an hour-long video through ffmpeg, a file on a slow network share)
holds a worker; `completed` only advances per finished file. `elapsed_sec`
and `eta_sec` are estimates from the rate so far. ffmpeg is killed after
3600 s per file, after which the file fails with `audio_decode_error` and the
job moves on. `AUDIOFP_INDEX_WORKERS` (0 = `min(4, CPU count)`) sets how many
files are fingerprinted in parallel.

**Cancel does not stop immediately.** `POST /api/v1/jobs/{id}/cancel` is
cooperative: files already handed to workers (up to two per worker) finish,
then the job ends as `cancelled`. `DELETE /api/v1/jobs/{id}` only works on
finished jobs (`Cannot remove a job that is still running; cancel it first`).

**Jobs vanish or 404 intermittently.** A job lives in the memory of the
process that accepted it and is only loaded from disk at startup. Behind a
multi-process WSGI setup (several gunicorn workers) a `GET /jobs/{id}` may
land on a process that never saw the job. Run one process with threads
(waitress, the default outside development, or `gunicorn --workers 1
--threads N`) - see `docs/DEPLOYMENT.md`.

**`database is locked`.** SQLite writers inside one process queue on a lock;
across processes (running `audiofp index` against the same file while the
server is indexing) they wait up to 30 s, then the write fails with a
`storage_error` and the run stops: the job ends as `failed` with
`Indexing stopped after N file(s): ... database is locked`, keeping what was
indexed. Let one writer finish, or index through the running server instead.

Finished jobs beyond `AUDIOFP_JOB_HISTORY_LIMIT` (default 200) are pruned,
oldest first, together with their files.

## Playback fails but search works

`GET /api/v1/tracks/{id}/audio` (alias `/play`) streams the original file
from the `filepath` recorded at index time. When that file is gone you get a
404:

```json
{"error": "The original file for this track is no longer on disk. The fingerprint still works for searching.", "code": "file_missing", "status": 404, "details": {"track_id": "..."}}
```

`GET /api/v1/tracks/{id}` reports `file_exists` for exactly this; the UI
shows "The original file is no longer on disk. Searching still works;
playback does not." in the track drawer and toasts "Could not play this
track" from the player. Search is unaffected: the fingerprints are in the
database, the audio is not.

Why it happens:

* **The library folder was moved or renamed.** `filepath` is absolute. There
  is no relocate command. Re-indexing from the new location does **not**
  update paths either: with `content` dedupe the moved files are byte-identical
  to the existing tracks and are reported as duplicates. Delete the affected
  tracks (`POST /api/v1/tracks/bulk-delete`, or select them in the Library)
  and index the new location.
* **An upload was deleted.** `DELETE /api/v1/tracks/{id}?delete_file=true`
  and the UI's "Delete track + file" remove the file, but only when it lives
  inside `<data_dir>/uploads`; files indexed from folders are never deleted by
  AudioFP. Failed uploads are removed automatically unless
  `AUDIOFP_KEEP_FAILED_UPLOADS=true`.
* **A different `AUDIOFP_UPLOAD_DIR` or `AUDIOFP_DATA_DIR`** than the one
  the files were uploaded under.

Other playback problems:

* Playback works in curl but not in the browser on a locked server: the
  `<audio>` element cannot send the key, so it needs a stream token -
  `GET /api/v1/tracks/{id}/stream-token` with the key returns a `url` of the
  form `.../audio?token=<token>` (valid one hour, this track only). The
  bundled UI does this; a custom front-end must too. `?api_key=` in the URL
  is not accepted.
* Seeking does not work through a proxy: the endpoint answers HTTP Range
  requests (`206`), which the proxy must pass through.
* Video tracks are streamed with their container's MIME type (`video/mp4`,
  `video/x-matroska`, ...) and the UI plays them through an `<audio>`
  element. Whether that works depends on the browser's codec support, not on
  AudioFP; the fingerprint side is unaffected.

## Microphone recording does nothing

The **Record** button on the Search view (it toggles: press once to start,
again to stop) uses the browser's `MediaRecorder`, which browsers only expose
in a *secure context*. Opening the UI as `http://<LAN address>:5000` therefore
shows "Microphone recording needs a secure context: open the app via https://
or http://localhost." and nothing is recorded; use `http://localhost:5000` on
the same machine or put the server behind TLS (see `docs/DEPLOYMENT.md`). The
same rule affects the copy buttons (track ids, request ids): on plain `http`
the clipboard API is unavailable and they fall back to `execCommand("copy")`,
showing "Copy failed - select the text manually" if the browser refuses that
too. Chrome/Edge record WebM/MP4, which the server can only decode with ffmpeg
(see [ffmpeg not found](#ffmpeg-not-found)); Firefox records OGG/Opus.

## Windows notes

* **Command not found.** `pip install -e .` puts `audiofp.exe` in the
  environment's `Scripts` folder, which is often not on PATH.
  `python -m fingerprint <command>` is identical. `python run.py` still
  starts the server (`--env production` is translated to
  `--profile production`).
* **ffmpeg.** `winget install Gyan.FFmpeg`, then open a new terminal, check
  `where ffmpeg`, and restart AudioFP (the lookup is cached per process). For
  a service account with a different PATH, set `AUDIOFP_FFMPEG_BINARY` to the
  full path of `ffmpeg.exe`. ffmpeg is launched with `CREATE_NO_WINDOW`, so
  no console windows flash when the server runs as a service.
* **Long paths.** AudioFP does not add the `\\?\` prefix. Paths longer than
  260 characters typically surface as files missing from a folder scan,
  `file_not_found`, or `Could not decode ...: System error`, unless Windows
  long paths are enabled (`LongPathsEnabled = 1` under
  `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`, or the "Enable Win32
  long paths" group policy), which Python honours (not verified against this
  code base - Windows/Python behaviour).
* **Production server.** `audiofp serve --profile production` (or
  `--server waitress`) uses waitress, which is a core dependency and runs
  natively on Windows; `--threads` / `AUDIOFP_SERVER_THREADS` (default 8) set
  its worker threads. The `gunicorn` extra is for Linux/macOS only. In the
  `development` profile `--server auto` picks Flask's built-in server.
* **Paths in configuration.** Backslashes are fine:
  `AUDIOFP_INDEX_ROOTS=D:\calls,E:\jingles`, `AUDIOFP_SQLITE_PATH=D:\audiofp\fingerprints.db`.
  The `.env` reader accepts quoted values. Commas separate list entries, so a
  root path cannot itself contain a comma.
* **SQLite on network shares.** Keep `AUDIOFP_SQLITE_PATH` on a local disk.
  The database runs in WAL mode, which is not safe on SMB/NFS shares. Indexing
  *from* a share is fine, if slow.
* **Antivirus.** Real-time scanners that briefly lock files can show up as
  sporadic `Could not decode ...` failures on otherwise fine files during
  large runs (not verified - operational experience, nothing in AudioFP
  retries a locked file). Re-run the folder (already-indexed files are skipped as
  duplicates) or exclude the data directory and library folders from
  scanning.

## Logs and request ids

**Where.** Everything is logged under the `fingerprint` logger namespace:

| Destination | When |
|---|---|
| Console (stderr) | Always. |
| `AUDIOFP_LOG_FILE` (rotating: `AUDIOFP_LOG_MAX_MB` x `AUDIOFP_LOG_BACKUP_COUNT`, defaults 20 MB x 5) | When set to a path, and only for `audiofp serve` / the WSGI app. Other CLI commands log to the console only. `none` (or an empty value) means console only. If the file cannot be created, a `Cannot write the log file ...; logging to the console only` warning is printed and the server keeps running. |
| `<data_dir>/logs/audiofp.log` | What the default `AUDIOFP_LOG_FILE=auto` resolves to in the `production` profile (console only in the other profiles). It follows `AUDIOFP_DATA_DIR`. `audiofp config` prints the raw value (`auto`), not the resolved path. |

The access log (`AUDIOFP_ACCESS_LOG`, default true) writes one INFO line per
`/api/` request: `METHOD /path?query -> status (N ms)`. The query string is
included, but the values of `token` and `api_key` parameters are replaced
with `***` before the line is written.

**Reading a request id.** Each request gets an id: the inbound
`X-Request-ID` header if you send one (first 64 characters, and only if those
consist of letters, digits, `.`, `_`, `:` and `-`), otherwise 12 random hex
characters. It is returned as the `X-Request-ID` response header,
placed in the `request_id` field of every error body, shown by the UI as
`request <id>` under error toasts and search errors, and injected into every
log line written while handling the request. In the text format
(`%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s`) it is
the bracketed token:

```text
2026-09-17 10:42:07 INFO    fingerprint.api.errors [3f9c2a1b7d4e] ffmpeg_not_found: 'call.m4a' (.m4a) requires ffmpeg, which is not installed. ...
2026-09-17 10:42:07 INFO    fingerprint.api.app [3f9c2a1b7d4e] POST /api/v1/search -> 422 (14 ms)
```

So: take the id from the response (or the toast), then

```bash
grep 3f9c2a1b7d4e data/logs/audiofp.log
```

```powershell
Select-String 3f9c2a1b7d4e data\logs\audiofp.log
```

Client errors (4xx) are logged at INFO as `<code>: <message>`; 5xx at ERROR,
and `internal_error` includes the traceback. Lines with `[-]` were written
outside a request - startup, and background jobs. Job lines carry the job id
in the message instead (`Job <id> queued: ...`, `Job <id> completed in ...`,
`Job <id> failed` with traceback) and each file logs `Indexed <name>: ...`,
`Failed to index <name>: ...` or `Skipping <name>: identical content already
indexed as <track_id>`.

Send your own id (`X-Request-ID: order-7781`) from a calling system to
correlate both sides. With `AUDIOFP_TRUST_PROXY=true` the `X-Forwarded-*`
headers from a reverse proxy are honoured as well.

## DEBUG and JSON logs

```bash
AUDIOFP_LOG_LEVEL=DEBUG audiofp serve          # server
audiofp search clip.wav --log-level DEBUG      # any CLI command accepts --log-level
```

The `development` profile already runs at DEBUG. DEBUG additionally logs the
exact ffmpeg command line for every decode and un-quiets the `werkzeug`,
`waitress` and `urllib3` loggers (they are held at WARNING otherwise).
`--quiet` on `audiofp index`, `search`, `tracks` and `stats` drops to WARNING
(`serve` keeps `AUDIOFP_LOG_LEVEL`; `doctor`, `config` and `db` do not
configure logging at all).

```bash
AUDIOFP_LOG_FORMAT=json audiofp serve
```

emits one JSON object per line - `ts`, `level`, `logger`, `message`, plus
`request_id` and `job_id` when set and `exception` for tracebacks - which is
what to use with Loki, Datadog, CloudWatch and friends:

```json
{"ts": "2026-09-17T10:42:07.123Z", "level": "INFO", "logger": "fingerprint.api.app", "message": "POST /api/v1/search -> 422 (14 ms)", "request_id": "3f9c2a1b7d4e"}
```

(`job_id` only appears in the JSON format; the text format prints the request
id alone.) If you embed the app in your own WSGI process and configure
logging yourself, call `create_app(configure_logs=False)`.

## Resetting everything

Everything AudioFP writes lives under `AUDIOFP_DATA_DIR` (default `./data`,
relative to the working directory; `--data-dir` on the CLI):

| Path | Contents |
|---|---|
| `fingerprints.db` (+ `-wal`, `-shm`) | The SQLite library (`AUDIOFP_SQLITE_PATH` overrides the location). |
| `uploads/` | Files uploaded through the API/UI (`AUDIOFP_UPLOAD_DIR` overrides). |
| `jobs/` | One JSON file per job. |
| `runtime-settings.json` | Search defaults saved through `PUT /api/v1/settings`. |
| `logs/` | `audiofp.log` and its rotated copies: where the `production` profile logs by default (`AUDIOFP_LOG_FILE=auto` - see [Logs and request ids](#logs-and-request-ids)). |

**Database only** (keeps uploads, job history and runtime settings):

```bash
audiofp db reset --yes      # wipes tracks + fingerprints, re-stamps the current fingerprint signature, VACUUMs SQLite
```

Works for PostgreSQL too (tables are cleared, not dropped).

**Everything:** stop the server first - SQLite in WAL mode keeps state in the
`-wal` file until the last connection closes - then delete the data
directory:

```bash
rm -rf data
```

```powershell
Remove-Item -Recurse -Force data
```

The next `audiofp serve` or `audiofp index` recreates the directory, an empty
database stamped with the current fingerprint signature, and empty `uploads/`
and `jobs/` folders. Deleting `runtime-settings.json` is what resets search
thresholds you changed in the UI; `AUDIOFP_*` variables and `.env` are of
course untouched.

To reclaim disk space without deleting anything, `audiofp db vacuum`
checkpoints the WAL and runs `VACUUM` (SQLite only).
