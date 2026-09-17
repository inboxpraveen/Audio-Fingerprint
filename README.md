# AudioFP — self-hosted audio search & pattern detection

[![CI](https://github.com/inboxpraveen/Audio-Fingerprint/actions/workflows/ci.yml/badge.svg)](https://github.com/inboxpraveen/Audio-Fingerprint/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> Drop in a 3-second clip and find out **what** it is and **where** it comes from.
> Index a jingle, a compliance disclaimer or a piece of hold music and find **every place it occurs** across thousands of recordings — with timestamps.

<p align="center">
  <img src="assets/Header.png" alt="AudioFP" width="800" />
</p>

AudioFP is a production-ready, self-hosted audio fingerprinting service. It implements the landmark ("Shazam-style") algorithm on top of numpy/scipy, stores fingerprints in SQLite or PostgreSQL, and ships with a REST API, a CLI and a web UI. No cloud, no API keys, no transcription — it matches sound, not words, so it works on music, jingles, IVR prompts, hold music, ads and any other recorded audio.

## Highlights

- **Identify clips** — 3-second snippets, noisy phone recordings, video files (audio is extracted automatically).
- **Find all occurrences** — the search mode built for QA: index short *patterns*, search with long *recordings* (or vice versa) and get every occurrence with start/end times, in the track *and* in the query.
- **Honest scores** — every match carries `confidence`, `aligned_hashes` and `peak_ratio`, plus a *strong / likely / weak* label; thresholds are configurable per request, per server, or in the UI. Chance matches are rejected instead of being reported as "4 matches found".
- **Flat memory** — audio is decoded and fingerprinted in a stream. An hour-long recording costs a few tens of MB, not gigabytes.
- **Enterprise hygiene** — typed configuration from environment variables, API-key auth, folder allow-lists, uniform JSON errors with request ids, structured (JSON) logs, background jobs with progress/ETA/cancel/history, duplicate detection, a fingerprint-compatibility stamp that refuses to silently mismatch a database, OpenAPI spec, Docker image, CI on three OSes.
- **Formats** — WAV, FLAC, OGG/Opus, MP3, AIFF natively (libsndfile); M4A/AAC/WMA and every common video container with ffmpeg.
- **Small footprint** — pure Python + numpy/scipy/soundfile; no librosa/numba, no compiler needed to install.

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

Optional but recommended: install [ffmpeg](https://ffmpeg.org) (`winget install Gyan.FFmpeg`, `brew install ffmpeg`, `apt install ffmpeg`) to decode video files and M4A/AAC/WMA. Everything else works without it.

Index a folder and search from the command line:

```bash
audiofp index ./my-recordings --tags "campaign-a"   # progress bar, duplicates skipped, errors listed
audiofp search clip.wav                              # what is this?
audiofp search call-2026-09-17.wav --mode occurrences   # where do known patterns occur inside this call?
audiofp stats
```

Or use the web UI: drop a file (or record from the microphone), pick *Identify clip* or *Find all occurrences*, and play any match from the exact position it was found.

## The two search modes

| Mode | Question it answers | Typical use |
|---|---|---|
| `identify` | "What is this clip and where does it sit in the original?" | music recognition, deduplication, finding the source of a snippet |
| `occurrences` | "Everywhere the query and a track share audio" — every occurrence, with spans | QA on call recordings: was the compliance disclaimer played? how often did the hold music loop? which ad ran when? |

Offsets are signed. A positive offset means the query clip starts *inside* the track (`track_offset_sec`); a negative one means the track's content was found *inside* the query at `query_offset_sec` — index short patterns, then search with long recordings.

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

## How it works (in one paragraph)

Audio is decoded in chunks to 11.025 kHz mono, turned into a spectrogram, and reduced to its most prominent spectral peaks (a "constellation"). Pairs of nearby peaks become 36-bit hashes stored in an inverted index together with the frame they occur at. A query goes through the same pipeline; every shared hash votes for a time offset between query and track, and real matches produce a sharp spike in that vote histogram while coincidences do not. The spike's height (distinct aligned hashes), its sharpness relative to the histogram background (`peak_ratio`) and the fraction of the matched region it explains (`confidence`) are all reported. Details, parameters and the maths: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Document | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, the algorithm, storage layout, jobs |
| [docs/API.md](docs/API.md) | every endpoint with examples (interactive version at `/docs`) |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | all `AUDIOFP_*` settings, profiles, derived paths |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | production checklist, Docker, PostgreSQL, nginx, systemd, upgrading from 1.x |
| [docs/TUNING.md](docs/TUNING.md) | scores, thresholds, fingerprint parameters, calibration recipe |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | memory/throughput characteristics and sizing |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | symptom → cause → fix, error codes |
| [docs/EXTENDING.md](docs/EXTENDING.md) | using AudioFP as a library, QA recipes, adding backends/endpoints/jobs |
| [docs/ROADMAP.md](docs/ROADMAP.md) | where this is going (keyword search on audio, agent evaluation) |
| [CHANGELOG.md](CHANGELOG.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | |

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

All commands accept `--profile`, `--data-dir`, `--storage`, `--sqlite-path` and `--log-level`. `python -m fingerprint ...` and the legacy `python run.py` work too.

## REST API

Everything the UI does is available under `/api/v1` — search, upload, folder indexing, jobs, track metadata (title, artist, tags, custom fields), audio streaming with Range support, stats, runtime settings. Errors are uniform JSON (`error`, `code`, `status`, `details`, `request_id`); the request id is also in the `X-Request-ID` header and the server log line. Set `AUDIOFP_API_KEY` to require a key (`X-API-Key` or `Authorization: Bearer`).

```bash
curl -F "audio=@track.mp3" -F "tags=ads,q3" http://localhost:5000/api/v1/tracks     # 202 + job
curl http://localhost:5000/api/v1/jobs/<job_id>
curl "http://localhost:5000/api/v1/tracks?q=disclaimer&sort=duration&order=desc"
```

Full reference: [docs/API.md](docs/API.md) or `/docs` on a running server.

## Configuration

Everything is an environment variable with an `AUDIOFP_` prefix (or a `.env` file — see [.env.example](.env.example)):

```bash
AUDIOFP_PROFILE=production AUDIOFP_API_KEY=s3cret AUDIOFP_INDEX_ROOTS=/srv/calls audiofp serve
```

The fingerprint parameters are stamped into the database; changing them refuses to open an existing database rather than silently degrading matches (`audiofp db reset` re-stamps after you re-index). Reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Deployment

```bash
docker build -t audiofp -f docker/Dockerfile .
docker run -p 5000:5000 -v audiofp-data:/data -e AUDIOFP_API_KEY=s3cret audiofp
# or: AUDIOFP_API_KEY=s3cret docker compose -f docker/docker-compose.yml up --build   (add --profile postgres for PostgreSQL;
#     the key is required, and the port is published on 127.0.0.1 unless AUDIOFP_BIND=0.0.0.0)
```

The image bundles ffmpeg and runs waitress as a non-root user with a health check. Production checklist, reverse proxy, systemd, PostgreSQL and upgrade notes: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Project layout

```
fingerprint/
  config.py        typed settings from env (+ fingerprint signature)
  formats.py       supported formats (single source of truth)
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
pytest -q                 # ~80 tests in a few seconds
pytest -q -m slow         # 30-minute-file memory/scale test
ruff check . && ruff format --check .
```

Tests generate all audio synthetically, so the repository stays small and the suite runs on Windows, macOS and Linux (see `.github/workflows/ci.yml`; the PostgreSQL contract tests run in CI against a service container, and locally when `AUDIOFP_TEST_POSTGRES_DSN` is set).

## Where this is going

Today AudioFP searches *sound*. The roadmap adds *keyword and phrase search on audio* (speech recognition alongside the fingerprint index) and evaluation workflows for contact-centre QA — scoring agents on scripted phrases, detecting silence and hold patterns, flagging calls — on the same tracks, tags, jobs and UI. See [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/EXTENDING.md](docs/EXTENDING.md) for the plug-in points that already exist.

## Contributing & security

Bug reports and pull requests are welcome — read [CONTRIBUTING.md](CONTRIBUTING.md) first. Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
