# Deployment

How to get AudioFP from a laptop onto a production host. The names used below all exist in the code. Settings are fields of `Settings` in `fingerprint/config.py`, and each one has an environment variable `AUDIOFP_<FIELD_UPPER>`. Commands live in `fingerprint/cli.py` and endpoints in `fingerprint/api/routes/*.py`. Run `audiofp config --describe` for the full option table.

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
| Python 3.10 or newer | `requires-python = ">=3.10"` in `pyproject.toml`, and `audiofp doctor` fails below 3.10. CI runs 3.10, 3.12 and 3.13 on Linux and 3.12/3.13 on Windows and macOS. |
| Python packages | `flask`, `flask-cors`, `werkzeug`, `numpy`, `scipy`, `soundfile`, `soxr`, `waitress`. All of them are hard dependencies, so waitress (the production server) is always available. |
| ffmpeg (optional) | Only needed for video containers and for audio formats libsndfile can't read. WAV, FLAC, OGG/Opus, MP3, AIFF, AU, CAF and W64 work without it. |
| Disk | Fingerprint database plus uploaded originals. See the sizing hint below. |

### Formats that need ffmpeg

The decoder in `fingerprint/core/decoder.py` reads everything in `NATIVE_AUDIO_EXTENSIONS` through `soundfile` and libsndfile, with no external binary: `wav`, `wave`, `flac`, `ogg`, `oga`, `opus`, `mp3`, `aiff`, `aif`, `aifc`, `au`, `caf`, `w64`. MP3 needs libsndfile 1.1+, which the `soundfile` 0.12+ wheels bundle.

ffmpeg is required for:

- `FFMPEG_AUDIO_EXTENSIONS`: `m4a`, `aac`, `wma`, `amr`, `ac3`, `dts`, `mka`, `weba`
- `VIDEO_EXTENSIONS`: `mp4`, `mkv`, `avi`, `mov`, `wmv`, `flv`, `webm`, `m4v`, `mpeg`, `mpg`, `ts`, `mts`, `3gp`, `vob`. The audio track is extracted.

Without ffmpeg those files fail with the error code `ffmpeg_not_found` (HTTP 422), and everything else keeps working. `audiofp doctor` and the `ffmpeg.available` field of `GET /api/v1/health` tell you which case you're in. ffmpeg is looked up on `PATH`. To use a specific binary, set the plain environment variable `AUDIOFP_FFMPEG_BINARY`. The decoder reads it directly at import time, so it isn't a `Settings` field and doesn't show up in `audiofp config`.

```bash
# Windows                      macOS                    Debian/Ubuntu
winget install Gyan.FFmpeg     brew install ffmpeg      sudo apt install ffmpeg
```

### Disk sizing hint

Most of the disk goes to the `fingerprints` table. We measured it with the 2.0 SQLite layout at default fingerprint parameters on synthetic material: `audiofp index` of 40 one-minute files per kind, then `audiofp stats`. Speech and music-like material from the test suite's generator produced about 130 to 145 hashes per second of audio. Sparse sustained tones produced about 25, and white noise, the worst case, about 520 to 550. Each stored hash costs roughly 16 to 18 bytes, or about 15.5 after `audiofp db vacuum`. That works out to about 8 to 35 MB of database per hour of indexed audio, so 1,000 hours lands somewhere between 8 and 35 GB. Real recordings vary with how dense they are. Index a representative sample, read `Database size` from `audiofp stats` and scale linearly. The `db_size_bytes` it reports includes the `-wal` file.

Two other things use disk:

- Uploads are kept. Files sent to `POST /api/v1/tracks` stay in `<data_dir>/uploads` so the UI can play them. Only duplicates and failed uploads are removed, and `keep_failed_uploads` controls the latter. Budget for the size of the originals.
- Folder-indexed files stay where they are. `POST /api/v1/tracks/index-directory` and `audiofp index` record the file's path as `filepath` and leave the file in place. Searching keeps working if the file disappears, but `GET /tracks/<id>/audio` then returns `file_missing`.

