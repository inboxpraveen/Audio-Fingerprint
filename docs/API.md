# API Reference

All endpoints are prefixed with `/api/v1`.  
Request bodies use `application/json` unless otherwise noted.  
All responses are JSON.

---

## Search

### `POST /api/v1/search`

Search the knowledge base with an audio clip.

**Request** - `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `audio` | file | ✓ | Audio clip to search with |

**Response `200`**

```json
{
  "found": true,
  "query_duration_sec": 4.23,
  "processing_time_ms": 187.4,
  "matches": [
    {
      "song_id": "3f2a1b...",
      "confidence": 0.8732,
      "match_offset_sec": 42.5,
      "title": "Artist - Song Title",
      "artist": "Artist",
      "duration": 214.8,
      "filename": "song.mp3",
      "num_hashes": 12400
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `confidence` | `0.0 – 1.0` fraction of query hashes that align at the best time offset |
| `match_offset_sec` | Position in the original song (seconds) where the clip best matches |

**Error `400`** - no file, unsupported format, or file too large  
**Error `500`** - processing failure

---

## Upload a File

### `POST /api/v1/upload`

Upload and index a single audio file. Returns immediately with a job ID; poll `/jobs/<id>` for progress.

**Request** - `multipart/form-data`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `audio` | file | ✓ | Audio file to index |

**Response `202`**

```json
{
  "job_id": "a1b2c3...",
  "message": "Indexing started",
  "filename": "track.mp3"
}
```

---

## Index a Directory

### `POST /api/v1/index`

Index all audio files found recursively in a local directory. Returns immediately with a job ID.

**Request body**

```json
{ "directory_path": "/absolute/path/to/music" }
```

**Response `202`**

```json
{
  "job_id": "d4e5f6...",
  "message": "Indexing started for 47 files",
  "total_files": 47
}
```

**Error `400`** - missing path, path not found, or no audio files in directory

---

## Job Status

### `GET /api/v1/jobs/<job_id>`

Poll indexing progress.

**Response `200`**

```json
{
  "status": "running",
  "type": "upload",
  "filename": "track.mp3",
  "total": 1,
  "completed": 0,
  "current_file": "track.mp3",
  "created_at": 1713300000.0,
  "started_at": 1713300001.2
}
```

| `status` | Meaning |
|----------|---------|
| `pending` | Job queued, not yet started |
| `running` | Actively indexing |
| `completed` | Finished; `result` field present |
| `failed` | Error; `error` field present |

When `status` is `completed`:

```json
{
  "status": "completed",
  "result": {
    "total": 47,
    "success": 45,
    "failed": 2,
    "errors": [
      { "file": "/path/bad.mp3", "error": "unsupported format" }
    ]
  },
  "completed_at": 1713300120.5
}
```

**Error `404`** - job not found

---

## Songs

### `GET /api/v1/songs`

List all indexed songs (newest first).

**Response `200`**

```json
{
  "count": 3,
  "songs": [
    {
      "song_id": "...",
      "title": "Track Name",
      "artist": "Artist",
      "filename": "track.mp3",
      "duration": 214.8,
      "num_peaks": 1840,
      "num_hashes": 12400,
      "indexed_at": 1713300120.5
    }
  ]
}
```

---

### `GET /api/v1/songs/<song_id>`

Get metadata for a single song.

**Response `200`** - same shape as an element in the list above  
**Error `404`** - song not found

---

### `DELETE /api/v1/songs/<song_id>`

Remove a song and all its fingerprints from the knowledge base.

**Response `200`**

```json
{ "message": "Song deleted", "song_id": "..." }
```

**Error `404`** - song not found

---

## Statistics

### `GET /api/v1/stats`

**Response `200`**

```json
{
  "total_songs": 124,
  "total_hashes": 1532800,
  "unique_hashes": 918430,
  "storage_type": "sqlite",
  "db_path": "data/fingerprint_dev.db"
}
```

---

## Health

### `GET /api/v1/health`

Liveness probe.

**Response `200`**

```json
{ "status": "healthy", "timestamp": 1713300000.0 }
```
