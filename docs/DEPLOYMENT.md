# Deployment

From a laptop to a production host. Everything below refers to names that exist in the code: settings are fields of `Settings` in `fingerprint/config.py` (environment variable = `AUDIOFP_<FIELD_UPPER>`), commands are in `fingerprint/cli.py`, endpoints in `fingerprint/api/routes/*.py`. Run `audiofp config --describe` for the full option table.

## Contents

- [Requirements](#requirements)
- [Install options](#install-options)
- [Running](#running)
- [Production checklist](#production-checklist)
- [Docker and Compose](#docker-and-compose)
- [PostgreSQL setup](#postgresql-setup)
- [Upgrading from 1.x](#upgrading-from-1x)
- [Health and monitoring](#health-and-monitoring)
- [Multi-instance caveats](#multi-instance-caveats)

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.10 or newer | `requires-python = ">=3.10"` in `pyproject.toml`; `audiofp doctor` fails below 3.10. CI runs 3.10, 3.12 and 3.13 on Linux and 3.12/3.13 on Windows and macOS. |
| Python packages | `flask`, `flask-cors`, `werkzeug`, `numpy`, `scipy`, `soundfile`, `soxr`, `waitress`. All are hard dependencies, so the production server (waitress) is always available. |
| ffmpeg (optional) | Needed only for video containers and for audio formats libsndfile cannot read. Not needed for WAV, FLAC, OGG/Opus, MP3, AIFF, AU, CAF or W64. |
| Disk | Fingerprint database plus uploaded originals; see the sizing hint below. |

### What ffmpeg unlocks

The decoder (`fingerprint/core/decoder.py`) reads everything in `NATIVE_AUDIO_EXTENSIONS` through `soundfile`/libsndfile with no external binary: `wav`, `wave`, `flac`, `ogg`, `oga`, `opus`, `mp3`, `aiff`, `aif`, `aifc`, `au`, `caf`, `w64` (MP3 needs libsndfile 1.1+, which the `soundfile` 0.12+ wheels bundle).

ffmpeg is required for:

- `FFMPEG_AUDIO_EXTENSIONS`: `m4a`, `aac`, `wma`, `amr`, `ac3`, `dts`, `mka`, `weba`
- `VIDEO_EXTENSIONS`: `mp4`, `mkv`, `avi`, `mov`, `wmv`, `flv`, `webm`, `m4v`, `mpeg`, `mpg`, `ts`, `mts`, `3gp`, `vob` (the audio track is extracted)

Without ffmpeg those files fail with the error code `ffmpeg_not_found` (HTTP 422) and everything else keeps working. `audiofp doctor` and `GET /api/v1/health` (`ffmpeg.available`) tell you which situation you are in. ffmpeg is located on `PATH`; to use a specific binary set the plain environment variable `AUDIOFP_FFMPEG_BINARY` (read directly by the decoder at import time, so it is not a `Settings` field and does not appear in `audiofp config`).

```bash
# Windows                      macOS                    Debian/Ubuntu
winget install Gyan.FFmpeg     brew install ffmpeg      sudo apt install ffmpeg
```

### Disk sizing hint

Storage cost is dominated by the `fingerprints` table. Measured with the 2.0 SQLite layout at default fingerprint parameters on synthetic material (`audiofp index` of 40 one-minute files per kind, then `audiofp stats`): about 130-145 hashes per second of audio for speech/music-like material (the test suite's generator), about 25 for sparse sustained tones and about 520-550 hashes per second for white noise (the worst case), at roughly 16-18 bytes per stored hash (about 15.5 after `audiofp db vacuum`). That is about **8-35 MB of database per hour of indexed audio**; 1,000 hours lands somewhere between 8 and 35 GB. Real recordings vary with how dense they are, so index a representative sample and read `Database size` from `audiofp stats` (its `db_size_bytes` includes the `-wal` file), then scale linearly.

Two things besides the database use disk:

- **Uploads are kept.** Files sent to `POST /api/v1/tracks` stay in `<data_dir>/uploads` so the UI can play them; only duplicates and failed uploads are removed (`keep_failed_uploads` controls the latter). Budget the size of the originals.
- **Folder-indexed files are referenced in place.** `POST /api/v1/tracks/index-directory` and `audiofp index` store the file's path (`filepath`), they do not copy it. Searching keeps working if the file disappears, but `GET /tracks/<id>/audio` then returns `file_missing`.

Memory stays flat regardless of file length because audio is decoded and fingerprinted in `chunk_seconds` (30 s) chunks; the opt-in scale test asserts less than 300 MB growth on a 30-minute file. SQLite uses one connection per thread with a `sqlite_cache_mb` (64 MB) page cache each, plus one in-memory fingerprint write buffer per process (`sqlite_write_batch_rows`, 2,000,000 rows at about 16 bytes each, roughly 32 MB when full), so a default single node is comfortable with 1-2 GB of RAM (rule of thumb, not measured).

## Install options

### pip install (recommended)

```bash
git clone https://github.com/inboxpraveen/Audio-Fingerprint.git
cd Audio-Fingerprint
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .                                        # installs the `audiofp` command
audiofp doctor                                          # dependencies, ffmpeg, storage, config, disk space
```

Optional extras defined in `pyproject.toml`:

| Extra | Installs | When |
|---|---|---|
| `pip install -e ".[postgres]"` | `psycopg[binary,pool]` | `AUDIOFP_STORAGE_TYPE=postgres` |
| `pip install -e ".[gunicorn]"` | `gunicorn` | Running under gunicorn on Linux/macOS |
| `pip install -e ".[dev]"` | `pytest`, `pytest-cov`, `ruff`, `psutil` | Running the test suite |

### requirements.txt

`requirements.txt` lists the same runtime dependencies as `pyproject.toml` but does not install the package itself, so there is no `audiofp` command. Use the module form instead:

```bash
pip install -r requirements.txt
python -m fingerprint serve        # identical to `audiofp serve`
python run.py                      # compatibility launcher, see below
```

### Docker

`docker/Dockerfile` builds a `python:3.12-slim` image with ffmpeg included, so every supported format works out of the box. See [Docker and Compose](#docker-and-compose).

## Running

### `audiofp serve`

```bash
audiofp serve                                   # development: Flask dev server on http://127.0.0.1:5000
audiofp serve --profile production              # waitress on 0.0.0.0:5000 (set AUDIOFP_API_KEY first)
audiofp serve --port 8080 --host 0.0.0.0        # override bind address
audiofp serve --server waitress --threads 16    # force a server / thread count
```

Flags: `--host`, `--port`, `--server auto|flask|waitress` (default `auto`), `--threads`, plus the options shared by every command: `--profile`, `--data-dir`, `--storage memory|sqlite|postgres`, `--sqlite-path`, `--log-level`, `--quiet`.

`--server auto` picks Flask's development server when the profile is `development` and waitress otherwise. When waitress is used, `cmd_serve` passes `threads` (`--threads` or `server_threads`, default 8), `channel_timeout=600` and `max_request_body_size` equal to `max_upload_mb` (default 2048 MB). Use `--server flask` in production only for debugging.

The banner printed at start-up shows the profile, storage backend, UI URL (`/`), API base (`/api/v1`) and docs page (`/docs`). It also warns when the server listens on `0.0.0.0` without an API key.

### Profiles

A profile is a set of defaults applied before environment variables (`_profile_defaults` in `fingerprint/config.py`). Select it with `AUDIOFP_PROFILE` or `--profile`.

| Profile | Defaults it changes |
|---|---|
| `development` (default) | `debug=true`, `log_level=DEBUG` (host stays at the field default `127.0.0.1`; `--server auto` picks the Flask dev server) |
| `production` | `debug=false`, `log_level=INFO`, `host=0.0.0.0`, waitress, directory indexing requires `index_roots`. The profile does not set `log_file`; the default `log_file=auto` resolves to `<data_dir>/logs/audiofp.log` in this profile only (`Settings.log_file_resolved`) |
| `testing` | `storage_type=memory`, `persist_jobs=false`, `log_level=WARNING`, `data_dir=data/test` |

Configuration is resolved as: field defaults, then the profile, then `AUDIOFP_*` environment variables (optionally read from a `.env` file in the working directory, or the file named by `AUDIOFP_ENV_FILE`), then CLI flags. Real environment variables override `.env`; only `AUDIOFP_*` keys are read from it. An empty value (`AUDIOFP_LOG_FILE=`, in the environment or in `.env`) counts as a real value for text and list settings - it sets the string to `""` or the list to `[]`, which is how you disable the log file or clear `AUDIOFP_CORS_ORIGINS` - but is ignored for numeric and boolean settings (`AUDIOFP_PORT=` leaves the default in place). `audiofp config` prints the effective result with secrets shown as `***`, which makes it safe to paste into a support ticket.

### `python -m fingerprint` and `run.py`

`python -m fingerprint <command>` is the same CLI (`fingerprint/__main__.py` calls `fingerprint.cli.main`). Useful when the package is installed without the console script or from a plain `requirements.txt` environment.

`run.py` is kept for 1.x muscle memory. It always runs `serve`, translates `--env <profile>` into `--profile <profile>` and passes every other argument through:

```bash
python run.py                      # audiofp serve
python run.py --port 8080          # audiofp serve --port 8080
python run.py --env production     # audiofp serve --profile production
```

### Bulk loading from the CLI

For the initial load, `audiofp index /srv/recordings --tags campaign-2026` fingerprints a folder with a progress bar and the same worker pool the API uses (`index_workers`, `0` = min(4, CPU count)). It is safe to run next to a serving instance (WAL readers never block a writer; the server can match the new tracks once the CLI has flushed its write buffer, which happens every `sqlite_write_batch_rows` rows and at the end of the run), but do not run two processes that both index into the same SQLite file at the same time; the write lock is per process and the fallback is a 30-second `busy_timeout`.

## Production checklist

Each row says what to set and why the code cares.

| Item | Setting | Why |
|---|---|---|
| Profile | `AUDIOFP_PROFILE=production` | Turns off Flask debug, selects waitress, INFO logging, and requires `index_roots` for `POST /tracks/index-directory`. `audiofp doctor` warns about the two most common omissions (no API key, no index roots). |
| API key | `AUDIOFP_API_KEY=<long random string>` | Without it anyone who can reach the port can index, search and delete. With it every `/api/v1` request must send `X-API-Key: <key>` or `Authorization: Bearer <key>`; `/api/v1/health` and `/api/v1/openapi.json` stay public, and because `<audio>` elements cannot send headers, `GET .../audio` and `.../play` also accept `?token=<stream token>`: a track-scoped token minted by `GET /api/v1/tracks/{id}/stream-token` (needs the key), HMAC-signed with the key and valid for one hour. Comparison is constant-time; the access log redacts `token` and `api_key` query values. The bundled UI asks for the key once and keeps it in the browser's local storage. One shared secret is all that is built in; per-user keys, SSO and rate limiting belong in the reverse proxy. |
| Folder indexing | `AUDIOFP_INDEX_ROOTS=/srv/recordings,/mnt/archive` | Comma-separated allow-list for `directory_path` in `POST /tracks/index-directory`. In production the endpoint returns 403 until this is set; set `AUDIOFP_ALLOW_DIRECTORY_INDEXING=false` to disable it in every profile (the route stays registered and always answers 403) and accept uploads only. `GET /api/v1/info` reports `features.directory_indexing` and `features.index_roots`. |
| Host binding | `AUDIOFP_HOST=127.0.0.1` behind a same-host proxy, `0.0.0.0` (the production default) only when the port itself should be reachable | Keeps the unencrypted, single-key service off the network when nginx terminates TLS in front of it. |
| Reverse proxy headers | `AUDIOFP_TRUST_PROXY=true` | Wraps the app in Werkzeug's `ProxyFix(x_for=1, x_proto=1, x_host=1)`, so Flask sees the client address, scheme and host from `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` set by exactly one proxy hop. Leave it off when clients hit AudioFP directly, otherwise those headers can be spoofed. |
| Logging | `AUDIOFP_LOG_FORMAT=json`, `AUDIOFP_LOG_FILE=/var/lib/audiofp/logs/audiofp.log`, `AUDIOFP_LOG_MAX_MB=20`, `AUDIOFP_LOG_BACKUP_COUNT=5` | JSON is one object per line (`ts`, `level`, `logger`, `message`, `request_id`, `job_id`, `exception`) for Loki/Datadog/CloudWatch. The file handler rotates; the console (stderr) always gets a copy, and if the log directory cannot be created or written the server logs `Cannot write the log file ...; logging to the console only` and keeps running with console output only. The default `AUDIOFP_LOG_FILE=auto` resolves to `<AUDIOFP_DATA_DIR>/logs/audiofp.log` in the production profile (console only in the others), so with an absolute data directory the log lands next to the database; set the path explicitly if it should live elsewhere, and `none` (or an empty value) to disable the file. `AUDIOFP_ACCESS_LOG=false` silences the per-request line; its query strings have `token` and `api_key` values masked. |
| Data directory | `AUDIOFP_DATA_DIR=/var/lib/audiofp` on a persistent disk, absolute | Holds `fingerprints.db` (+ `-wal`/`-shm` while open), `uploads/`, `jobs/` and `runtime-settings.json` (search defaults changed through the UI or `PUT /settings`). `audiofp doctor` checks it is writable and warns below 1 GB free. |
| Backups | see below | The SQLite database is in WAL mode: a raw copy of `fingerprints.db` alone can miss the newest writes. |
| Upload limit | `AUDIOFP_MAX_UPLOAD_MB=2048` (default) | Applied as Flask's `MAX_CONTENT_LENGTH` and waitress's `max_request_body_size`. Oversize requests get a 413 whose message names this variable. Your proxy's body limit must be at least as large. |
| Threads and workers | `AUDIOFP_SERVER_THREADS=8` (or `--threads`), `AUDIOFP_INDEX_WORKERS=0`, `AUDIOFP_MAX_CONCURRENT_JOBS=2` | Waitress threads serve requests; index workers fingerprint files in parallel (0 = min(4, CPU count)); concurrent jobs share that pool. Search is CPU-bound in numpy, so more threads than cores buys little. |
| CORS | leave `AUDIOFP_CORS_ORIGINS` unset unless a separate frontend calls the API | The bundled UI is same-origin. No profile enables CORS by default; `*` allows any origin (never combine it with a missing API key). |
| Fingerprint compatibility | keep `AUDIOFP_FINGERPRINT_COMPAT=strict` | The store records the fingerprint parameter signature; a mismatch means matching would silently degrade, so the default refuses to start. Change fingerprint parameters only together with `audiofp db reset --yes` and a full re-index. |
| Pre-flight | `audiofp doctor` and `audiofp db check` with the production environment loaded | Confirms Python, libsndfile formats, ffmpeg, writable directories, free space and that the database opens with a compatible signature. |

### Backups of the SQLite file

While the server is running, the database consists of `fingerprints.db`, `fingerprints.db-wal` and `fingerprints.db-shm`. Options, safest first:

```bash
# 1. Online, consistent, no downtime: SQLite's backup API
sqlite3 /var/lib/audiofp/fingerprints.db ".backup '/backups/fingerprints-$(date +%F).db'"

# 2. Stop the service, then copy every file that exists
systemctl stop audiofp
cp /var/lib/audiofp/fingerprints.db* /backups/
systemctl start audiofp
```

`audiofp db vacuum` runs `PRAGMA wal_checkpoint(TRUNCATE)` followed by `VACUUM`, which folds the WAL back into the main file and compacts it. It takes an exclusive lock and needs up to twice the database size in free space, so schedule it in a quiet window, not before every backup. Back up `uploads/` alongside the database if you rely on playback of uploaded files. PostgreSQL deployments use `pg_dump` as usual.

### gunicorn on Linux

`fingerprint/api/wsgi.py` exposes a ready-made application object built from the environment (`app = create_app()`):

```bash
pip install -e ".[gunicorn]"
AUDIOFP_PROFILE=production gunicorn "fingerprint.api.wsgi:app" \
  --bind 127.0.0.1:5000 --workers 1 --threads 8 --timeout 600
```

Keep **one worker** and scale with threads. Three things in the process model break with several workers:

- The job manager (`fingerprint/jobs/manager.py`) is in-process. A `POST /tracks` handled by worker A creates a job only worker A knows about; `GET /jobs/<id>` routed to worker B answers 404.
- Runtime search defaults (`runtime-settings.json`) are read once at start-up, so `PUT /settings` on one worker does not reach the others until restart.
- The SQLite write lock is per process; concurrent writers from several processes fall back to the 30-second `busy_timeout` and can fail with `storage_error`.

Do not use `--preload`: `create_app()` opens and uses a SQLite connection (schema and signature check) and constructs the job thread pool in the master process, and SQLite connections are not safe to carry across a fork (not tested). `--timeout 600` matches waitress's `channel_timeout`; searching an hour-long recording or ingesting a 2 GB upload is one long request. gunicorn does not run on Windows; there `audiofp serve --server waitress` is the production server (run it under a service wrapper such as NSSM or Task Scheduler).

### systemd unit

`/etc/audiofp/audiofp.env` (systemd's `EnvironmentFile` format: `KEY=value`, full-line `#` comments only):

```ini
AUDIOFP_PROFILE=production
AUDIOFP_HOST=127.0.0.1
AUDIOFP_PORT=5000
AUDIOFP_DATA_DIR=/var/lib/audiofp
AUDIOFP_LOG_FILE=/var/lib/audiofp/logs/audiofp.log
AUDIOFP_LOG_FORMAT=json
AUDIOFP_API_KEY=replace-with-a-long-random-string
AUDIOFP_INDEX_ROOTS=/srv/recordings
AUDIOFP_TRUST_PROXY=true
AUDIOFP_MAX_UPLOAD_MB=2048
```

`/etc/systemd/system/audiofp.service`:

```ini
[Unit]
Description=AudioFP audio fingerprinting service
After=network-online.target
Wants=network-online.target

[Service]
User=audiofp
Group=audiofp
# Relative defaults (data dir, log file, .env) resolve against this directory.
WorkingDirectory=/var/lib/audiofp
EnvironmentFile=/etc/audiofp/audiofp.env
ExecStart=/opt/audiofp/venv/bin/audiofp serve --server waitress
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
useradd --system --home /var/lib/audiofp --create-home audiofp
python3 -m venv /opt/audiofp/venv && /opt/audiofp/venv/bin/pip install /path/to/Audio-Fingerprint
chmod 600 /etc/audiofp/audiofp.env
systemctl daemon-reload && systemctl enable --now audiofp
curl -s http://127.0.0.1:5000/api/v1/health
```

The `audiofp` user needs read access to every folder listed in `AUDIOFP_INDEX_ROOTS`. `systemctl stop` sends `SIGTERM`, which `audiofp serve` turns into a clean shutdown: running jobs are cancelled at the next file boundary (their record ends `cancelled`), the SQLite write buffer is flushed and the process exits, normally within a few seconds (keep `TimeoutStopSec` above the time one file takes to fingerprint). Only a hard kill (`SIGKILL`, power loss) is ungraceful: on the next start the job is reported with status `interrupted` and the files whose fingerprints had reached the disk are kept. SQLite buffers fingerprints in memory (`AUDIOFP_SQLITE_WRITE_BATCH_ROWS`, default 2,000,000 rows) and writes them at the end of each run, on shutdown and whenever the buffer fills; tracks still in that buffer when the process was killed are removed at the next start with a warning (`Removed N track(s) whose fingerprints were lost in an unclean shutdown`) and are simply re-indexed when the folder is submitted again. Set `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0` if you would rather have every track written immediately.

### nginx reverse proxy

```nginx
server {
    listen 443 ssl;
    server_name audiofp.example.com;
    # ssl_certificate / ssl_certificate_key ...

    # Must be >= AUDIOFP_MAX_UPLOAD_MB (default 2048), otherwise nginx answers 413 before AudioFP sees the upload.
    client_max_body_size 2048m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;   # read by ProxyFix when AUDIOFP_TRUST_PROXY=true
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Request-ID      $request_id;                  # optional: nginx's id then appears in AudioFP logs

        # A search over an hour-long recording or a large upload is one long request.
        # Match waitress channel_timeout / gunicorn --timeout (600 s).
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;

        # Stream uploads through instead of buffering 2 GB to nginx's disk first.
        proxy_request_buffering off;
    }
}
```

AudioFP does not terminate TLS itself. Streaming (`GET /tracks/<id>/audio`) uses HTTP range requests, which nginx proxies unchanged.

## Docker and Compose

### Image

`docker/Dockerfile` (build context is the repository root):

- `python:3.12-slim` plus `ffmpeg` and `curl`; `pip install .` of the package
- runs as the unprivileged user `audiofp` (uid 1000); `/data` is a declared volume owned by that user
- environment baked in: `AUDIOFP_PROFILE=production`, `AUDIOFP_HOST=0.0.0.0`, `AUDIOFP_PORT=5000`, `AUDIOFP_DATA_DIR=/data` and `AUDIOFP_LOG_FILE=` (empty = console only, see below)
- `EXPOSE 5000`; `HEALTHCHECK` curls `/api/v1/health` every 30 s (20 s start period, 3 retries)
- `CMD ["audiofp", "serve", "--server", "waitress"]`

```bash
docker build -t audiofp -f docker/Dockerfile .
docker run -d --name audiofp -p 5000:5000 \
  -v audiofp-data:/data \
  -v /srv/recordings:/recordings:ro \
  -e AUDIOFP_API_KEY=change-me \
  -e AUDIOFP_INDEX_ROOTS=/recordings \
  -e AUDIOFP_LOG_FILE=/data/logs/audiofp.log \
  audiofp
docker logs -f audiofp          # console logging always goes to stderr
```

Points worth knowing:

- **Volumes.** Everything AudioFP writes lives under `/data` (database, uploads, job history, runtime settings). Keep it on a named volume or a bind mount; a container without it loses the library on removal.
- **Recordings read-only.** Mount the folders you want to index with `:ro` and list the mount point in `AUDIOFP_INDEX_ROOTS`; the paths you send to `POST /tracks/index-directory` are container paths (`/recordings/...`). Because indexed files are referenced in place, the mount must stay at the same path for playback. The container user is uid 1000, so the host files must be readable by that uid.
- **Log file.** The image sets `AUDIOFP_LOG_FILE=` (empty), which means console only: the container logs to stderr and `docker logs` shows everything. For a rotating file on the volume set `AUDIOFP_LOG_FILE=/data/logs/audiofp.log` (as above) or `AUDIOFP_LOG_FILE=auto`, which with `AUDIOFP_DATA_DIR=/data` and the production profile resolves to the same path.
- **Configuration** is entirely `AUDIOFP_*` environment variables; there is nothing to mount under `/app`.
- **Stopping.** `CMD` is in exec form, so `docker stop` delivers `SIGTERM` straight to `audiofp serve`, which cancels running jobs at the next file boundary, flushes the SQLite write buffer and exits. Docker kills the container 10 s after `SIGTERM` by default; use `docker stop -t 60 audiofp` (the compose file sets `stop_grace_period: 60s`) so a flush of a full buffer is not cut short.

### Compose

`docker/docker-compose.yml` runs one `audiofp` service (SQLite on the `audiofp-data` volume) and, behind the `postgres` profile, a `postgres:16-alpine` service on the `audiofp-pg` volume. Values it interpolates from your shell (or a `.env` next to the compose file): `AUDIOFP_API_KEY` (required: compose refuses to start without it), `AUDIOFP_BIND` (host interface for port 5000, default `127.0.0.1`; set `0.0.0.0` to expose it on the network), `AUDIOFP_STORAGE_TYPE` (default `sqlite`), `AUDIOFP_POSTGRES_DSN` (default `postgresql://audiofp:audiofp@postgres:5432/audiofp`), `AUDIOFP_RECORDINGS` (host folder mounted read-only at `/recordings`, default `./recordings` relative to the `docker/` directory). It sets `AUDIOFP_PROFILE=production`, `AUDIOFP_INDEX_ROOTS=/recordings` and `AUDIOFP_LOG_FORMAT=json` and restarts the service unless stopped.

```bash
# SQLite
AUDIOFP_API_KEY=change-me AUDIOFP_RECORDINGS=/srv/recordings \
  docker compose -f docker/docker-compose.yml up -d --build

# PostgreSQL: the profile starts the database, the variable switches the backend
AUDIOFP_API_KEY=change-me AUDIOFP_STORAGE_TYPE=postgres AUDIOFP_RECORDINGS=/srv/recordings \
  docker compose -f docker/docker-compose.yml --profile postgres up -d --build
```

- `--profile postgres` alone only starts the database container; `AUDIOFP_STORAGE_TYPE=postgres` is what makes AudioFP use it.
- The `audiofp` service has no `depends_on` for `postgres`. On a cold start AudioFP may try to connect before PostgreSQL is ready, fail after the 15-second pool wait with `storage_error`, and be restarted by `restart: unless-stopped` until it succeeds.
- The bundled PostgreSQL password is `audiofp`. For anything but a local trial change `POSTGRES_PASSWORD` in the compose file and pass a matching `AUDIOFP_POSTGRES_DSN`.
- Use an absolute `AUDIOFP_RECORDINGS`; a relative value is resolved against the compose file's directory.
- The compose file does not set `AUDIOFP_LOG_FILE`, so the image's empty value applies and the container logs to the console only; add `AUDIOFP_LOG_FILE: /data/logs/audiofp.log` (or `auto`) to the service's `environment:` block (or a compose override file) if you want a rotating log file on the volume (see the image notes above).

## PostgreSQL setup

### When to choose it

SQLite is the default because a single process with a local file is the fastest and simplest setup. Move to PostgreSQL when you need more than one AudioFP process or host against the same library, when the library outgrows one disk, or when you want the database's own backup, replication and monitoring tooling. `fingerprint/storage/postgres_store.py` bulk-loads fingerprints with `COPY` and serves lookups from a covering index (`idx_fp_hash` on `hash_value INCLUDE (track_ref, time_offset)`).

There is no export/import between backends: switching means re-indexing from the source files.

### Steps

```bash
pip install -e ".[postgres]"                 # psycopg 3 with the binary wheel and connection pool
createdb -O audiofp audiofp                  # or: CREATE DATABASE audiofp OWNER audiofp;

export AUDIOFP_STORAGE_TYPE=postgres
export AUDIOFP_POSTGRES_DSN='postgresql://audiofp:secret@db.internal:5432/audiofp'
audiofp db check                             # opens the pool, creates the schema, verifies the signature
```

- `postgres_dsn` is required when `storage_type=postgres` (`Settings.validate` rejects the combination otherwise) and is a secret: `audiofp config` prints it as `***` and error messages redact the password.
- The schema (`meta`, `tracks`, `fingerprints` and their indexes) is created with `CREATE TABLE IF NOT EXISTS` on first connect, so the database must already exist and the role needs `CREATE` on its default schema. No migration tool is involved; `meta.schema_version` records the layout version (2).
- `AUDIOFP_POSTGRES_POOL_SIZE` (default 4) is the maximum pooled connections **per process**. Size `max_connections` on the server for `instances x pool size`.
- A connection failure surfaces as `storage_error` with a hint to check `AUDIOFP_POSTGRES_DSN`, reachability and that the database exists; the pool waits up to 15 seconds.
- The extra is missing: `storage_error` says `pip install 'audiofp[postgres]'`.
- To validate a server against the storage contract tests: `AUDIOFP_TEST_POSTGRES_DSN=postgresql://... pytest tests/test_storage.py` (needs `.[dev,postgres]`); CI does exactly this against `postgres:16-alpine`.

## Upgrading from 1.x

### The database is not compatible

2.0 changed the hash layout (`FINGERPRINT_ALGORITHM_VERSION = 2`) and the schema, so 1.x fingerprints cannot be upgraded in place. The default file name also changed: 1.x wrote `data/fingerprint.db` / `data/fingerprint_dev.db` / `data/database/fingerprint.db` depending on the config class, 2.x uses `<data_dir>/fingerprints.db`. Starting 2.0 with default settings therefore creates a fresh, empty database and leaves the old file untouched. If you point `AUDIOFP_SQLITE_PATH` at a 1.x file, `SQLiteStore` detects it (`user_version` 0 with a `songs` table and no `meta` table) and refuses with `fingerprint_incompatible`. Either way: re-index your library, for example `audiofp index /srv/recordings`, then delete the old file.

### The `config/` package is gone

`config/default.py`, `config/development.py` and `config/production.py` were removed. Everything is now a `Settings` field driven by `AUDIOFP_*` variables (or a `.env` file). Mapping for the values people typically changed:

| 1.x (`config/*.py`) | 2.x |
|---|---|
| `DEBUG`, `FLASK_ENV` | `AUDIOFP_PROFILE=development\|production\|testing`, `AUDIOFP_DEBUG` |
| `SAMPLE_RATE`, `N_FFT`, `HOP_LENGTH` | `AUDIOFP_SAMPLE_RATE`, `AUDIOFP_N_FFT`, `AUDIOFP_HOP_LENGTH` |
| `PEAK_NEIGHBORHOOD_SIZE`, `MIN_AMPLITUDE`, `FAN_VALUE` | `AUDIOFP_PEAK_NEIGHBORHOOD_SIZE`, `AUDIOFP_MIN_AMPLITUDE`, `AUDIOFP_FAN_VALUE` |
| `STORAGE_TYPE` | `AUDIOFP_STORAGE_TYPE` |
| `SQLITE_DATABASE_PATH` | `AUDIOFP_SQLITE_PATH` (default `<AUDIOFP_DATA_DIR>/fingerprints.db`) |
| `UPLOAD_FOLDER` | `AUDIOFP_UPLOAD_DIR` (default `<AUDIOFP_DATA_DIR>/uploads`) |
| `MAX_CONTENT_LENGTH` (bytes) | `AUDIOFP_MAX_UPLOAD_MB` (megabytes) |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | one `AUDIOFP_POSTGRES_DSN` |
| `LOG_LEVEL`, `LOG_FILE` | `AUDIOFP_LOG_LEVEL`, `AUDIOFP_LOG_FILE` (+ `AUDIOFP_LOG_FORMAT=json`) |
| `CORS_ORIGINS` | `AUDIOFP_CORS_ORIGINS` (comma-separated; off by default in every profile, 1.x development used `*`) |
| `NUM_WORKERS` | `AUDIOFP_INDEX_WORKERS` |
| `ALLOWED_EXTENSIONS` | fixed list in `fingerprint/formats.py`, not configurable |
| `BATCH_SIZE` | removed |

Run `audiofp config --describe` for the complete table.

### Launch commands

- `python run.py --env production` still works (translated to `audiofp serve --profile production`), but 1.x's `run.py` bound `0.0.0.0` by default while the 2.x `development` profile binds `127.0.0.1`; pass `--host 0.0.0.0` or `AUDIOFP_HOST` if you relied on LAN access during development.
- The 1.x gunicorn form `gunicorn "fingerprint.api.app:create_app('production')"` no longer works: `create_app`'s first positional parameter is now a `Settings` object. Use `AUDIOFP_PROFILE=production gunicorn fingerprint.api.wsgi:app --workers 1 --threads 8 --timeout 600` (see above).
- `librosa` and `gunicorn` are no longer required dependencies; decoding is `soundfile` + `soxr`, the production server is `waitress`. gunicorn remains available as the `[gunicorn]` extra.
- New in 2.x and off by default: `AUDIOFP_API_KEY`. 1.x had no authentication.

### Renamed endpoints

The 1.x paths remain as deprecated aliases (marked `deprecated: true` in `/api/v1/openapi.json`), so old clients keep working; update them at your convenience.

| 1.x | 2.x |
|---|---|
| `POST /api/v1/upload` | `POST /api/v1/tracks` |
| `POST /api/v1/index` | `POST /api/v1/tracks/index-directory` (production requires `AUDIOFP_INDEX_ROOTS`) |
| `GET /api/v1/songs` | `GET /api/v1/tracks` (response keeps `songs` and `count` next to `items` and `total`) |
| `GET /api/v1/songs/{id}` | `GET /api/v1/tracks/{id}` |
| `DELETE /api/v1/songs/{id}` | `DELETE /api/v1/tracks/{id}` |
| `GET /api/v1/songs/{id}/play` | `GET /api/v1/tracks/{id}/audio` (`/tracks/{id}/play` also accepted) |
| `GET /api/v1/stats` | unchanged; `total_songs` kept as an alias of `total_tracks` |

Error responses keep `error` as a plain string and add `code`, `status`, optional `details` and `request_id`. New endpoints: `PATCH /tracks/{id}`, `POST /tracks/bulk-delete`, `GET /tracks/{id}/stream-token`, `GET /tags`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}`, `GET /info`, `GET /openapi.json`.

## Health and monitoring

### `GET /api/v1/health`

No API key required. Returns HTTP 200 with `"status": "ok"` or 503 with `"status": "degraded"` when the storage check fails (`count_tracks()` for memory/SQLite, `SELECT 1` for PostgreSQL). Suitable for both liveness and readiness probes; the Docker image's `HEALTHCHECK` uses it.

```json
{
  "status": "ok",
  "version": "2.0.0",
  "uptime_sec": 8123.4,
  "storage": {"type": "sqlite", "ok": true},
  "ffmpeg": {"available": true, "version": "6.1.1"},
  "jobs": {"active": 0},
  "fingerprint": {"algorithm_version": 2, "signature": "0123456789abcdef"},
  "timestamp": 1758100000.0
}
```

`fingerprint.signature` must be identical on every instance that shares a database. `GET /api/v1/stats` is cheap (it never scans the fingerprint table) and safe to poll for `total_tracks`, `total_hashes`, `total_duration_sec`, `db_size_bytes`, `jobs_active` and, on SQLite, `pending_rows` (fingerprint rows still in the write buffer; a value that stays high with no job running would mean a flush failed - look for `Failed to write ... fingerprints` in the log). `GET /api/v1/info` exposes formats, limits (`max_upload_mb`, `max_query_seconds`, `max_top_k`) and feature flags (`auth_required`, `directory_indexing`, `index_roots`).

### Request ids in logs

Every request gets an id: the incoming `X-Request-ID` header (first 64 characters, accepted only if they consist of letters, digits, `.`, `_`, `:` and `-`) or a generated 12-character hex string. The id is returned in the `X-Request-ID` response header, included in every log line emitted during the request (text format: `2026-09-17 10:00:00 INFO    fingerprint.api.app [3f9c1a2b7d4e] POST /api/v1/search -> 200 (412 ms)`; JSON format: a `request_id` key) and in JSON error bodies as `request_id`. Unexpected 500s say so explicitly ("The request id below identifies it in the server logs"). Background work logs carry a `job_id` key in the JSON format only; the text format shows just the request id. When `access_log` is on (default) each `/api/` request produces one `METHOD path -> status (ms)` line at INFO. Werkzeug, waitress and urllib3 loggers are held at WARNING unless the level is DEBUG.

### Jobs directory

With `persist_jobs=true` (default) every job is written to `<data_dir>/jobs/<job_id>.json` (progress updates at most every 2 seconds, always on state changes). On start-up the files are reloaded, so `GET /api/v1/jobs` shows history across restarts; jobs that were `pending` or `running` when the process died are re-labelled `interrupted` with an explanatory `error`. Finished jobs beyond `job_history_limit` (200) are dropped together with their files; `DELETE /jobs/<id>` removes one (finished jobs only; cancel a running job first). `POST /tracks/index-directory` answers 429 when the number of active jobs reaches `max_concurrent_jobs x 4`.

Useful alert sources: the health endpoint's status, `jobs.active` stuck at a non-zero value with no progress in the job's `completed` counter, `job_error`/`storage_error` codes in the JSON log, and free space in `AUDIOFP_DATA_DIR`.

## Multi-instance caveats

- **The job manager is per process.** Jobs and their progress exist only in the process that accepted the request. Behind a load balancer, `GET /jobs/<id>` on another instance returns 404 and each instance's `jobs.active` counts only its own work. Use one instance for ingestion (uploads and folder jobs) and route the rest to search-only replicas, or use sticky sessions.
- **Never share `data_dir` between running instances.** On start-up `JobManager._load_history` marks every `pending`/`running` job file as `interrupted`, so a second instance starting against the same `jobs/` folder clobbers the first one's live jobs. Give each instance its own `AUDIOFP_DATA_DIR` (or `AUDIOFP_PERSIST_JOBS=false`).
- **Uploads are local to the instance that received them.** The track's `filepath` points into that instance's `upload_dir`; `GET /tracks/<id>/audio` from another instance returns `file_missing` unless `AUDIOFP_UPLOAD_DIR` is shared storage mounted at the identical path everywhere. The same holds for `AUDIOFP_INDEX_ROOTS`: folder paths are stored as-is and must resolve on every host.
- **Runtime search defaults are cached per process.** `PUT /settings` updates `runtime-settings.json` and the memory of the instance that handled it; others keep their values until restart. Prefer fixing `AUDIOFP_TOP_K`, `AUDIOFP_MIN_CONFIDENCE`, `AUDIOFP_MIN_ALIGNED_HASHES` and `AUDIOFP_MIN_PEAK_RATIO` in the environment for fleets.
- **Do not share a SQLite file across hosts.** WAL mode needs the `-shm` shared-memory file on one machine; over NFS/SMB it corrupts or locks. On a single host several processes can open the same file, but the write lock is per process and the fallback is a 30-second `busy_timeout`, so keep writes (indexing) in one process. The SQLite write buffer is per process too: a track another process has indexed but not yet flushed is visible in its `tracks` row (with `flushed=0`) but cannot be matched from your process until that process flushes (end of its run, buffer full, or shutdown). Multi-host means PostgreSQL.
- **PostgreSQL is the shared backend.** All instances point `AUDIOFP_POSTGRES_DSN` at the same database; the schema is created once and reused. Each instance holds up to `postgres_pool_size` connections. Every instance must run the same fingerprint parameters: the signature check at start-up (`fingerprint_compat=strict`) refuses a mismatch, which is the safeguard you want.
- **Health per instance.** Probe `/api/v1/health` on each instance rather than through the load balancer, since `jobs.active` and `uptime_sec` are instance-local.