Memory stays flat whatever the file length, because audio is decoded and fingerprinted in chunks of `chunk_seconds` (30 s). The opt-in scale test asserts less than 300 MB growth on a 30-minute file. SQLite uses one connection per thread, each with a `sqlite_cache_mb` page cache of 64 MB, plus one in-memory fingerprint write buffer per process. That buffer holds `sqlite_write_batch_rows` rows, 2,000,000 by default at about 16 bytes each, so roughly 32 MB when full. A default single node should be comfortable with 1 to 2 GB of RAM. That's a rule of thumb. We haven't measured it.

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

`requirements.txt` lists the same runtime dependencies as `pyproject.toml`, but installing from it doesn't install the package itself, so there is no `audiofp` command. Use the module form:

```bash
pip install -r requirements.txt
python -m fingerprint serve        # identical to `audiofp serve`
python run.py                      # compatibility launcher, see below
```

### Docker

`docker/Dockerfile` builds a `python:3.12-slim` image with ffmpeg included, so every supported format works without further setup. See [Docker and Compose](#docker-and-compose).

## Running

### `audiofp serve`

```bash
audiofp serve                                   # development: Flask dev server on http://127.0.0.1:5000
audiofp serve --profile production              # waitress on 0.0.0.0:5000 (set AUDIOFP_API_KEY first)
audiofp serve --port 8080 --host 0.0.0.0        # override bind address
audiofp serve --server waitress --threads 16    # force the server and thread count
```

Flags: `--host`, `--port`, `--server auto|flask|waitress` (default `auto`), `--threads`, plus the options shared by every command: `--profile`, `--data-dir`, `--storage memory|sqlite|postgres`, `--sqlite-path`, `--log-level`, `--quiet`.

`--server auto` picks Flask's development server when the profile is `development` and waitress otherwise. With waitress, `cmd_serve` passes `threads` (from `--threads` or `server_threads`, default 8), `channel_timeout=600` and a `max_request_body_size` equal to `max_upload_mb`, which defaults to 2048 MB. Use `--server flask` in production only for debugging.

The start-up banner shows the profile, the storage backend and the URLs for the UI at `/`, the API at `/api/v1` and the docs page at `/docs`. It also warns when the server listens on `0.0.0.0` without an API key.

### Profiles

A profile is a set of defaults applied before environment variables (`_profile_defaults` in `fingerprint/config.py`). Select one with `AUDIOFP_PROFILE` or `--profile`.

| Profile | Defaults it changes |
|---|---|
| `development` (default) | `debug=true`, `log_level=DEBUG`. The host stays at the field default `127.0.0.1` and `--server auto` picks the Flask dev server |
| `production` | `debug=false`, `log_level=INFO`, `host=0.0.0.0`, waitress, and directory indexing requires `index_roots`. The profile doesn't set `log_file`, but the default `log_file=auto` resolves to `<data_dir>/logs/audiofp.log` in this profile only (see `Settings.log_file_resolved`) |
| `testing` | `storage_type=memory`, `persist_jobs=false`, `log_level=WARNING`, `data_dir=data/test` |

Configuration is resolved in this order: field defaults, then the profile, then `AUDIOFP_*` environment variables, then CLI flags. The environment variables can also come from a `.env` file in the working directory, or from the file named by `AUDIOFP_ENV_FILE`. Real environment variables override `.env`, and only `AUDIOFP_*` keys are read from it. An empty value such as `AUDIOFP_LOG_FILE=`, in the environment or in `.env`, counts as a real value for text and list settings: it sets the string to `""` or the list to `[]`. That's how you disable the log file or clear `AUDIOFP_CORS_ORIGINS`. For numeric and boolean settings an empty value is ignored, so `AUDIOFP_PORT=` leaves the default in place. `audiofp config` prints the effective result with secrets shown as `***`, so it's safe to paste into a support ticket.

### `python -m fingerprint` and `run.py`

`python -m fingerprint <command>` is the same CLI (`fingerprint/__main__.py` calls `fingerprint.cli.main`). It's useful when the package was installed without the console script, or from a plain `requirements.txt` environment.

`run.py` is kept for 1.x muscle memory. It always runs `serve`, translates `--env <profile>` into `--profile <profile>` and passes every other argument through:

```bash
python run.py                      # audiofp serve
python run.py --port 8080          # audiofp serve --port 8080
python run.py --env production     # audiofp serve --profile production
```

### Bulk loading from the CLI

For the initial load, `audiofp index /srv/recordings --tags campaign-2026` fingerprints a folder with a progress bar and the same worker pool the API uses. `index_workers` sets the pool size, and `0` means min(4, CPU count). It's safe to run next to a serving instance. WAL readers never block a writer, and the server can match the new tracks once the CLI has flushed its write buffer, which it does every `sqlite_write_batch_rows` rows and at the end of the run. But don't run two processes that both index into the same SQLite file at the same time. The write lock is per process and the fallback is a 30-second `busy_timeout`.

## Production checklist

What to set, and why it matters.

| Item | Setting | Why |
|---|---|---|
| Profile | `AUDIOFP_PROFILE=production` | Turns off Flask debug, selects waitress and INFO logging, and requires `index_roots` for `POST /tracks/index-directory`. `audiofp doctor` warns about the two most common omissions, a missing API key and missing index roots. |
| API key | `AUDIOFP_API_KEY=<long random string>` | Without it anyone who can reach the port can index, search and delete. With it every `/api/v1` request must send `X-API-Key: <key>` or `Authorization: Bearer <key>`. `/api/v1/health` and `/api/v1/openapi.json` stay public. `<audio>` elements can't send headers, so `GET .../audio` and `.../play` also accept `?token=<stream token>`. That is a track-scoped token minted by `GET /api/v1/tracks/{id}/stream-token`, which needs the key. It is HMAC-signed with the key and valid for one hour. Key comparison is constant-time and the access log redacts `token` and `api_key` query values. The bundled UI asks for the key once and keeps it in the browser's local storage. One shared secret is all that's built in. Per-user keys, SSO and rate limiting belong in the reverse proxy. |
| Folder indexing | `AUDIOFP_INDEX_ROOTS=/srv/recordings,/mnt/archive` | Comma-separated allow-list for `directory_path` in `POST /tracks/index-directory`. In production the endpoint returns 403 until this is set. To accept uploads only, set `AUDIOFP_ALLOW_DIRECTORY_INDEXING=false`, which disables folder indexing in every profile (the route stays registered and always answers 403). `GET /api/v1/info` reports `features.directory_indexing` and `features.index_roots`. |
| Host binding | `AUDIOFP_HOST=127.0.0.1` behind a same-host proxy. Use `0.0.0.0`, the production default, only when the port itself should be reachable | Keeps the unencrypted, single-key service off the network when nginx terminates TLS in front of it. |
| Reverse proxy headers | `AUDIOFP_TRUST_PROXY=true` | Wraps the app in Werkzeug's `ProxyFix(x_for=1, x_proto=1, x_host=1)`, so Flask takes the client address, scheme and host from `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` as set by exactly one proxy hop. Leave it off when clients hit AudioFP directly, otherwise those headers can be spoofed. |
| Logging | `AUDIOFP_LOG_FORMAT=json`, `AUDIOFP_LOG_FILE=/var/lib/audiofp/logs/audiofp.log`, `AUDIOFP_LOG_MAX_MB=20`, `AUDIOFP_LOG_BACKUP_COUNT=5` | JSON is one object per line with the keys `ts`, `level`, `logger`, `message`, `request_id`, `job_id` and `exception`, for Loki, Datadog or CloudWatch. The file handler rotates. The console (stderr) always gets a copy. If the log directory can't be created or written, the server logs `Cannot write the log file ...; logging to the console only` and keeps running with console output only. The default `AUDIOFP_LOG_FILE=auto` resolves to `<AUDIOFP_DATA_DIR>/logs/audiofp.log` in the production profile and to console only in the others, so with an absolute data directory the log lands next to the database. Set the path explicitly if it should live elsewhere, or set it to `none` or an empty value to disable the file. `AUDIOFP_ACCESS_LOG=false` silences the per-request line. Its query strings have `token` and `api_key` values masked. |
| Data directory | `AUDIOFP_DATA_DIR=/var/lib/audiofp` on a persistent disk, absolute | Holds `fingerprints.db` plus its `-wal` and `-shm` files while open, `uploads/`, `jobs/` and `runtime-settings.json`, which stores search defaults changed through the UI or `PUT /settings`. `audiofp doctor` checks that it's writable and warns below 1 GB free. |
| Backups | see below | The SQLite database is in WAL mode: a raw copy of `fingerprints.db` alone can miss the newest writes. |
| Upload limit | `AUDIOFP_MAX_UPLOAD_MB=2048` (default) | Applied as Flask's `MAX_CONTENT_LENGTH` and waitress's `max_request_body_size`. Oversize requests get a 413 whose message names this variable. Your proxy's body limit must be at least as large. |
| Threads and workers | `AUDIOFP_SERVER_THREADS=8` (or `--threads`), `AUDIOFP_INDEX_WORKERS=0`, `AUDIOFP_MAX_CONCURRENT_JOBS=2` | Waitress threads serve requests. Index workers fingerprint files in parallel, and 0 means min(4, CPU count). Concurrent jobs share that pool. Search is CPU-bound in numpy, so more threads than cores buys little. |
| CORS | leave `AUDIOFP_CORS_ORIGINS` unset unless a separate frontend calls the API | The bundled UI is same-origin. No profile enables CORS by default. `*` allows any origin, so never combine it with a missing API key. |
| Fingerprint compatibility | keep `AUDIOFP_FINGERPRINT_COMPAT=strict` | The store records the fingerprint parameter signature. A mismatch would quietly degrade matching, so the default refuses to start. Change fingerprint parameters only together with `audiofp db reset --yes` and a full re-index. |
| Pre-flight | `audiofp doctor` and `audiofp db check` with the production environment loaded | Confirms Python, libsndfile formats, ffmpeg, writable directories, free space and that the database opens with a compatible signature. |

### Backups of the SQLite file

While the server is running, the database consists of `fingerprints.db`, `fingerprints.db-wal` and `fingerprints.db-shm`. Options, safest first:

```bash
# 1. Online and consistent, no downtime: SQLite's backup API
sqlite3 /var/lib/audiofp/fingerprints.db ".backup '/backups/fingerprints-$(date +%F).db'"

# 2. Stop the service, then copy every file that exists
systemctl stop audiofp
cp /var/lib/audiofp/fingerprints.db* /backups/
systemctl start audiofp
```

`audiofp db vacuum` runs `PRAGMA wal_checkpoint(TRUNCATE)` followed by `VACUUM`, which folds the WAL back into the main file and compacts it. It takes an exclusive lock and needs up to twice the database size in free space, so schedule it for a quiet window. There is no need to run it before every backup. Back up `uploads/` alongside the database if you rely on playback of uploaded files. PostgreSQL deployments use `pg_dump` as usual.

### gunicorn on Linux

`fingerprint/api/wsgi.py` exposes a ready-made application object built from the environment (`app = create_app()`):

```bash
pip install -e ".[gunicorn]"
AUDIOFP_PROFILE=production gunicorn "fingerprint.api.wsgi:app" \
  --bind 127.0.0.1:5000 --workers 1 --threads 8 --timeout 600
```

Keep one worker and scale with threads. Three things break with several workers:

- The job manager in `fingerprint/jobs/manager.py` is in-process. A `POST /tracks` handled by worker A creates a job only worker A knows about, and a `GET /jobs/<id>` routed to worker B answers 404.
- Runtime search defaults (`runtime-settings.json`) are read once at start-up, so `PUT /settings` on one worker does not reach the others until restart.
- The SQLite write lock is per process. Concurrent writers from several processes fall back to the 30-second `busy_timeout` and can fail with `storage_error`.

Don't use `--preload`. `create_app()` opens and uses a SQLite connection for the schema and signature check, and it builds the job thread pool. With `--preload` both happen in the master process, and SQLite connections are not safe to carry across a fork. We haven't tested that path. `--timeout 600` matches waitress's `channel_timeout`, because searching an hour-long recording or ingesting a 2 GB upload is one long request. gunicorn doesn't run on Windows. There, `audiofp serve --server waitress` is the production server. Run it under a service wrapper such as NSSM or Task Scheduler.

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

The `audiofp` user needs read access to every folder listed in `AUDIOFP_INDEX_ROOTS`. `systemctl stop` sends `SIGTERM`, which `audiofp serve` turns into a clean shutdown. Running jobs are cancelled at the next file boundary and their record ends as `cancelled`, the SQLite write buffer is flushed and the process exits, normally within a few seconds. Keep `TimeoutStopSec` above the time one file takes to fingerprint. Only a hard kill (`SIGKILL`, power loss) is ungraceful. On the next start the job is reported with status `interrupted` and the files whose fingerprints had reached the disk are kept.

SQLite buffers fingerprints in memory, up to `AUDIOFP_SQLITE_WRITE_BATCH_ROWS` rows with a default of 2,000,000, and writes them at the end of each run, on shutdown and whenever the buffer fills. Tracks still in that buffer when the process was killed are removed at the next start with the warning `Removed N track(s) whose fingerprints were lost in an unclean shutdown`. They get re-indexed when the folder is submitted again. Set `AUDIOFP_SQLITE_WRITE_BATCH_ROWS=0` if you would rather have every track written immediately.

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

        # Stream uploads through. Otherwise nginx buffers up to 2 GB to its own disk first.
        proxy_request_buffering off;
    }
}
```

AudioFP does not terminate TLS itself. Streaming (`GET /tracks/<id>/audio`) uses HTTP range requests, which nginx proxies unchanged.

## Docker and Compose

### Image

`docker/Dockerfile` (build context is the repository root):

- `python:3.12-slim` plus `ffmpeg` and `curl`, then `pip install .` of the package
- runs as the unprivileged user `audiofp` (uid 1000). `/data` is a declared volume owned by that user
- environment baked in: `AUDIOFP_PROFILE=production`, `AUDIOFP_HOST=0.0.0.0`, `AUDIOFP_PORT=5000`, `AUDIOFP_DATA_DIR=/data` and an empty `AUDIOFP_LOG_FILE=`, which means console only (see below)
- `EXPOSE 5000`, and a `HEALTHCHECK` that curls `/api/v1/health` every 30 s with a 20 s start period and 3 retries
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

A few things to know:

- Everything AudioFP writes lives under `/data`: the database, uploads, job history and runtime settings. Keep it on a named volume or a bind mount. A container without one loses the library when it's removed.
- Mount the folders you want to index read-only with `:ro` and list the mount point in `AUDIOFP_INDEX_ROOTS`. The paths you send to `POST /tracks/index-directory` are container paths such as `/recordings/...`. Indexed files are referenced in place, so the mount has to stay at the same path for playback to work. The container user is uid 1000, so the host files must be readable by that uid.
- The image sets `AUDIOFP_LOG_FILE=` to an empty value, which means console only. The container logs to stderr and `docker logs` shows everything. For a rotating file on the volume, set `AUDIOFP_LOG_FILE=/data/logs/audiofp.log` as in the example above, or `AUDIOFP_LOG_FILE=auto`, which resolves to the same path with `AUDIOFP_DATA_DIR=/data` and the production profile.
- Configuration is entirely `AUDIOFP_*` environment variables. There is nothing to mount under `/app`.
- `CMD` is in exec form, so `docker stop` delivers `SIGTERM` straight to `audiofp serve`, which cancels running jobs at the next file boundary, flushes the SQLite write buffer and exits. Docker kills the container 10 s after `SIGTERM` by default. Use `docker stop -t 60 audiofp` so a flush of a full buffer isn't cut short. The compose file sets `stop_grace_period: 60s` for the same reason.

### Compose

`docker/docker-compose.yml` runs one `audiofp` service with SQLite on the `audiofp-data` volume. Behind the `postgres` profile it also runs a `postgres:16-alpine` service on the `audiofp-pg` volume. It reads these values from your shell, or from a `.env` next to the compose file:

- `AUDIOFP_API_KEY`, required. Compose refuses to start without it.
- `AUDIOFP_BIND`, the host interface for port 5000. The default is `127.0.0.1`. Set `0.0.0.0` to expose it on the network.
- `AUDIOFP_STORAGE_TYPE`, default `sqlite`.
- `AUDIOFP_POSTGRES_DSN`, default `postgresql://audiofp:audiofp@postgres:5432/audiofp`.
- `AUDIOFP_RECORDINGS`, the host folder mounted read-only at `/recordings`. The default is `./recordings`, relative to the `docker/` directory.

