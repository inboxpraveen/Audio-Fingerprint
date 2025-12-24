# API Documentation

## Base URL

```
http://localhost:5000/api/v1
```

## Endpoints

### 1. Search for Song

Search for a song by uploading an audio clip.

**Endpoint:** `POST /api/v1/search`

**Content-Type:** `multipart/form-data`

**Parameters:**
- `audio` (file, required): Audio file to search (5-10 seconds recommended)

**Supported Formats:**
- MP3 (.mp3)
- WAV (.wav)
- FLAC (.flac)
- M4A (.m4a)
- OGG (.ogg)

**Request Example:**

```bash
curl -X POST \
  -F "audio=@query.mp3" \
  http://localhost:5000/api/v1/search
```

**Response Example:**

```json
{
  "matches": [
    {
      "song_id": "abc123",
      "title": "Song Title",
      "artist": "Artist Name",
      "confidence": 0.9523,
      "duration": 180.5,
      "filepath": "/path/to/song.mp3"
    },
    {
      "song_id": "def456",
      "title": "Another Song",
      "artist": "Another Artist",
      "confidence": 0.3421,
      "duration": 210.0,
      "filepath": "/path/to/another.mp3"
    }
  ],
  "query_duration_sec": 8.5,
  "processing_time_ms": 125.34,
  "found": true
}
```

**Response Fields:**
- `matches`: Array of matching songs (ordered by confidence)
  - `song_id`: Unique song identifier
  - `title`: Song title
  - `artist`: Artist name
  - `confidence`: Match confidence score (0-1)
  - `duration`: Song duration in seconds
  - `filepath`: Path to original audio file
- `query_duration_sec`: Duration of query audio
- `processing_time_ms`: Total processing time
- `found`: Boolean indicating if any matches were found

**Status Codes:**
- `200 OK`: Search completed successfully
- `400 Bad Request`: Invalid file or missing audio
- `500 Internal Server Error`: Processing error

---

### 2. Get All Songs

Retrieve list of all indexed songs.

**Endpoint:** `GET /api/v1/songs`

**Request Example:**

```bash
curl http://localhost:5000/api/v1/songs
```

**Response Example:**

```json
{
  "songs": [
    {
      "song_id": "abc123",
      "title": "Song Title",
      "artist": "Artist Name",
      "filepath": "/path/to/song.mp3",
      "duration": 180.5,
      "num_peaks": 1234,
      "num_hashes": 4567
    }
  ],
  "count": 1
}
```

**Status Codes:**
- `200 OK`: Request successful
- `500 Internal Server Error`: Database error

---

### 3. Get Song Details

Get metadata for a specific song.

**Endpoint:** `GET /api/v1/songs/<song_id>`

**Request Example:**

```bash
curl http://localhost:5000/api/v1/songs/abc123
```

**Response Example:**

```json
{
  "song_id": "abc123",
  "title": "Song Title",
  "artist": "Artist Name",
  "filepath": "/path/to/song.mp3",
  "duration": 180.5,
  "filename": "song.mp3",
  "num_peaks": 1234,
  "num_hashes": 4567
}
```

**Status Codes:**
- `200 OK`: Song found
- `404 Not Found`: Song ID does not exist
- `500 Internal Server Error`: Database error

---

### 4. Index Songs

Start indexing songs from a directory.

**Endpoint:** `POST /api/v1/index`

**Content-Type:** `application/json`

**Request Body:**

```json
{
  "directory_path": "/path/to/songs"
}
```

**Request Example:**

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"directory_path": "/path/to/songs"}' \
  http://localhost:5000/api/v1/index
```

**Response Example:**

```json
{
  "message": "Indexing job started",
  "directory": "/path/to/songs"
}
```

**Status Codes:**
- `202 Accepted`: Indexing job started
- `400 Bad Request`: Invalid directory path
- `500 Internal Server Error`: Indexing error

**Note:** This endpoint currently returns immediately. For production use, implement async job processing with status tracking.

---

### 5. Get Statistics

Get database statistics.

**Endpoint:** `GET /api/v1/stats`

**Request Example:**

```bash
curl http://localhost:5000/api/v1/stats
```

**Response Example:**

```json
{
  "total_songs": 1000,
  "total_hashes": 3500000,
  "unique_hashes": 2800000,
  "storage_type": "sqlite",
  "db_path": "./data/database/fingerprint.db"
}
```

**Response Fields:**
- `total_songs`: Number of indexed songs
- `total_hashes`: Total number of hash entries
- `unique_hashes`: Number of unique hash values
- `storage_type`: Storage backend type (memory/sqlite/postgres)
- `db_path`: Database file path (SQLite only)

**Status Codes:**
- `200 OK`: Request successful
- `500 Internal Server Error`: Database error

---

### 6. Health Check

Check if the API is running.

**Endpoint:** `GET /api/v1/health`

**Request Example:**

```bash
curl http://localhost:5000/api/v1/health
```

**Response Example:**

```json
{
  "status": "healthy",
  "timestamp": 1703001234.567
}
```

**Status Codes:**
- `200 OK`: Service is healthy

---

## Error Responses

All error responses follow this format:

```json
{
  "error": "Error message describing what went wrong",
  "status": 400
}
```

### Common Error Codes

**400 Bad Request:**
- No audio file provided
- Invalid file type
- File too large
- Missing required parameters

**404 Not Found:**
- Song ID not found
- Endpoint does not exist

**500 Internal Server Error:**
- Audio processing failure
- Database connection error
- Unexpected server error

---

## Rate Limiting

Currently, no rate limiting is implemented. For production deployment, consider adding:

- Per-IP rate limits
- API key authentication
- Request throttling

---

## CORS Configuration

CORS is enabled by default for all origins in development mode.

For production, configure allowed origins in `config/production.py`:

```python
CORS_ORIGINS = ['https://yourdomain.com']
```

---

## File Size Limits

**Maximum Upload Size:** 16 MB (configurable)

To change the limit, update `MAX_CONTENT_LENGTH` in configuration:

```python
MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB
```

---

## Performance Tips

1. **Query Audio Length:** 5-10 seconds provides best balance of speed and accuracy
2. **Audio Quality:** Lower quality audio (128 kbps MP3) works fine
3. **Concurrent Requests:** API supports concurrent requests
4. **Database Choice:** Use SQLite for <100k songs, PostgreSQL for larger datasets

---

## Python Client Example

```python
import requests

# Search for a song
with open('query.mp3', 'rb') as f:
    response = requests.post(
        'http://localhost:5000/api/v1/search',
        files={'audio': f}
    )
    
    if response.status_code == 200:
        data = response.json()
        if data['found']:
            best_match = data['matches'][0]
            print(f"Found: {best_match['title']} by {best_match['artist']}")
            print(f"Confidence: {best_match['confidence']:.2%}")
        else:
            print("No matches found")
    else:
        print(f"Error: {response.json()['error']}")
```

---

## JavaScript Client Example

```javascript
// Search for a song
const formData = new FormData();
formData.append('audio', audioFile);

fetch('http://localhost:5000/api/v1/search', {
  method: 'POST',
  body: formData
})
  .then(response => response.json())
  .then(data => {
    if (data.found) {
      const match = data.matches[0];
      console.log(`Found: ${match.title} by ${match.artist}`);
      console.log(`Confidence: ${(match.confidence * 100).toFixed(2)}%`);
    } else {
      console.log('No matches found');
    }
  })
  .catch(error => console.error('Error:', error));
```

