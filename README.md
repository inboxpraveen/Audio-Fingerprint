# AudioFP

[![CI](https://github.com/inboxpraveen/Audio-Fingerprint/actions/workflows/ci.yml/badge.svg)](https://github.com/inboxpraveen/Audio-Fingerprint/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

AudioFP is a self-hosted audio fingerprinting service. Give it a few seconds of audio and it tells you which track the clip came from and where in that track it sits. Index a jingle, a compliance disclaimer or a piece of hold music and it finds every place that audio turns up across your recordings, with timestamps.

<p align="center">
  <img src="assets/Header.png" alt="AudioFP" width="800" />
</p>

It works on the sound itself. There is no speech recognition involved and nothing leaves your machine, so it is just as happy with music as with IVR prompts, hold music, ads or anything else that was recorded once and played many times. The engine is the landmark ("Shazam") algorithm on numpy and scipy, with fingerprints stored in SQLite or PostgreSQL. You get a REST API, a command line and a small web UI.

## What it does

- Identifies clips as short as 3 seconds, including noisy phone recordings and video files (the audio is extracted for you).
- Finds every occurrence of a pattern. Index short patterns, search with a long recording, or the other way round, and you get start and end times in both the track and the query. This mode was built for QA on call recordings.
- Tells you how good a match is. Every match carries `confidence`, `aligned_hashes` and `peak_ratio`, plus a strong / likely / weak label. Thresholds can be set per request, per server or in the UI, and anything under them is dropped rather than reported as a match.
- Streams audio through the pipeline, so an hour-long recording takes a few tens of MB of memory.
- Covers the operational side: typed configuration from environment variables, API key auth, a folder allow-list, JSON errors with request ids, JSON logs, background jobs with progress and cancellation, duplicate detection, an OpenAPI spec, a Docker image and CI on Linux, Windows and macOS.
- Reads WAV, FLAC, OGG/Opus, MP3 and AIFF directly through libsndfile. With ffmpeg installed it also reads M4A, AAC, WMA and the usual video containers.
- Installs without a compiler. It is pure Python on numpy, scipy and soundfile; no librosa, no numba.

<p align="center">
  <img src="assets/Audio-Search-Light.png" alt="Search results with match timelines" width="800" />
</p>

## Quick start

```bash
git clone https://github.com/inboxpraveen/Audio-Fingerprint.git
cd Audio-Fingerprint
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .                                   # or: pip install -r requirements.txt

audiofp doctor                                     # checks Python, libsndfile, ffmpeg, storage
audiofp serve                                      # UI + API at http://localhost:5000
```

ffmpeg is optional but worth having (`winget install Gyan.FFmpeg`, `brew install ffmpeg`, `apt install ffmpeg`). Without it you can't decode video files or M4A/AAC/WMA. Everything else works.

Index a folder and search from the terminal:

```bash
audiofp index ./my-recordings --tags "campaign-a"   # progress bar, duplicates skipped, errors listed
audiofp search clip.wav                              # which track is this?
audiofp search call-2026-09-17.wav --mode occurrences   # which known patterns occur in this call, and where?
audiofp stats
```

Or open the web UI, drop a file in (or record from the microphone), pick a search mode and play any match from the exact spot it was found.

## The two search modes

| Mode | What you get | Typical use |
|---|---|---|
| `identify` | The best match per track: which track the clip is from and where it sits in it | music recognition, deduplication, tracing where a snippet came from |
| `occurrences` | Every place the query and a track share audio, with start and end times | QA on call recordings: did the disclaimer play, how many times did the hold music loop, which ad ran when |

Offsets are signed. A positive offset means the query clip starts somewhere inside the track (`track_offset_sec`). A negative one means the track's content was found inside the query, at `query_offset_sec`. The second case is the usual one for QA: index short patterns, then search with whole recordings.

```bash
curl -F "audio=@call.wav" -F mode=occurrences http://localhost:5000/api/v1/search
```

```json
{
  "found": true,
  "matches": [{
    "title": "Compliance disclaimer v3", "quality": "strong",
    "confidence": 0.81, "aligned_hashes": 351, "peak_ratio": 132.3,
    "occurrences": [
      {"query_offset_sec": 30.0, "query_start_sec": 30.0, "query_end_sec": 33.8, "aligned_hashes": 351, "quality": "strong"},
      {"query_offset_sec": 53.96, "query_start_sec": 54.5, "query_end_sec": 57.8, "aligned_hashes": 200, "quality": "strong"}
    ]
  }]
}
```

## How it works

Audio is decoded in chunks to 11.025 kHz mono and turned into a spectrogram, which is then reduced to its most prominent peaks. Pairs of nearby peaks become 36-bit hashes, stored in an inverted index together with the frame they occur at. A query goes through the same steps. Every hash the query shares with a track votes for a time offset between the two, and a real match shows up as a sharp spike in that vote histogram, while coincidences only add noise. The height of the spike (distinct aligned hashes), how sharp it is compared with the background (`peak_ratio`) and how much of the matched region it explains (`confidence`) are all reported. The details, including the parameters and the maths, are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Document | Covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, the algorithm, storage layout, jobs |
| [docs/API.md](docs/API.md) | every endpoint with examples (interactive version at `/docs`) |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | all `AUDIOFP_*` settings, profiles, derived paths |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | production checklist, Docker, PostgreSQL, nginx, systemd, upgrading from 1.x |
| [docs/TUNING.md](docs/TUNING.md) | scores, thresholds, fingerprint parameters, how to calibrate |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | memory and throughput, sizing |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptoms, causes and fixes, error codes |
| [docs/EXTENDING.md](docs/EXTENDING.md) | using AudioFP as a library, QA recipes, adding backends, endpoints and jobs |
| [docs/ROADMAP.md](docs/ROADMAP.md) | what comes next (keyword search on audio, agent evaluation) |
| [CHANGELOG.md](CHANGELOG.md), [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) | |

## CLI

```
audiofp serve      [--host] [--port] [--profile development|production] [--server flask|waitress]
audiofp index      PATH... [--no-recursive] [--workers N] [--tags a,b] [--json]
audiofp search     CLIP [--mode identify|occurrences] [--top-k N] [--min-confidence X] [--min-peak-ratio X] [--json]
audiofp tracks     list [--q text] | show ID | delete ID... [--yes]
audiofp stats      [--full]
audiofp doctor
audiofp config     [--describe]
audiofp db         check | reset --yes | vacuum
```

All commands accept `--profile`, `--data-dir`, `--storage`, `--sqlite-path` and `--log-level`. `python -m fingerprint ...` works too, and so does the old `python run.py`.

## REST API

The UI only talks to the public API, so anything you can do there you can do with curl under `/api/v1`: search, upload, folder indexing, jobs, track metadata (title, artist, tags, custom fields), audio streaming with Range support, stats and runtime settings. Errors are JSON with `error`, `code`, `status`, `details` and `request_id`. The same id is in the `X-Request-ID` header and in the server log line. Set `AUDIOFP_API_KEY` to require a key, sent as `X-API-Key` or `Authorization: Bearer`.

```bash
curl -F "audio=@track.mp3" -F "tags=ads,q3" http://localhost:5000/api/v1/tracks     # 202 + job
curl http://localhost:5000/api/v1/jobs/<job_id>
curl "http://localhost:5000/api/v1/tracks?q=disclaimer&sort=duration&order=desc"
```

The full reference is [docs/API.md](docs/API.md), or open `/docs` on a running server.

## Configuration

Everything is an environment variable with an `AUDIOFP_` prefix, or a line in a `.env` file (see [.env.example](.env.example)):

```bash
AUDIOFP_PROFILE=production AUDIOFP_API_KEY=s3cret AUDIOFP_INDEX_ROOTS=/srv/calls audiofp serve
```

The fingerprint parameters are stamped into the database. If you change them, AudioFP refuses to open the old database, because the stored fingerprints would no longer line up with freshly computed ones. `audiofp db reset` wipes it and stamps the new parameters so you can re-index. The full list is in [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Deployment

```bash
docker build -t audiofp -f docker/Dockerfile .
docker run -p 5000:5000 -v audiofp-data:/data -e AUDIOFP_API_KEY=s3cret audiofp
```

Or with compose: `AUDIOFP_API_KEY=s3cret docker compose -f docker/docker-compose.yml up --build`. Add `--profile postgres` to run PostgreSQL next to it. The key is required, and the port is only published on 127.0.0.1 unless you set `AUDIOFP_BIND=0.0.0.0`.

The image bundles ffmpeg, runs waitress as a non-root user and has a health check. The production checklist, reverse proxy and systemd examples, PostgreSQL setup and the notes on upgrading from 1.x are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Project layout

```
fingerprint/
  config.py        typed settings from env (+ fingerprint signature)
  formats.py       supported formats (the only place extensions are listed)
  core/            decoder (streaming), fingerprinter (chunked STFT + peaks), hash_generator, matcher
  storage/         StorageBackend contract; memory, sqlite, postgres backends
  indexing/        folder scanner, Indexer (dedupe, error isolation, cancellation), progress
  jobs/            JobManager (bounded pool, persistence, cancel, history)
  api/             Flask app factory, Runtime, routes/, auth, errors, OpenAPI
  cli.py           the `audiofp` command
  static/          web UI (vanilla HTML/CSS/JS, no build step) + /docs page
tests/             pytest suite (synthetic audio, no binary fixtures); `-m slow` for the scale test
docs/              guides; docker/ image + compose; .github/ CI + templates
```

## Development

```bash
pip install -e ".[dev]"
pytest -q                 # the whole suite runs in about ten seconds
pytest -q -m slow         # 30-minute-file memory and scale test
ruff check . && ruff format --check .
```

All test audio is generated on the fly, so there are no binary fixtures in the repository and the suite behaves the same on Windows, macOS and Linux. CI runs it on all three (`.github/workflows/ci.yml`) and also runs the PostgreSQL contract tests against a service container. Locally those run when `AUDIOFP_TEST_POSTGRES_DSN` is set.

## Roadmap

Right now AudioFP searches sound. The next step is keyword and phrase search on audio, with speech recognition running next to the fingerprint index, and on top of that the QA workflows contact centres need: scoring agents against scripted phrases, spotting silence and hold patterns, flagging calls. All of it on the same tracks, tags, jobs and UI. [docs/ROADMAP.md](docs/ROADMAP.md) has the plan and [docs/EXTENDING.md](docs/EXTENDING.md) describes the extension points that exist today.

## Contributing and security

Bug reports and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first. For security problems follow [SECURITY.md](SECURITY.md) and don't open a public issue.

## Licence

MIT, see [LICENSE](LICENSE).