The file itself sets `AUDIOFP_PROFILE=production`, `AUDIOFP_INDEX_ROOTS=/recordings` and `AUDIOFP_LOG_FORMAT=json`, and restarts the service unless stopped.

```bash
# SQLite
AUDIOFP_API_KEY=change-me AUDIOFP_RECORDINGS=/srv/recordings \
  docker compose -f docker/docker-compose.yml up -d --build

# PostgreSQL: the profile starts the database, the variable switches the backend
AUDIOFP_API_KEY=change-me AUDIOFP_STORAGE_TYPE=postgres AUDIOFP_RECORDINGS=/srv/recordings \
  docker compose -f docker/docker-compose.yml --profile postgres up -d --build
```

- `--profile postgres` on its own only starts the database container. `AUDIOFP_STORAGE_TYPE=postgres` is what makes AudioFP use it.
- The `audiofp` service has no `depends_on` for `postgres`. On a cold start AudioFP may try to connect before PostgreSQL is ready, fail after the 15-second pool wait with `storage_error`, and be restarted by `restart: unless-stopped` until it succeeds.
- The bundled PostgreSQL password is `audiofp`. For anything but a local trial, change `POSTGRES_PASSWORD` in the compose file and pass a matching `AUDIOFP_POSTGRES_DSN`.
- Use an absolute `AUDIOFP_RECORDINGS`. A relative value is resolved against the compose file's directory.
- The compose file doesn't set `AUDIOFP_LOG_FILE`, so the image's empty value applies and the container logs to the console only. If you want a rotating log file on the volume, add `AUDIOFP_LOG_FILE: /data/logs/audiofp.log` (or `auto`) to the service's `environment:` block, or put it in a compose override file. See the image notes above.

