# Troubleshooting

This guide is organised by symptom. Each section gives the error `code`, log
line or UI message you'll see and what to change. Settings are the fields of
`Settings` in `fingerprint/config.py`. Every one of them is also an
environment variable `AUDIOFP_<FIELD_UPPER>`, listed in
`docs/CONFIGURATION.md`. Endpoints live under `/api/v1` and are described in
`docs/API.md`.

## Start with `audiofp doctor`

Run this before anything else:

```bash
audiofp doctor            # or: python -m fingerprint doctor
```

It loads settings the same way the server does: profile, `.env`, environment
variables, and `--profile`, `--data-dir`, `--storage` or `--sqlite-path` if
you pass them. Then it prints one line per check in five groups, Runtime,
Dependencies, ffmpeg, Configuration and Storage:

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

The output above is shortened. A `[FAIL]` line makes the command exit with
status 1, a `[warn]` line does not. The checks that matter most:

| Line | What it means |
|---|---|
| `libsndfile lacks MP3, ... support; those files will need ffmpeg` | Your `soundfile` wheel bundles an old libsndfile. Upgrade `soundfile` (>= 0.12 bundles MP3 support) or install ffmpeg. |
| `waitress not installed` | `audiofp serve --profile production` falls back to Flask's development server. `pip install waitress`. |
| `production profile without AUDIOFP_API_KEY` | Anyone who can reach the port can use the API. Set `AUDIOFP_API_KEY`. |
| `directory indexing is effectively disabled in production until AUDIOFP_INDEX_ROOTS is set` | See [403 when indexing a folder](#403-when-indexing-a-folder). |
| `data dir not writable` / `upload dir not writable` | Fix permissions, or point `AUDIOFP_DATA_DIR` / `AUDIOFP_UPLOAD_DIR` elsewhere. |
| `fingerprint_incompatible: ...` under Storage | See [`fingerprint_incompatible`](#fingerprint_incompatible). |
| `storage_error: ...` under Storage | The SQLite file cannot be opened, PostgreSQL is unreachable, or the schema is newer than this version. The message says which. |

Three more commands show what the server is using:

```bash
audiofp config            # effective settings (secrets shown as ***), plus _fingerprint_signature
audiofp config --json
audiofp db check          # opens the database and prints track/hash counts and the stored signature
```

Settings come from three layers: field defaults, then the profile
(`AUDIOFP_PROFILE` or `--profile`), then `AUDIOFP_*` environment variables.
A `.env` file in the working directory is read too, or the file named by
`AUDIOFP_ENV_FILE`. So when a setting seems to be ignored, the usual cause is
a different working directory or a `.env` you forgot about. `audiofp config`
shows the result of all three layers.

On a running server, `GET /api/v1/health` needs no API key and reports
`status`, `storage.ok`, `ffmpeg.available` and `jobs.active`. It returns
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

`code` is stable and meant for branching in clients. `error` is for humans
and `details` is optional. `request_id` is also sent as the `X-Request-ID`
response header and appears in every log line written while handling that
request, see [Logs and request ids](#logs-and-request-ids).

Where a decode problem shows up depends on the endpoint:

* `POST /api/v1/search` decodes the clip synchronously, so decode problems
  come back as HTTP errors (`ffmpeg_not_found`, `audio_decode_error`, ...).
* `POST /api/v1/tracks` (upload) and `POST /api/v1/tracks/index-directory`
  return 202 straight away with a `job_id`. Decode problems for those end up
  in the job, and the HTTP response says nothing about them.
  `GET /api/v1/jobs/{job_id}` has an `errors[]` list with a
  per-file `error_code`, and a single upload that failed has
  `result.status: "failed"`. The web UI shows the same thing in Activity.

### HTTP error codes

| HTTP | `code` | Meaning | What to do |
|---|---|---|---|
| 400 | `validation_error` | Bad input: no file in the `audio` field, a file without extension, an out-of-range number, an unknown sort field, a `directory_path` that is not a directory on the server, an empty `PATCH` body, ... | Read `error`. `details.field` names the offending field. |
| 400 | `job_error` | Cancelling a job that already finished, removing a job that is still running, or submitting while the server is shutting down. | Check `GET /api/v1/jobs/{id}` first, and cancel before removing. |
| 401 | `unauthorized` | Missing or wrong API key. | [401 unauthorized](#401-unauthorized). |
| 403 | `forbidden` | Folder indexing not allowed by configuration. | [403 when indexing a folder](#403-when-indexing-a-folder). |
| 404 | `not_found` | Unknown track id, job id or route. | `details.track_id` / `details.job_id` echo what you asked for. |
| 404 | `file_not_found` | The decoder was given a path that does not exist. Uploads are saved to disk first, so over HTTP you will hardly ever see this. It turns up as a per-file job code. | Re-run the index. |
| 404 | `file_missing` | The track exists but its original file is gone, so it cannot be streamed. | [Playback fails](#playback-fails-but-search-works). |
| 411 | `length_required` | A JSON body was sent to a JSON endpoint with `Transfer-Encoding: chunked` and no `Content-Length`, so its size cannot be checked before reading it. | Send `Content-Length`. Most HTTP clients do unless the body is streamed. |
| 413 | `payload_too_large` | Request body larger than `AUDIOFP_MAX_UPLOAD_MB`, or a JSON body over 1 MB on a JSON endpoint (`JSON body too large (max 1024 KB)`). | [413 payload too large](#413-payload-too-large). |
| 415 | `unsupported_format` | File extension is not in the supported list (checked at upload and again at decode). | [Unsupported format](#unsupported-format). |
| 422 | `ffmpeg_not_found` | The file needs ffmpeg and ffmpeg is not installed or not on PATH. | [ffmpeg not found](#ffmpeg-not-found). |
| 422 | `audio_decode_error` | libsndfile or ffmpeg rejected the file (corrupt, wrong extension, no audio stream, 0 bytes, decode timeout). | [Decode errors](#decode-errors). |
| 422 | `empty_audio` | The file decoded to zero samples. | The file is empty or truncated. Re-export it. |
| 422 | `audio_processing_error` | Generic fingerprinting failure (for example out of memory). | Check the server log under the request id. |
| 429 | `job_error` | `Too many jobs queued`: a folder-indexing request arrived while active jobs were at `4 x AUDIOFP_MAX_CONCURRENT_JOBS`. Uploads skip this check and just queue. | Wait, or cancel jobs. |
| 500 | `internal_error` | Unhandled exception. The traceback is in the server log under the request id. | Report it with the request id. |
| 500 | `configuration_error` | Invalid configuration (bad env value, `storage_type=postgres` without a DSN, ...). Normally this is fatal at startup and you never see it as an HTTP response. | Fix the value. `audiofp config` shows what was loaded. |
| 503 | `storage_error` | The storage backend failed, and the message says how. SQLite could not open the file. `AUDIOFP_SQLITE_PATH=:memory:` was set, which is refused, so use `AUDIOFP_STORAGE_TYPE=memory`. The database stayed locked (`database is locked`) for longer than 30 s. A batched fingerprint write failed with `Failed to write N fingerprints`. PostgreSQL is unreachable, reported as `Could not connect to PostgreSQL (...)` with the password redacted. The schema is newer than this AudioFP. | Run `audiofp doctor`, then check disk, DSN and versions. |
| 503 | `fingerprint_incompatible` | Database built with different fingerprint parameters, or by AudioFP 1.x. | [`fingerprint_incompatible`](#fingerprint_incompatible), [1.x database](#database-was-created-by-audiofp-1x). |

Errors that come from Flask or Werkzeug themselves get a generic code derived
from the status: `bad_request`, `method_not_allowed`,
`unsupported_media_type`, `unprocessable`, `too_many_requests`,
`service_unavailable`. `conflict` for 409 and `matching_error` for 500 exist
in the exception hierarchy in `fingerprint/utils/exceptions.py`, but no 2.0
endpoint raises them.

### Per-file job error codes

These appear in `errors[].error_code` of a job (the list is capped at
`AUDIOFP_JOB_MAX_ERRORS`, default 500), in `result.errors` of a finished job,
and in `errors[]` of `audiofp index --json`:

| `error_code` | Meaning |
|---|---|
| `file_not_found` | The file disappeared after the folder was scanned. |
| `unsupported_format` | Extension not supported. Folder scans skip such files silently, and uploads are rejected with a 415 before a job exists. In practice this comes from `audiofp index <file>` with an explicit file argument, which is not pre-filtered. |
| `empty_fingerprint` | Decoded fine but produced no hashes: silence, an extremely short clip, or noise-only audio. |
| `empty_audio` | Decoded to zero samples (empty or truncated file). |
| `audio_decode_error`, `ffmpeg_not_found`, `audio_processing_error` | As in the table above. |
| `out_of_memory` | `MemoryError` while processing that one file outside the fingerprint pass, so during content hashing or storing. A `MemoryError` inside the fingerprint pass itself (decode, STFT, peaks and hashes are one pass, normalisation included) is reported as `audio_processing_error` with the message `Out of memory while fingerprinting '<name>'`. |
| `internal_error` | Unexpected exception. The traceback is logged as `Unexpected error while indexing <name>`. |

A `storage_error` in the middle of a run is different: the whole run stops.
Files already in flight still finish and what was indexed is kept. The job
then ends as `failed` with
`error: "Indexing stopped after N file(s): <storage message>"`.
`audiofp index` prints the same as `error (storage_error): ...` and exits
with status 1.

## ffmpeg not found

You'll see one or more of these:

* `code: "ffmpeg_not_found"` (HTTP 422) from `/api/v1/search`, or the same
  `error_code` on files in a job.
* `audiofp doctor` prints `[warn] ffmpeg not found: ...`.
* `GET /api/v1/health` reports `"ffmpeg": {"available": false}`. The UI status
  pill says `no ffmpeg` and the format badges show `ffmpeg not installed`.
* Microphone search in Chrome or Edge is refused with "This browser records
  WebM or MP4. The server needs ffmpeg to decode those and it isn't
  installed." Firefox records OGG/Opus, which needs no ffmpeg.

Which files need ffmpeg is defined in `fingerprint/formats.py` and also
served by `GET /api/v1/info` under `formats`:

| Group | Extensions | Decoder |
|---|---|---|
| `native_audio` | wav, wave, flac, ogg, oga, opus, mp3, aiff, aif, aifc, au, caf, w64 | libsndfile via `soundfile`, no external binary |
| `ffmpeg_audio` | m4a, aac, wma, amr, ac3, dts, mka, weba | ffmpeg |
| `video` | mp4, mkv, avi, mov, wmv, flv, webm, m4v, mpeg, mpg, ts, mts, 3gp, vob | ffmpeg (audio track extracted) |

The fix is to install ffmpeg and make sure it is on `PATH`. The one-line
hint AudioFP appends to the error names these commands:

```text
Windows:        winget install Gyan.FFmpeg
macOS:          brew install ffmpeg
Debian/Ubuntu:  sudo apt install ffmpeg
```

Then:

1. Open a new terminal, because the PATH change does not reach existing
   shells, and check `ffmpeg -version`.
2. **Restart AudioFP.** The location is looked up once per process and
   cached (`ffmpeg_info()` in `fingerprint/core/decoder.py` is `lru_cache`d),
   so a running server keeps reporting "not found" until you restart it.
3. `audiofp doctor` should now print `[ok] ffmpeg <version> at <path>`.

If ffmpeg is installed but not on the service's PATH, set
`AUDIOFP_FFMPEG_BINARY=/opt/ffmpeg/bin/ffmpeg` (or a Windows path). The
decoder module reads this variable straight from the environment when it is
imported. It isn't a `Settings` field, so `audiofp config` does not list it.

If `GET /api/v1/info` shows `ffmpeg.available: false` together with a non-null
`ffmpeg.path`, the binary was found but `ffmpeg -version` did not exit
with 0. That points to a broken install or missing shared libraries. Run it
by hand to see why.

## Unsupported format

`code: "unsupported_format"` (HTTP 415). Format detection goes by the
extension only, lower-cased. There are two related 400s. A file with no
extension is rejected with "The uploaded file has no extension, so its
format cannot be determined". A folder with no supported files at all fails
with "No supported audio or video files were found in that directory", and
`details.supported` lists what would count.

* Rename the file to its real extension, or convert it:
  `ffmpeg -i input.xyz output.wav`.
* Folder scans skip unsupported files silently, along with hidden
  directories (names starting with `.`) and `.git`, `__pycache__`,
  `node_modules`, `.venv`, `venv`, `.idea`, `.vscode`, `$RECYCLE.BIN`,
  `System Volume Information`. Nothing about them appears in the job.
* A file whose extension lies (AAC data named `.mp3`) is not "unsupported".
  libsndfile fails to open it, and when ffmpeg is installed the decoder
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
| `ffmpeg could not decode 'x': <stderr tail>` (`details.ffmpeg_exit_code`) | ffmpeg exited non-zero. The last 20 lines of its stderr are included. |
| `'x' contains no decodable audio stream` | A video container without an audio track, or ffmpeg produced no PCM. |
| `ffmpeg timed out after 3600s decoding 'x'` | ffmpeg was killed after the built-in 3600 s limit of `iter_audio_chunks`. Usually a hung or absurdly long input. |
| `'x' decoded to zero samples. The file may be empty or truncated.` (`code: "empty_audio"`) | The decoder returned nothing. |
| `No fingerprint could be extracted (silent, extremely short or noise-only audio)` (`error_code: "empty_fingerprint"`, indexing only) | Audio decoded but yielded no peaks or hashes. |

What to try:

1. Decode it outside AudioFP: `ffmpeg -v error -i file.m4a -f null -`. If
   ffmpeg complains, the file is the problem.
2. Run the CLI with `--log-level DEBUG`, or the server with
   `AUDIOFP_LOG_LEVEL=DEBUG`. The decoder then logs the exact ffmpeg command
   line as `ffmpeg decode: ...`.
3. With `backend="auto"`, the default, the fallback from libsndfile to ffmpeg
   only happens when libsndfile fails before it has produced any audio, which
   normally means at open. A failure part-way through, say a truncated
   download, is reported as it is. Replaying through ffmpeg would duplicate
   the audio already fingerprinted.
4. Uploads that fail to index are deleted from `<data_dir>/uploads` unless
   `AUDIOFP_KEEP_FAILED_UPLOADS=true`. Set that while you investigate.

## `fingerprint_incompatible`

```text
error (fingerprint_incompatible): The fingerprint database was built with different fingerprint parameters
(stored signature 7c1e0d2a9b4f6e31, current 91ab...). Matching would silently degrade. Either restore the
original AUDIOFP_* fingerprint settings, point AUDIOFP_SQLITE_PATH / AUDIOFP_POSTGRES_DSN at a new
database, or re-index everything with `audiofp db reset`.
```

Fingerprints depend on `sample_rate`, `n_fft`, `hop_length`,
`peak_neighborhood_size`, `min_amplitude`, `fan_value`,
`min_hash_time_delta`, `max_hash_time_delta` and the internal
`algorithm_version`. `Settings.fingerprint_signature()` hashes those (first
16 hex chars of a SHA-256). The value is stamped into the database's meta
table as `fingerprint_signature` and `fingerprint_params` the first time an
empty database is opened, and on every later start the stored signature is
compared to the current one.

Hashes computed with different parameters do not line up with the stored
ones. A mismatch would not crash anything. Searches would just quietly
return fewer matches, or none. That is why the default
`fingerprint_compat=strict` refuses to start. `audiofp serve`, like every
CLI command, prints `error (fingerprint_incompatible): ...` followed by
`details: {...}` and exits with status 1. Under an external WSGI server the
exception is raised while the app is created.

To find out what changed, look at `details.stored_params`. It is the JSON of
the parameters the database was built with, and `audiofp db check` prints
the stored signature. Compare with `audiofp config`, which prints the current
values and `_fingerprint_signature`. The usual causes are a forgotten `.env`
or a different working directory, which means a different `.env`. Profiles
do not change fingerprint parameters.

Then pick one of these:

| Option | Command | Effect |
|---|---|---|
| Restore the original settings | fix `AUDIOFP_SAMPLE_RATE`, `AUDIOFP_N_FFT`, ... and restart | Keeps the library. |
| Use a new database next to the old one | `AUDIOFP_SQLITE_PATH=data/fingerprints-v2.db` (or `--sqlite-path`), or a new `AUDIOFP_POSTGRES_DSN` | Old data stays untouched. Re-index into the new file. |
| Wipe and re-stamp | `audiofp db reset --yes` | Deletes every track and fingerprint, writes the current signature, runs `VACUUM` on SQLite. `--yes` skips the prompt. Then `audiofp index <folder>`. |
| Continue anyway | `AUDIOFP_FINGERPRINT_COMPAT=warn` or `ignore` | Only for experiments: matching against the old fingerprints silently degrades. |

Two details that catch people out:

* Deleting every track through the API or UI does not clear the stamp. Track
  deletion only touches the `tracks` and `fingerprints` tables, and nothing
  except `audiofp db reset` rewrites the stored signature. So use `db reset`
  when you change parameters, even on an empty library.
* `db reset` opens the store with the compatibility check disabled, so it
  works on a mismatched database. It does not work on a 1.x database, see the
  next section.

## Database was created by AudioFP 1.x

```text
'data/fingerprints.db' was created by AudioFP 1.x. Its fingerprints use an incompatible hash layout and
cannot be upgraded in place. Point AUDIOFP_SQLITE_PATH at a new file (or delete the old one) and re-index
your library.
```

`SQLiteStore` recognises a 1.x file by `PRAGMA user_version = 0` plus a
`songs` table and no `meta` table. It raises `fingerprint_incompatible`
(`details.db_path`, `details.schema_version`) before reading anything else
from the file. There is no migration. The hash layout changed, so the stored
fingerprints cannot be reused. You can either

* set `AUDIOFP_SQLITE_PATH=<new file>` (or `--sqlite-path`) and re-index, or
* stop the server, delete or rename the old file together with its `-wal`
  and `-shm` companions, and re-index.

`audiofp db reset` cannot help here. The error is raised while the store is
being opened, before `reset` gets a chance to clear anything. The detection
is SQLite-only. A 2.x PostgreSQL schema is created fresh with its own
`schema_version` meta row.

The reverse case, `'...' uses schema version N, newer than this AudioFP
supports (2). Upgrade AudioFP.`, is a `storage_error`. The file was written
by a newer release.

## 401 unauthorized

When `AUDIOFP_API_KEY` is set, every request whose path starts with `/api/`
must carry the key, except `GET /api/v1/health`, `GET /api/v1/openapi.json`
and CORS `OPTIONS` preflights. There are three messages:

* `This server requires an API key. Send it as the X-API-Key header or as 'Authorization: Bearer <key>'.` means nothing was sent.
* `Invalid API key.` means something was sent and it does not match. The comparison is constant-time.
* `The stream token is invalid or has expired; request a new one from /stream-token.` means a `GET .../audio` or `.../play` request carried a `?token=` that is expired, forged, or minted for another track. More on tokens below.

```bash
curl -H "X-API-Key: $AUDIOFP_API_KEY"            http://localhost:5000/api/v1/stats
curl -H "Authorization: Bearer $AUDIOFP_API_KEY"  http://localhost:5000/api/v1/stats
```

The web UI page itself (`/`, `/docs`, `/static/*`) is always served. Only
the API calls behind it need the key, so on a locked server the UI loads and
every panel then fails. What the UI does with the key:

* The key is kept in the browser's `localStorage` under `audiofp:apiKey` as
  a JSON-encoded string, per browser profile, and never sent anywhere except
  this origin.
* It goes out as `X-API-Key` on every `fetch` and upload.
* `<audio src>` URLs cannot carry headers, so for those the UI first calls
  `GET /api/v1/tracks/{id}/stream-token` with the key and plays the `url` it
  returns, `.../audio?token=<token>`. The token is HMAC-signed with the API
  key, valid for one hour (`STREAM_TOKEN_TTL` in `fingerprint/api/auth.py`)
  and only for that track. The server accepts it only on `GET .../audio` and
  `.../play`. The key itself is never put in a URL. `?api_key=` is ignored
  everywhere and gets a 401.
* On any 401 it opens a password prompt ("API key required"), saves the
  value and reloads. The status pill reads `API key needed` until then. The
  Access panel in the Settings view has Save and Forget buttons for the same
  value.

When the key "does not work", check these:

1. Supplied keys are trimmed of surrounding whitespace, but the configured
   value is compared verbatim. A trailing space or newline in
   `AUDIOFP_API_KEY` is easy to pick up in a systemd unit or a Windows
   service definition, and it makes every key `Invalid API key.`.
   `audiofp config` only shows `***`, so check the environment itself.
2. Clear a stale key with the Forget button in the Access panel of the
   Settings view, or run `localStorage.removeItem("audiofp:apiKey")` in the
   browser console.
3. If a reverse proxy strips `Authorization`, use `X-API-Key`.
4. Audio URLs carry a stream token as a query string. The key itself never
   goes into a URL, but the token can land in reverse-proxy access logs and
   browser history. A leaked token opens one track for at most an hour.
   AudioFP's own access log masks the `token` and `api_key` query values as
   `***`, and `AUDIOFP_ACCESS_LOG=false` disables that log entirely.

## 403 when indexing a folder

`POST /api/v1/tracks/index-directory` (in the UI, the Index a folder panel
in the Library view) is gated by `Runtime.check_directory_allowed()` and can
answer:

| HTTP | Message | Fix |
|---|---|---|
| 403 | `Directory indexing is disabled on this server (AUDIOFP_ALLOW_DIRECTORY_INDEXING=false).` | Set `AUDIOFP_ALLOW_DIRECTORY_INDEXING=true` (the default) and restart. |
| 403 | `That directory is outside the folders this server may index.` (`details.allowed_roots`) | Index a path inside one of `AUDIOFP_INDEX_ROOTS`, or add the folder to the list. |
| 403 | `In production, directory indexing requires AUDIOFP_INDEX_ROOTS to list the folders the server may read.` | The production rule: with `AUDIOFP_PROFILE=production` and no roots configured, folder indexing is refused outright. Set `AUDIOFP_INDEX_ROOTS`. |
| 400 | `'X' is not a directory on the server` | The path is checked on the machine running AudioFP, so a folder on the browser's machine won't be found. This check runs only after the 403 rules above pass, so a forbidden path is never told whether it exists. |
| 400 | `No supported audio or video files were found in that directory.` | Nothing with a supported extension below that folder (recursion is on unless `recursive: false`). |
| 429 | `Too many jobs queued; wait for running jobs to finish.` | Active jobs reached `4 x AUDIOFP_MAX_CONCURRENT_JOBS`. |

`AUDIOFP_INDEX_ROOTS` is comma-separated, for example
`AUDIOFP_INDEX_ROOTS=/srv/calls,/srv/jingles` or
`AUDIOFP_INDEX_ROOTS=D:\calls,E:\jingles`. Both the roots and the requested
directory are resolved before comparison, with symlinks and junctions
followed. A symlink that points outside a root is rejected even though its
path looks fine. `~` is expanded in the requested `directory_path` only.
Roots are taken literally, so write them out in full.

In development with no roots configured, any path is allowed. That is why
the `production` profile insists on an explicit list. The UI reads
`features.directory_indexing` and `features.index_roots` from
`GET /api/v1/info` and greys out the folder panel when indexing is not
available. A greyed-out panel means it is switched off in configuration.
None of this applies to the CLI. `audiofp index <folder>` runs on the server
with the server's permissions.

## 413 payload too large

```json
{"error": "Upload is too large. The server accepts at most 2048 MB per request (AUDIOFP_MAX_UPLOAD_MB).", "code": "payload_too_large", "status": 413}
```

Flask's `MAX_CONTENT_LENGTH` is `AUDIOFP_MAX_UPLOAD_MB` (default 2048)
megabytes and applies to `/search` as well as `/tracks`. Raise it and
restart. The UI compares files against `limits.max_upload_mb` from
`GET /api/v1/info` before uploading, and marks oversize files
`larger than the N MB limit` without sending them.

The JSON endpoints (`PATCH`/`PUT /tracks/{id}`, `POST /tracks/index-directory`,
`POST /tracks/bulk-delete`, `PUT`/`PATCH /settings`) have a separate, fixed
1 MiB cap that no setting raises: `JSON body too large (max 1024 KB)`. The
upload limit has nothing to do with it. Split a very large `metadata` object
or a bulk-delete list, which takes at most 1000 ids anyway.

If you get a 413 that isn't JSON, it did not come from Flask:

* `audiofp serve` also passes the same limit to waitress as
  `max_request_body_size`, and waitress can reject a body before Flask sees
  it.
* A reverse proxy in front has its own limit, nginx `client_max_body_size`
  for example. See `docs/DEPLOYMENT.md`.

For search you rarely need big files. A few seconds identifies a clip, and
only the first `AUDIOFP_MAX_QUERY_SECONDS` (default 3600) of a query are
analysed anyway. The response then has `query.truncated: true`.

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

1. Check the track is in the library at all, with
   `audiofp tracks list --q "<name>"` or the Library search. A file skipped
   as a duplicate of something else was not indexed twice, but the earlier
   copy is searchable.
2. `query.num_hashes` near 0 means silence, a sub-second clip or pure noise,
   so there is nothing to match. Use a longer, cleaner clip. 5 to 10 s helps
   with noisy audio. All audio is resampled to `sample_rate` (11025 Hz by
   default), so content above about 5.5 kHz does not contribute.
3. Work out which thresholds applied. The defaults are `min_confidence`
   0.02, `min_aligned_hashes` 10 and `min_peak_ratio` 12.0, but three things
   override them:
   * `PUT /api/v1/settings` (in the UI: "Server search defaults" in the
     Settings view) persists to `<data_dir>/runtime-settings.json` and
     overrides the env values for both the API and the CLI. `audiofp config`
     shows the env values. `GET /api/v1/settings` shows the effective ones.
   * The Thresholds panel on the Search view keeps per-browser values in
     `localStorage` (`audiofp:searchOptions`) and sends them with every
     search. The "Reset to server defaults" button restores the server
     values.
   * Per-request form fields `min_confidence`, `min_aligned_hashes`,
     `min_peak_ratio`, `top_k`, `mode` (CLI: `--min-confidence`,
     `--min-aligned`, `--min-peak-ratio`, `--top-k`, `--mode`).
   To see weak candidates, lower them for one search:
   `audiofp search clip.wav --min-confidence 0.005 --min-peak-ratio 5 --json`.
   Results are labelled `strong` when confidence >= 0.15 and peak ratio
   >= 30, `likely` at >= 0.05 and >= 18, otherwise `weak`. A lone `weak` hit
   at very low thresholds is probably chance.
4. `identify` returns the best alignment per track. `occurrences` returns
   every alignment above the thresholds, and offsets may be negative, which
   means the indexed track's content was found inside the query. To find
   every place a jingle appears in a set of calls, index the short jingle
   and search with the long recording using `--mode occurrences`.
5. `query.truncated: true` means only the first `AUDIOFP_MAX_QUERY_SECONDS`
   were analysed, and the match may lie beyond that.
6. Read `diagnostics`. `db_rows: 0` means no query hash exists in the
   library at all: wrong library, different fingerprint parameters, or audio
   that never overlaps. `candidate_tracks` with `scored_tracks: 0` means no
   track came within reach of `min_aligned_hashes` even before scoring. A
   large `skipped_common_hashes` means the query consists of hashes the
   library repeats more than `AUDIOFP_MAX_ROWS_PER_HASH` (2000) times. Hold
   music, tones and loops do this, and such hashes are skipped as stop
   words. A non-zero `dropped_for_vote_cap` means `AUDIOFP_MAX_SEARCH_VOTES`
   bit, and the log also says `Search vote cap hit`. Raise those two only
   for libraries that really consist of such material, see `docs/TUNING.md`.
7. A parameter mismatch looks the same. If you run with
   `AUDIOFP_FINGERPRINT_COMPAT=warn` or `ignore`, different fingerprint
   parameters show up as nothing ever matching. Check the log for the
   warning, or run `audiofp db check` and compare the signature with
   `audiofp config`.
8. Consider how the audio was processed. Fingerprints survive MP3/AAC
   compression, resampling and moderate noise, but not speed or pitch
   changes.

`audiofp search` exits with status 1 when nothing is found, so scripts can
branch on it.

## Duplicates being skipped

Indexing reports `duplicate` outcomes. The UI shows `Already in the library
(duplicate)`, the job counts them under `skipped`, and the summary lists them
in `duplicates_of[]` with the existing `track_id`. Behaviour follows
`AUDIOFP_DEDUPE`:

| Mode | Duplicate means | Notes |
|---|---|---|
| `content` (default) | Same SHA-256 of the file bytes as an existing track. | Byte-identical only. A re-encoded or trimmed copy counts as new and is indexed as a second track. Every file is hashed before decoding, so re-running a folder is cheap. |
| `path` | Same absolute path as an existing track. | Uploads are saved as `<8 hex>_<safe name>` in the upload folder, so two uploads never share a path and `path` mode never dedupes uploads. |
| `none` | Nothing is skipped. | Every file becomes a new track. Identical tracks then all match with identical scores. |

Changing `AUDIOFP_DEDUPE` needs a restart. Duplicate uploads are removed from
`<data_dir>/uploads` right away. The existing track keeps its file.

To re-index a file on purpose, say after a parameter change or to correct
metadata, delete the track first and then index it again. Deleting works
through `DELETE /api/v1/tracks/{id}`, the Library view, or
`audiofp tracks delete <id> --yes`.

## Jobs stuck, or `interrupted` after a restart

Job statuses are `pending`, `running`, `completed`, `failed`, `cancelled`
and `interrupted`. `GET /api/v1/jobs?status=active` lists pending and
running jobs. `GET /api/v1/jobs/{id}` includes the error list. The Activity
view polls every 1.5 s while something is active.

Jobs are written to `<data_dir>/jobs/<job_id>.json` when
`AUDIOFP_PERSIST_JOBS` is on (default true, the `testing` profile turns it
off). When the server starts, any record still `pending` or `running` is
marked `interrupted` with the error:

> The server restarted before this job finished. Files already indexed were kept; re-run to index the rest.

Do what it says and submit the same folder again. With the default `content`
dedupe the already-indexed files are skipped as duplicates and only the
remainder is fingerprinted. Progress is written to the file at most every
2 s, so the counters of an interrupted job may lag the real progress
slightly.

With SQLite, "already indexed" means "already flushed to disk". Fingerprints
are buffered in memory (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`, default 2,000,000
rows) and written at the end of each run, when the buffer fills, and on
shutdown. `Ctrl+C`, `systemctl stop` and `docker stop` all flush. If the
process was killed outright, tracks whose rows were still in the buffer are
removed the next time the store opens. The warning is
`Removed N track(s) whose fingerprints were lost in an unclean shutdown (...);
re-index them.` Re-running the folder indexes them again, since nothing in
the library matches them now. Set `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0` to
write every track immediately if that trade-off is wrong for you.

Only `AUDIOFP_MAX_CONCURRENT_JOBS` (default 2) jobs run at once, and
uploads and folder jobs share the pool, so a job can sit at `pending` for a
while. A third job waits. When the active count reaches
four times that number, new folder-indexing requests get a 429. Uploads are
still accepted and queue behind the others.

If a job is `running` but not moving, look at `current_item`. One very long
or slow file, say an hour-long video through ffmpeg or a file on a slow
network share, holds a worker, and `completed` only advances per finished
file. `elapsed_sec` and `eta_sec` are estimates from the rate so far. ffmpeg
is killed after 3600 s per file, after which the file fails with
`audio_decode_error` and the job moves on. `AUDIOFP_INDEX_WORKERS` (0 =
`min(4, CPU count)`) sets how many files are fingerprinted in parallel.

Cancel does not stop a job immediately. `POST /api/v1/jobs/{id}/cancel` is
cooperative: files already handed to workers (up to two per worker) finish,
then the job ends as `cancelled`. `DELETE /api/v1/jobs/{id}` only works on
finished jobs and otherwise answers
`Cannot remove a job that is still running; cancel it first`.

If jobs vanish or 404 intermittently, check how many server processes you
run. A job lives in the memory of the process that accepted it and is only
loaded from disk at startup. Behind a multi-process WSGI setup (several
gunicorn workers) a `GET /jobs/{id}` may land on a process that never saw
the job. Run one process with threads: waitress, which is the default outside
development, or `gunicorn --workers 1 --threads N`. See `docs/DEPLOYMENT.md`.

`database is locked` comes from SQLite's write lock. Writers inside one
process queue on it. Across processes, for example `audiofp index` against the same
file while the server is indexing, they wait up to 30 s. Then the write fails
with a `storage_error` and the run stops. The job ends as `failed` with
`Indexing stopped after N file(s): ... database is locked`, keeping what was
indexed. Let one writer finish, or index through the running server.

Finished jobs beyond `AUDIOFP_JOB_HISTORY_LIMIT` (default 200) are pruned,
oldest first, together with their files.

## Playback fails but search works

`GET /api/v1/tracks/{id}/audio` (alias `/play`) streams the original file
from the `filepath` recorded at index time. When that file is gone you get a
404:

```json
{"error": "The original file for this track is no longer on disk. The fingerprint still works for searching.", "code": "file_missing", "status": 404, "details": {"track_id": "..."}}
```

`GET /api/v1/tracks/{id}` reports `file_exists` for this. The UI shows "The
original file is no longer on disk. Search still works, but playback will
not." in the track drawer, and the player toasts "Could not play this
track". Search is unaffected because it only needs the fingerprints, which
are in the database.

Why it happens:

* The library folder was moved or renamed. `filepath` is absolute and there
  is no relocate command. Re-indexing from the new location does not update
  the paths either: with `content` dedupe the moved files are byte-identical
  to the existing tracks and are reported as duplicates. Delete the affected
  tracks (`POST /api/v1/tracks/bulk-delete`, or select them in the Library)
  and index the new location.
* An upload was deleted. `DELETE /api/v1/tracks/{id}?delete_file=true` and
  the UI's "Delete track + file" remove the file, but only when it lives
  inside `<data_dir>/uploads`. AudioFP never deletes files indexed from
  folders. Failed uploads are removed automatically unless
  `AUDIOFP_KEEP_FAILED_UPLOADS=true`.
* The server now runs with a different `AUDIOFP_UPLOAD_DIR` or
  `AUDIOFP_DATA_DIR` from the one the files were uploaded under.

Other playback problems:

* Playback works in curl but fails in the browser on a locked server. The
  `<audio>` element cannot send the key, so it needs a stream token.
  `GET /api/v1/tracks/{id}/stream-token` with the key returns a `url` of the
  form `.../audio?token=<token>`, valid for one hour and for this track
  only. The bundled UI does this and a custom front-end must too.
  `?api_key=` in the URL is not accepted.
* Seeking fails through a proxy. The endpoint answers HTTP Range requests
  (`206`), which the proxy must pass through.
* Video tracks are streamed with their container's MIME type (`video/mp4`,
  `video/x-matroska`, ...) and the UI plays them through an `<audio>`
  element. Whether that works depends on the browser's codec support, and
  AudioFP can't do anything about it. The fingerprint side is unaffected.

## Microphone recording does nothing

The Record button on the Search view toggles: press once to start, again to
stop. It uses the browser's `MediaRecorder`, which browsers only expose in a
secure context. So opening the UI as `http://<LAN address>:5000` shows
"Microphone recording needs a secure context. Open the app over https:// or
at http://localhost." and nothing is recorded. Use `http://localhost:5000` on
the same machine, or put the server behind TLS (see `docs/DEPLOYMENT.md`).

The same rule affects the copy buttons for track ids and request ids. On
plain `http` the clipboard API is unavailable, so they fall back to
`execCommand("copy")` and show "Copy failed, select the text by hand" if the
browser refuses that too.

Chrome and Edge record WebM/MP4, which the server can only decode with
ffmpeg (see [ffmpeg not found](#ffmpeg-not-found)). Firefox records
OGG/Opus.

## Windows notes

* If `audiofp` is not found, that is because `pip install -e .` puts
  `audiofp.exe` in the environment's `Scripts` folder, which is often not on
  PATH. `python -m fingerprint <command>` does the same thing. `python run.py`
  still starts the server, and `--env production` is translated to
  `--profile production`.
* For ffmpeg, run `winget install Gyan.FFmpeg`, then open a new terminal,
  check `where ffmpeg`, and restart AudioFP (the lookup is cached per
  process). For a service account with a different PATH, set
  `AUDIOFP_FFMPEG_BINARY` to the full path of `ffmpeg.exe`. ffmpeg is
  launched with `CREATE_NO_WINDOW`, so no console windows flash when the
  server runs as a service.
* AudioFP does not add the `\\?\` prefix for long paths. Paths longer than
  260 characters typically surface as files missing from a folder scan,
  `file_not_found`, or `Could not decode ...: System error`. Enabling Windows
  long paths avoids that: `LongPathsEnabled = 1` under
  `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`, or the "Enable Win32
  long paths" group policy. Python honours that setting. We haven't checked
  this against the code base. It is general Windows and Python behaviour.
* `audiofp serve --profile production` (or `--server waitress`) uses
  waitress, which is a core dependency and runs natively on Windows.
  `--threads` or `AUDIOFP_SERVER_THREADS` (default 8) sets its worker
  threads. The `gunicorn` extra is for Linux and macOS only. In the
  `development` profile `--server auto` picks Flask's built-in server.
* Backslashes in configuration values are fine:
  `AUDIOFP_INDEX_ROOTS=D:\calls,E:\jingles`, `AUDIOFP_SQLITE_PATH=D:\audiofp\fingerprints.db`.
  The `.env` reader accepts quoted values. Commas separate list entries, so a
  root path cannot itself contain a comma.
* Keep `AUDIOFP_SQLITE_PATH` on a local disk. The database runs in WAL mode,
  which is not safe on SMB or NFS shares. Indexing from a share is fine, if
  slow.
* Real-time antivirus scanners that briefly lock files can show up as
  sporadic `Could not decode ...` failures on otherwise fine files during
  large runs. This is operational experience we haven't verified, and
  nothing in AudioFP retries a locked file. Re-run the folder (already-indexed
  files are skipped as duplicates) or exclude the data directory and library
  folders from scanning.

## Logs and request ids

Everything is logged under the `fingerprint` logger namespace, and it goes
to:

| Destination | When |
|---|---|
| Console (stderr) | Always. |
| `AUDIOFP_LOG_FILE` (rotating: `AUDIOFP_LOG_MAX_MB` x `AUDIOFP_LOG_BACKUP_COUNT`, defaults 20 MB x 5) | When set to a path, and only for `audiofp serve` and the WSGI app. Other CLI commands log to the console only. `none` or an empty value also means console only. If the file cannot be created, the server prints a `Cannot write the log file ...; logging to the console only` warning and keeps running. |
| `<data_dir>/logs/audiofp.log` | What the default `AUDIOFP_LOG_FILE=auto` resolves to in the `production` profile. In the other profiles `auto` means console only. It follows `AUDIOFP_DATA_DIR`. `audiofp config` prints the raw value, `auto`, so don't expect to see the resolved path there. |

The access log (`AUDIOFP_ACCESS_LOG`, default true) writes one INFO line per
`/api/` request: `METHOD /path?query -> status (N ms)`. The query string is
included, but the values of `token` and `api_key` parameters are replaced
with `***` before the line is written.

Each request gets an id. If you send an inbound `X-Request-ID` header, its
first 64 characters are used, provided they consist only of letters, digits,
`.`, `_`, `:` and `-`. Otherwise the id is 12 random hex characters. It is
returned as the `X-Request-ID` response header and placed in the
`request_id` field of every error body. The UI shows it as `request <id>`
under error toasts and search errors. It is also injected into every log
line written while handling the request. In the text format
(`%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s`) it is
the bracketed token:

```text
2026-09-17 10:42:07 INFO    fingerprint.api.errors [3f9c2a1b7d4e] ffmpeg_not_found: 'call.m4a' (.m4a) requires ffmpeg, which is not installed. ...
2026-09-17 10:42:07 INFO    fingerprint.api.app [3f9c2a1b7d4e] POST /api/v1/search -> 422 (14 ms)
```

Take the id from the response or the toast, then

```bash
grep 3f9c2a1b7d4e data/logs/audiofp.log
```

```powershell
Select-String 3f9c2a1b7d4e data\logs\audiofp.log
```

4xx client errors are logged at INFO as `<code>: <message>`. 5xx errors
are logged at ERROR, and `internal_error` includes the traceback. Lines with
`[-]` were written outside a request, during startup or by background jobs.
Job lines carry the job id in the message: `Job <id> queued: ...`,
`Job <id> completed in ...`, and `Job <id> failed` with a traceback. Each
file logs `Indexed <name>: ...`, `Failed to index <name>: ...` or
`Skipping <name>: identical content already indexed as <track_id>`.

Send your own id (`X-Request-ID: order-7781`) from a calling system to
correlate both sides. With `AUDIOFP_TRUST_PROXY=true` the `X-Forwarded-*`
headers from a reverse proxy are honoured as well.

## DEBUG and JSON logs

```bash
AUDIOFP_LOG_LEVEL=DEBUG audiofp serve          # server
audiofp search clip.wav --log-level DEBUG      # any CLI command accepts --log-level
```

The `development` profile already runs at DEBUG. At DEBUG you also get the
exact ffmpeg command line for every decode, and the `werkzeug`, `waitress`
and `urllib3` loggers are un-quieted (they are held at WARNING otherwise).
`--quiet` on `audiofp index`, `search`, `tracks` and `stats` drops to
WARNING. `serve` keeps `AUDIOFP_LOG_LEVEL`, and `doctor`, `config` and `db`
do not configure logging at all.

```bash
AUDIOFP_LOG_FORMAT=json audiofp serve
```

emits one JSON object per line with `ts`, `level`, `logger` and `message`,
plus `request_id` and `job_id` when set and `exception` for tracebacks. Use
this with Loki, Datadog, CloudWatch and the like:

```json
{"ts": "2026-09-17T10:42:07.123Z", "level": "INFO", "logger": "fingerprint.api.app", "message": "POST /api/v1/search -> 422 (14 ms)", "request_id": "3f9c2a1b7d4e"}
```

`job_id` only appears in the JSON format. The text format prints the request
id alone. If you embed the app in your own WSGI process and configure
logging yourself, call `create_app(configure_logs=False)`.

## Resetting everything

Everything AudioFP writes lives under `AUDIOFP_DATA_DIR`, or `--data-dir` on
the CLI. The default is `./data`, relative to the working directory:

| Path | Contents |
|---|---|
| `fingerprints.db` (+ `-wal`, `-shm`) | The SQLite library (`AUDIOFP_SQLITE_PATH` overrides the location). |
| `uploads/` | Files uploaded through the API/UI (`AUDIOFP_UPLOAD_DIR` overrides). |
| `jobs/` | One JSON file per job. |
| `runtime-settings.json` | Search defaults saved through `PUT /api/v1/settings`. |
| `logs/` | `audiofp.log` and its rotated copies. This is where the `production` profile logs by default, `AUDIOFP_LOG_FILE=auto`, see [Logs and request ids](#logs-and-request-ids). |

To reset the database only, keeping uploads, job history and runtime
settings:

```bash
audiofp db reset --yes      # wipes tracks + fingerprints, re-stamps the current fingerprint signature, VACUUMs SQLite
```

This works for PostgreSQL too. The tables are emptied and left in place.

To reset everything, stop the server first. SQLite in WAL mode keeps state
in the `-wal` file until the last connection closes. Then delete the data
directory:

```bash
rm -rf data
```

```powershell
Remove-Item -Recurse -Force data
```

The next `audiofp serve` or `audiofp index` recreates the directory, an
empty database stamped with the current fingerprint signature, and empty
`uploads/` and `jobs/` folders. Deleting `runtime-settings.json` is what
resets search thresholds you changed in the UI. `AUDIOFP_*` variables and
`.env` are untouched by any of this.

To reclaim disk space without deleting anything, `audiofp db vacuum`
checkpoints the WAL and runs `VACUUM` (SQLite only).
