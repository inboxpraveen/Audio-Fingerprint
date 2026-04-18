# AudioFP - Audio Intelligence Platform

> Google-like audio search for your private audio library.  
> Upload a 3-second clip and instantly find where it appears across thousands of files.

AudioFP is a production-ready, self-hosted audio fingerprinting platform. It combines a high-accuracy Shazam-style matching engine with a clean web interface, letting you build a searchable audio knowledge base from your own files - no cloud, no transcription, no API keys required.

---

## Features

- **Short-clip search** - recognise audio from clips as short as 3 seconds, even with background noise
- **Exact & near-match** - Shazam-style spectral fingerprinting with time-offset voting
- **Match timestamp** - tells you exactly where in the original recording the clip comes from
- **Batch indexing** - upload individual files or point to a local folder; indexing runs in the background
- **Fast search** - batch hash lookups in a single SQL query; SQLite with WAL mode, covering index, and 64 MB page cache
- **500 MB upload limit** - handles large uncompressed WAV files out of the box
- **Simple UI** - drag-and-drop search, library management, and live stats in one page
- **REST API** - every feature is accessible via a JSON API for programmatic use

---

## Quick Start

**Prerequisites** — install [FFmpeg](https://ffmpeg.org/) (or [SoX](https://sourceforge.net/projects/sox/)) so the platform can decode all supported audio formats:

```bash
# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt-get install ffmpeg

# Windows (via Chocolatey)
choco install ffmpeg

# Windows (via Scoop)
scoop install ffmpeg
```

Verify the installation:

```bash
ffmpeg -version
```

Then:

```bash
# 1. Clone and install dependencies
git clone https://github.com/your-org/audio-fingerprint.git
cd audio-fingerprint
pip install -r requirements.txt

# 2. Start the server
python run.py

# 3. Open the browser
# → http://localhost:5000
```

The server creates `data/fingerprint_dev.db` on first run. Your library persists across restarts.

### Options

```bash
python run.py --port 8080                   # custom port
python run.py --env production --port 80    # production config
```

---

## How to Use

### Search
1. Go to the **Search** tab
2. Drop or browse an audio clip (3–30 seconds recommended)
3. Click **Search Library**
4. Results show song name, confidence %, and the exact timestamp match in the original file

### Build Your Library
1. Go to the **Library** tab
2. **Upload files** - drag-and-drop one or more audio files; each is indexed automatically
3. **Index a folder** - paste a local folder path and click **Index Folder** to bulk-index your entire music collection
4. Progress is shown live; indexed files appear in the grid below
5. Delete any file from the library with the trash button on its card

### Stats
The **Stats** tab shows total songs, total fingerprints, unique hashes, and the storage backend path.

---

## Supported Formats

MP3 · WAV · FLAC · M4A · OGG · AAC · WMA · OPUS

---

## REST API

The full API lives under `/api/v1`. See [docs/API.md](docs/API.md) for complete reference.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/search` | Search with an audio clip |
| `POST` | `/api/v1/upload` | Upload & index a single file |
| `POST` | `/api/v1/index` | Index all files in a directory |
| `GET`  | `/api/v1/jobs/<id>` | Job status and progress |
| `GET`  | `/api/v1/songs` | List all indexed songs |
| `GET`  | `/api/v1/songs/<id>` | Get song metadata |
| `DELETE` | `/api/v1/songs/<id>` | Remove a song |
| `GET`  | `/api/v1/stats` | Database statistics |
| `GET`  | `/api/v1/health` | Health check |

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_TYPE` | `sqlite` | `memory` · `sqlite` · `postgres` |
| `SQLITE_DATABASE_PATH` | `data/fingerprint_dev.db` | SQLite file path |
| `UPLOAD_FOLDER` | `data/uploads` | Where uploaded files are saved |
| `SAMPLE_RATE` | `11025` | Audio sample rate (Hz) |
| `FAN_VALUE` | `10` | Hash pairs per anchor peak (higher = more robust) |
| `MAX_CONTENT_LENGTH` | `500 MB` | Maximum upload size |

Edit `config/development.py` (dev) or `config/production.py` (prod) to change defaults.

---

## Architecture

```
Browser / API client
       │
  Flask REST API  (/api/v1/*)
       │
  ┌────┴────────────────────────────┐
  │  Fingerprint Pipeline            │
  │  Audio → STFT → Peaks → Hashes  │
  └────┬────────────────────────────┘
       │
  SQLite (WAL · covering index · batch lookup)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full algorithm walkthrough.

---

## License

MIT - see [LICENSE](LICENSE).