## PostgreSQL setup

### When to choose it

SQLite is the default because a single process with a local file is the simplest setup, and also the fastest. Move to PostgreSQL when you need more than one AudioFP process or host against the same library, when the library outgrows one disk, or when you want the database's own backup, replication and monitoring tooling. `fingerprint/storage/postgres_store.py` bulk-loads fingerprints with `COPY` and serves lookups from a covering index, `idx_fp_hash` on `hash_value INCLUDE (track_ref, time_offset)`.

There is no export/import between backends: switching means re-indexing from the source files.

### Steps

```bash
pip install -e ".[postgres]"                 # psycopg 3 with the binary wheel and connection pool
createdb -O audiofp audiofp                  # or: CREATE DATABASE audiofp OWNER audiofp;

export AUDIOFP_STORAGE_TYPE=postgres
export AUDIOFP_POSTGRES_DSN='postgresql://audiofp:secret@db.internal:5432/audiofp'
audiofp db check                             # opens the pool, creates the schema, verifies the signature
```

- `postgres_dsn` is required when `storage_type=postgres`, and `Settings.validate` rejects the combination without it. It's treated as a secret: `audiofp config` prints it as `***` and error messages redact the password.
- The schema (`meta`, `tracks`, `fingerprints` and their indexes) is created with `CREATE TABLE IF NOT EXISTS` on first connect, so the database must already exist and the role needs `CREATE` on its default schema. There is no migration tool. `meta.schema_version` records the layout version, which is 2.
- `AUDIOFP_POSTGRES_POOL_SIZE` (default 4) is the maximum number of pooled connections per process. Size `max_connections` on the server for `instances x pool size`.
- A connection failure surfaces as `storage_error` with a hint to check `AUDIOFP_POSTGRES_DSN`, reachability and that the database exists. The pool waits up to 15 seconds first.
- If the extra is missing, `storage_error` says `pip install 'audiofp[postgres]'`.
- To validate a server against the storage contract tests, run `AUDIOFP_TEST_POSTGRES_DSN=postgresql://... pytest tests/test_storage.py` with the `.[dev,postgres]` extras installed. CI does the same against `postgres:16-alpine`.

