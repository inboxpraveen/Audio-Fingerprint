# Architecture Documentation

## System Overview

The Audio Fingerprint system is a production-ready audio search engine inspired by Shazam and Google Sound Search. It uses acoustic fingerprinting with spectral peak extraction and combinatorial hashing for fast and accurate song identification.

## Core Algorithm

### 1. Audio Processing Pipeline

```
Audio File -> Load & Resample -> Mono Conversion -> Normalization -> STFT -> Spectrogram
```

**Key Parameters:**
- Sample Rate: 11025 Hz (balance between quality and speed)
- FFT Window: 2048 samples
- Hop Length: 512 samples

### 2. Fingerprint Generation

The fingerprinting algorithm consists of three main steps:

#### Step 1: Spectrogram Computation
- Compute Short-Time Fourier Transform (STFT)
- Generate magnitude spectrogram (frequency x time)
- Apply logarithmic scaling for better dynamic range

#### Step 2: Spectral Peak Detection
- Find local maxima using maximum filter
- Apply amplitude threshold to filter weak peaks
- Extract peak coordinates: (time_index, frequency_index, amplitude)

#### Step 3: Combinatorial Hashing
- For each peak (anchor), pair with next N peaks (fan_value = 5)
- Create hash from: (freq1, freq2, time_delta)
- Hash encoding: `(f1 << 20) | (f2 << 10) | (time_delta & 0x3FF)`
- Store: (hash_value, time_offset, song_id)

### 3. Matching Algorithm

The matching process uses time-offset histogram analysis:

```python
1. Query database for each hash in query
2. Group matches by song_id
3. For each candidate song:
   - Calculate time_offset = db_time - query_time
   - Build histogram of time offsets
   - Score = max(histogram) / total_query_hashes
4. Return top K matches sorted by score
```

**Why This Works:**
- Correct matches will have consistent time offsets
- Noise and false matches will have random offsets
- The histogram peak indicates the true match

## Data Flow

### Indexing Flow

```
Audio Files
    |
    v
Dataset Loader (find audio files)
    |
    v
Indexer (parallel processing)
    |
    +-> Load Audio
    +-> Generate Fingerprint
    +-> Create Hashes
    +-> Store in Database
    |
    v
Storage Backend (Memory/SQLite/PostgreSQL)
```

### Query Flow

```
Query Audio
    |
    v
Load & Process
    |
    v
Generate Fingerprint
    |
    v
Create Hashes
    |
    v
Query Database
    |
    v
Match & Score
    |
    v
Return Top Matches
```

## Storage Schema

### Memory Store Structure

```python
hash_table = {
    hash_int: [(song_id, time_offset), ...],
    ...
}

song_metadata = {
    song_id: {
        'title': str,
        'artist': str,
        'filepath': str,
        'duration': float,
        'num_peaks': int,
        'num_hashes': int
    },
    ...
}
```

### SQLite Schema

**Songs Table:**
```sql
CREATE TABLE songs (
    song_id TEXT PRIMARY KEY,
    title TEXT,
    artist TEXT,
    filepath TEXT,
    duration REAL,
    metadata TEXT  -- JSON blob
)
```

**Fingerprints Table:**
```sql
CREATE TABLE fingerprints (
    hash_value INTEGER,
    song_id TEXT,
    time_offset INTEGER,
    FOREIGN KEY (song_id) REFERENCES songs(song_id)
)

CREATE INDEX idx_hash_value ON fingerprints(hash_value)
```

## Performance Characteristics

### Time Complexity

- **Indexing**: O(n) per song, where n is audio length
  - STFT: O(n log n)
  - Peak detection: O(m), where m is spectrogram size
  - Hash generation: O(p * f), where p is peaks, f is fan_value

- **Query**: O(h * log s), where h is query hashes, s is songs
  - Hash lookup: O(1) average with index
  - Scoring: O(h * m), where m is matches per hash

### Space Complexity

- **Per Song**: ~2000-5000 hashes (depends on duration)
- **Hash Size**: 8 bytes per hash entry
- **Metadata**: ~200 bytes per song
- **Total**: ~1-2 MB per 100 songs

### Scalability Estimates

| Songs | Hashes | Memory (RAM) | Query Time |
|-------|--------|--------------|------------|
| 1,000 | 3M | 24 MB | <50 ms |
| 10,000 | 30M | 240 MB | <100 ms |
| 100,000 | 300M | 2.4 GB | <200 ms |
| 1,000,000 | 3B | 24 GB | <500 ms |

## Component Architecture

### Core Module

```
core/
├── audio_processor.py    # Audio I/O and preprocessing
├── fingerprinter.py      # Peak detection algorithm
├── hash_generator.py     # Combinatorial hashing
└── matcher.py           # Matching and scoring
```

### Storage Module

```
storage/
├── base.py              # Abstract interface
├── memory_store.py      # In-memory (fast, volatile)
├── sqlite_store.py      # SQLite (persistent, single-node)
└── postgres_store.py    # PostgreSQL (distributed, scalable)
```

### API Module

```
api/
├── app.py              # Flask application factory
├── routes.py           # REST endpoints
├── validators.py       # Input validation
└── responses.py        # Response formatting
```

### Training Module

```
training/
├── indexer.py          # Batch indexing with parallelization
├── dataset_loader.py   # Audio file discovery
└── progress_tracker.py # Progress reporting
```

## Design Decisions

### Why 11025 Hz Sample Rate?
- Nyquist frequency of 5512 Hz covers most musical content
- 50% reduction in computation vs 22050 Hz
- Minimal impact on recognition accuracy

### Why Combinatorial Hashing?
- Robust to noise and distortion
- Time-invariant (works with clips from any position)
- Efficient storage and lookup

### Why Time-Offset Histogram?
- Simple and effective scoring method
- Naturally filters false positives
- No machine learning required

## Extension Points

### Adding New Storage Backends
1. Inherit from `StorageBackend`
2. Implement all abstract methods
3. Add to `create_app()` in `app.py`

### Custom Fingerprinting Algorithms
1. Create new class in `core/`
2. Implement `generate_fingerprint()` method
3. Return list of (time, freq, amplitude) tuples

### API Extensions
1. Add new routes in `routes.py`
2. Register blueprint in `app.py`
3. Add tests in `tests/test_api.py`

## Security Considerations

1. **File Upload**: Validate file types and sizes
2. **Path Traversal**: Use secure filename handling
3. **Resource Limits**: Set max file size and query limits
4. **Rate Limiting**: Implement in production (not included)
5. **Authentication**: Add if exposing publicly (not included)

## Monitoring and Logging

### Log Levels
- **DEBUG**: Detailed algorithm steps
- **INFO**: Request/response, indexing progress
- **WARNING**: Validation failures, recoverable errors
- **ERROR**: Processing failures, database errors

### Metrics to Track
- Query response time
- Match confidence scores
- Database size growth
- Error rates by endpoint
- Peak detection counts

## Future Optimizations

1. **Numba JIT Compilation**: Speed up peak detection
2. **Redis Caching**: Cache frequent query results
3. **Batch Query Processing**: Process multiple queries together
4. **GPU Acceleration**: Use CUDA for STFT computation
5. **Distributed Storage**: Shard hash table across nodes

