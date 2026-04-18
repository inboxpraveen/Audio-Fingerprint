# Architecture

## Overview

AudioFP is a single-process Python service. Every component - web server, fingerprint engine, and storage - runs in one process. Background indexing jobs use daemon threads.

```
┌─────────────────────────────────────────────────┐
│  Browser / REST client                           │
└────────────────────┬────────────────────────────┘
                     │ HTTP
┌────────────────────▼────────────────────────────┐
│  Flask (threaded)                                │
│  ┌──────────────────────────────────────────┐   │
│  │  /api/v1/*  routes (routes.py)           │   │
│  └──────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────┐   │
│  │  Static frontend (fingerprint/static/)   │   │
│  └──────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│  Fingerprint Pipeline                            │
│                                                  │
│  load_audio → preprocess → STFT spectrogram      │
│  → spectral peak extraction → hash generation    │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│  SQLiteStore                                     │
│  WAL · covering index · thread-local conns       │
│  batch hash lookup (IN clause, not N queries)    │
└─────────────────────────────────────────────────┘
```

---

## Fingerprint Algorithm

The algorithm is a direct implementation of the approach described in Wang (2003), the paper behind Shazam.

### 1. Spectrogram

```
audio  →  librosa.stft(n_fft=2048, hop_length=512)  →  magnitude spectrogram
```

The signal is loaded at 11,025 Hz (mono), producing a 1025 × T magnitude spectrogram. The low sample rate keeps processing fast while retaining all perceptually relevant frequency information.

### 2. Spectral Peak Extraction

```python
log_spec  = log1p(spectrogram)
local_max = maximum_filter(log_spec, size=20) == log_spec   # local maxima
above_thr = log_spec > log1p(min_amplitude)                  # amplitude gate
peaks     = where(local_max & above_thr)                     # (freq, time) pairs
```

A 20×20 neighbourhood filter finds the most prominent spectral events - the "constellation map". Only peaks above a minimum amplitude threshold are kept, discarding quiet noise.

### 3. Combinatorial Hash Generation

For each anchor peak, the next `fan_value` (default 10) peaks in time are paired with it:

```
hash = (anchor_freq << 20) | (target_freq << 10) | (time_delta & 0x3FF)
```

Each hash encodes two frequencies and the time gap between them in a single 32-bit integer. The anchor's time position is stored alongside the hash for offset alignment.

With `fan_value=10`, a typical 3-minute song produces ~100K–500K hashes.

### 4. Search / Matching

```
query_clip  →  same pipeline  →  query_hashes

# One batch SQL query instead of N individual queries
db_results = SELECT hash_value, song_id, time_offset
             FROM fingerprints
             WHERE hash_value IN (h1, h2, … hN)

for each (hash, song_id, db_time) in db_results:
    time_offset = db_time - query_time
    candidate_offsets[song_id].append(time_offset)

for song_id, offsets in candidates:
    best_offset, count = Counter(offsets).most_common(1)[0]
    confidence = count / total_query_hashes
    match_time  = best_offset * hop_length / sample_rate
```

The key insight: if two recordings share the same audio segment, their hashes will align at a consistent time offset. The most common offset in the histogram identifies the match, and its absolute value gives the position in the original recording.

---

## Storage

### Schema

```sql
CREATE TABLE songs (
    song_id    TEXT PRIMARY KEY,
    title      TEXT,
    artist     TEXT,
    filepath   TEXT,
    duration   REAL,
    metadata   TEXT,   -- full JSON blob
    indexed_at REAL
);

CREATE TABLE fingerprints (
    hash_value  INTEGER NOT NULL,
    song_id     TEXT    NOT NULL,
    time_offset INTEGER NOT NULL
);

-- Primary lookup index
CREATE INDEX idx_hash       ON fingerprints (hash_value);
-- Covering index - lookups never touch the table heap
CREATE INDEX idx_hash_cover ON fingerprints (hash_value, song_id, time_offset);
```

### SQLite Tuning

| Pragma | Value | Reason |
|--------|-------|--------|
| `journal_mode` | `WAL` | Concurrent reads during writes |
| `synchronous` | `NORMAL` | Faster commits, still crash-safe |
| `cache_size` | `-65536` (64 MB) | Hot index pages stay in memory |
| `mmap_size` | `268435456` (256 MB) | OS-level memory-mapped reads |
| `temp_store` | `MEMORY` | Temp tables in RAM |

Thread-local connections mean each Flask worker thread reuses its own connection rather than opening/closing one per request.

---

## Background Indexing

When a file is uploaded or a directory is submitted for indexing, the route handler:

1. Creates a job entry in `app.jobs` (an in-memory dict)
2. Starts a daemon thread running `_run_index_job`
3. Returns `202 Accepted` with the `job_id`

The thread updates `app.jobs[job_id]` directly (safe due to Python's GIL for dict operations). The frontend polls `GET /api/v1/jobs/<id>` every 1.5 seconds until `status` is `completed` or `failed`.

For directory indexing, `Indexer.index_directory` uses a `ThreadPoolExecutor` (default 4 workers) to parallelise per-file fingerprinting.

---

## Key Files

| Path | Role |
|------|------|
| `run.py` | Entry point - starts the Flask dev server |
| `fingerprint/api/app.py` | Flask app factory |
| `fingerprint/api/routes.py` | All REST endpoints |
| `fingerprint/core/audio_processor.py` | Audio loading and STFT |
| `fingerprint/core/fingerprinter.py` | Peak extraction |
| `fingerprint/core/hash_generator.py` | Combinatorial hash generation |
| `fingerprint/core/matcher.py` | Batch lookup and offset scoring |
| `fingerprint/storage/sqlite_store.py` | Optimised SQLite backend |
| `fingerprint/storage/memory_store.py` | In-memory backend (testing) |
| `fingerprint/training/indexer.py` | Single-file and directory indexer |
| `fingerprint/static/index.html` | Single-file frontend SPA |
| `config/default.py` | Base configuration |