## Upgrading from 1.x

### The database is not compatible

2.0 changed the hash layout (`FINGERPRINT_ALGORITHM_VERSION = 2`) and the schema, so 1.x fingerprints can't be upgraded in place. The default file name changed too. 1.x wrote `data/fingerprint.db`, `data/fingerprint_dev.db` or `data/database/fingerprint.db` depending on the config class, and 2.x uses `<data_dir>/fingerprints.db`. So starting 2.0 with default settings creates a fresh, empty database and leaves the old file untouched. If you point `AUDIOFP_SQLITE_PATH` at a 1.x file, `SQLiteStore` recognises it by `user_version` 0 with a `songs` table and no `meta` table, and refuses with `fingerprint_incompatible`. Either way, re-index your library, for example with `audiofp index /srv/recordings`, then delete the old file.

### The `config/` package is gone

`config/default.py`, `config/development.py` and `config/production.py` were removed. Everything is now a `Settings` field driven by `AUDIOFP_*` variables or a `.env` file. Here is the mapping for the values people typically changed:

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
| `LOG_LEVEL`, `LOG_FILE` | `AUDIOFP_LOG_LEVEL`, `AUDIOFP_LOG_FILE`, plus `AUDIOFP_LOG_FORMAT=json` |
| `CORS_ORIGINS` | `AUDIOFP_CORS_ORIGINS` (comma-separated). Off by default in every profile. 1.x development used `*` |
| `NUM_WORKERS` | `AUDIOFP_INDEX_WORKERS` |
| `ALLOWED_EXTENSIONS` | a fixed list in `fingerprint/formats.py` with no setting to change it |
| `BATCH_SIZE` | removed |

Run `audiofp config --describe` for the complete table.

### Launch commands

- `python run.py --env production` still works and is translated to `audiofp serve --profile production`. But 1.x's `run.py` bound `0.0.0.0` by default, while the 2.x `development` profile binds `127.0.0.1`. Pass `--host 0.0.0.0` or set `AUDIOFP_HOST` if you relied on LAN access during development.
- The 1.x gunicorn form `gunicorn "fingerprint.api.app:create_app('production')"` fails in 2.x, because `create_app`'s first positional parameter is now a `Settings` object. Use `AUDIOFP_PROFILE=production gunicorn fingerprint.api.wsgi:app --workers 1 --threads 8 --timeout 600` (see above).
- 2.x dropped `librosa` and `gunicorn` from the required dependencies. Decoding uses `soundfile` and `soxr`, and the production server is `waitress`. gunicorn is still available as the `[gunicorn]` extra.
- New in 2.x and off by default: `AUDIOFP_API_KEY`. 1.x had no authentication.

### Renamed endpoints

The 1.x paths remain as deprecated aliases, marked `deprecated: true` in `/api/v1/openapi.json`, so old clients keep working. Update them when convenient.

| 1.x | 2.x |
|---|---|
| `POST /api/v1/upload` | `POST /api/v1/tracks` |
| `POST /api/v1/index` | `POST /api/v1/tracks/index-directory` (production requires `AUDIOFP_INDEX_ROOTS`) |
| `GET /api/v1/songs` | `GET /api/v1/tracks` (response keeps `songs` and `count` next to `items` and `total`) |
| `GET /api/v1/songs/{id}` | `GET /api/v1/tracks/{id}` |
| `DELETE /api/v1/songs/{id}` | `DELETE /api/v1/tracks/{id}` |
| `GET /api/v1/songs/{id}/play` | `GET /api/v1/tracks/{id}/audio` (`/tracks/{id}/play` also accepted) |
| `GET /api/v1/stats` | unchanged, with `total_songs` kept as an alias of `total_tracks` |

Error responses keep `error` as a plain string and add `code`, `status`, optional `details` and `request_id`. New endpoints: `PATCH /tracks/{id}`, `POST /tracks/bulk-delete`, `GET /tracks/{id}/stream-token`, `GET /tags`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}`, `GET /info`, `GET /openapi.json`.

## Health and monitoring

### `GET /api/v1/health`

No API key required. Returns HTTP 200 with `"status": "ok"`, or 503 with `"status": "degraded"` when the storage check fails. That check is `count_tracks()` for memory and SQLite and `SELECT 1` for PostgreSQL. It works for both liveness and readiness probes, and the Docker image's `HEALTHCHECK` uses it.

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

`fingerprint.signature` must be identical on every instance that shares a database. `GET /api/v1/stats` is cheap, since it never scans the fingerprint table, and safe to poll for `total_tracks`, `total_hashes`, `total_duration_sec`, `db_size_bytes` and `jobs_active`. On SQLite it also reports `pending_rows`, the fingerprint rows still in the write buffer. A value that stays high with no job running would mean a flush failed. Look for `Failed to write ... fingerprints` in the log. `GET /api/v1/info` exposes the formats, the limits `max_upload_mb`, `max_query_seconds` and `max_top_k`, and the feature flags `auth_required`, `directory_indexing` and `index_roots`.

### Request ids in logs

Every request gets an id. If the incoming `X-Request-ID` header is present, its first 64 characters are used, provided they consist only of letters, digits, `.`, `_`, `:` and `-`. Otherwise a 12-character hex string is generated. The id comes back in the `X-Request-ID` response header, appears in every log line emitted during the request, and is included in JSON error bodies as `request_id`. In the text log format a line looks like `2026-09-17 10:00:00 INFO    fingerprint.api.app [3f9c1a2b7d4e] POST /api/v1/search -> 200 (412 ms)`. In the JSON format it is a `request_id` key. Unexpected 500s say so explicitly ("The request id below identifies it in the server logs"). Background work logs carry a `job_id` key in the JSON format only, and the text format shows just the request id. When `access_log` is on, which is the default, each `/api/` request produces one `METHOD path -> status (ms)` line at INFO. Werkzeug, waitress and urllib3 loggers are held at WARNING unless the level is DEBUG.

### Jobs directory

With `persist_jobs=true`, the default, every job is written to `<data_dir>/jobs/<job_id>.json`. Progress is written at most every 2 seconds and always on a state change. On start-up the files are reloaded, so `GET /api/v1/jobs` shows history across restarts. Jobs that were `pending` or `running` when the process died are re-labelled `interrupted` with an explanatory `error`. Finished jobs beyond `job_history_limit` (200) are dropped together with their files. `DELETE /jobs/<id>` removes one, but only a finished one, so cancel a running job first. `POST /tracks/index-directory` answers 429 when the number of active jobs reaches `max_concurrent_jobs x 4`.

Things worth alerting on: the health endpoint's status, `jobs.active` stuck at a non-zero value while the job's `completed` counter doesn't move, `job_error` or `storage_error` codes in the JSON log, and free space in `AUDIOFP_DATA_DIR`.

## Multi-instance caveats

- The job manager is per process, so jobs and their progress exist only in the process that accepted the request. Behind a load balancer, `GET /jobs/<id>` on another instance returns 404 and each instance's `jobs.active` counts only its own work. Use one instance for ingestion (uploads and folder jobs) and route the rest to search-only replicas, or use sticky sessions.
- Never share `data_dir` between running instances. On start-up `JobManager._load_history` marks every `pending` or `running` job file as `interrupted`, so a second instance starting against the same `jobs/` folder clobbers the first one's live jobs. Give each instance its own `AUDIOFP_DATA_DIR`, or set `AUDIOFP_PERSIST_JOBS=false`.
- Uploads are local to the instance that received them. The track's `filepath` points into that instance's `upload_dir`, so `GET /tracks/<id>/audio` from another instance returns `file_missing` unless `AUDIOFP_UPLOAD_DIR` is shared storage mounted at the identical path everywhere. The same holds for `AUDIOFP_INDEX_ROOTS`: folder paths are stored as-is and must resolve on every host.
- `PUT /settings` updates `runtime-settings.json` and the memory of the instance that handled it, because runtime search defaults are cached per process. The others keep their values until restart. For a fleet, fix `AUDIOFP_TOP_K`, `AUDIOFP_MIN_CONFIDENCE`, `AUDIOFP_MIN_ALIGNED_HASHES` and `AUDIOFP_MIN_PEAK_RATIO` in the environment.
- Don't share a SQLite file across hosts. WAL mode needs the `-shm` shared-memory file on one machine, and over NFS or SMB it corrupts or locks. On a single host several processes can open the same file, but the write lock is per process and the fallback is a 30-second `busy_timeout`, so keep writes (indexing) in one process. The SQLite write buffer is per process too. A track another process has indexed but not yet flushed shows up in its `tracks` row with `flushed=0`, but your process can't match it until that other process flushes. That happens at the end of its run, when the buffer fills, or on shutdown. Multi-host means PostgreSQL.
- PostgreSQL is the shared backend. All instances point `AUDIOFP_POSTGRES_DSN` at the same database, and the schema is created once and reused. Each instance holds up to `postgres_pool_size` connections. Every instance must run the same fingerprint parameters. The signature check at start-up (`fingerprint_compat=strict`) refuses a mismatch. Leave it on.
- Probe `/api/v1/health` on each instance, bypassing the load balancer, since `jobs.active` and `uptime_sec` are instance-local.
