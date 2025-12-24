# Audio Fingerprint

![Header](assets/Header.png)

Ultra-fast local audio fingerprinting search system inspired by Shazam and Google Sound Search.

## Overview

Audio Fingerprint is a production-ready audio identification system that uses acoustic fingerprinting with spectral peak extraction and combinatorial hashing. Identify songs from short audio clips in milliseconds with high accuracy.

## Key Features

- **Fast**: Query 10-second clips in <100ms against 10,000 songs
- **Accurate**: 95%+ recognition rate with noise and distortion
- **Scalable**: Handle up to 1 million songs on a single server
- **Simple**: Easy deployment with SQLite or in-memory storage
- **Production-Ready**: Docker support, logging, metrics, error handling
- **REST API**: Clean HTTP API for easy integration

## Quick Start

### Installation

```bash
# Clone repository
git clone <repository-url>
cd Audio-Fingerprint

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install FFmpeg (required for audio processing)
# Ubuntu/Debian: sudo apt-get install ffmpeg libsndfile1
# macOS: brew install ffmpeg
# Windows: Download from https://ffmpeg.org
```

### Index Your Songs

```bash
# Create data directory
mkdir -p data/songs

# Copy your audio files to data/songs/

# Index the songs
python scripts/train_index.py --songs-dir ./data/songs --workers 4
```

### Start the Server

```bash
# Start Flask development server
python -m fingerprint.api.app

# Server runs at http://localhost:5000
```

### Search for a Song

```bash
# Using curl
curl -X POST -F "audio=@query.mp3" http://localhost:5000/api/v1/search

# Using Python
import requests

with open('query.mp3', 'rb') as f:
    response = requests.post(
        'http://localhost:5000/api/v1/search',
        files={'audio': f}
    )
    print(response.json())
```

## Architecture

![Architecture](assets/Architecture.png)

### How It Works

1. **Audio Processing**: Load audio, resample to 11025 Hz, convert to mono
2. **Fingerprinting**: Extract spectral peaks using STFT and local maxima detection
3. **Hashing**: Generate combinatorial hashes from peak pairs
4. **Matching**: Query database and score candidates using time-offset histograms

### Core Algorithm

```
Audio -> STFT -> Spectral Peaks -> Combinatorial Hashes -> Database
Query -> Same Process -> Match Hashes -> Score by Time Alignment -> Results
```

## API Endpoints

### Search for Song

```bash
POST /api/v1/search
Content-Type: multipart/form-data

# Response
{
  "matches": [
    {
      "song_id": "abc123",
      "title": "Song Title",
      "artist": "Artist Name",
      "confidence": 0.95
    }
  ],
  "processing_time_ms": 85.3,
  "found": true
}
```

### Get All Songs

```bash
GET /api/v1/songs
```

### Get Song Details

```bash
GET /api/v1/songs/<song_id>
```

### Get Statistics

```bash
GET /api/v1/stats
```

### Health Check

```bash
GET /api/v1/health
```

See [API Documentation](docs/API.md) for complete details.

## Performance

### Benchmarks

| Metric | Value |
|--------|-------|
| Indexing Speed | 100-200 songs/minute (single core) |
| Query Time | <100ms for 10-second clip (10k songs) |
| Accuracy | 95%+ for 5+ second clips |
| Memory Usage | ~200 MB for 10,000 songs |
| Database Size | ~60 MB per 1,000 songs (SQLite) |

### Scalability

| Songs | Query Time | Memory (RAM) | Storage |
|-------|------------|--------------|---------|
| 1,000 | 30-50 ms | 20 MB | 60 MB |
| 10,000 | 80-120 ms | 200 MB | 600 MB |
| 100,000 | 150-250 ms | 2 GB | 6 GB |
| 1,000,000 | 400-600 ms | 20 GB | 60 GB |

See [Performance Documentation](docs/PERFORMANCE.md) for detailed benchmarks.

## Configuration

### Environment Variables

Create a `.env` file:

```bash
# Flask Environment
FLASK_ENV=development

# Storage Configuration
STORAGE_TYPE=memory  # memory, sqlite, postgres
SQLITE_DATABASE_PATH=./data/database/fingerprint.db

# Audio Processing
SAMPLE_RATE=11025
N_FFT=2048
HOP_LENGTH=512
PEAK_NEIGHBORHOOD_SIZE=20
MIN_AMPLITUDE=10
FAN_VALUE=5

# API Configuration
MAX_CONTENT_LENGTH=16777216  # 16 MB
CORS_ORIGINS=*

# Logging
LOG_LEVEL=INFO
LOG_FILE=./data/logs/fingerprint.log
```

### Storage Backends

**Memory Store** (default):
- Fastest performance
- Data lost on restart
- Best for <100k songs

**SQLite**:
- Persistent storage
- Single-file database
- Best for <500k songs

**PostgreSQL**:
- Distributed storage
- High concurrency
- Best for >100k songs

## Docker Deployment

```bash
# Build image
docker build -t audio-fingerprint:latest -f docker/Dockerfile .

# Run container
docker run -d \
  --name fingerprint \
  -p 5000:5000 \
  -v $(pwd)/data:/app/data \
  audio-fingerprint:latest

# Or use docker-compose
docker-compose up -d
```

See [Deployment Guide](docs/DEPLOYMENT.md) for production setup.

## Project Structure

```
Audio-Fingerprint/
├── config/                    # Configuration files
├── fingerprint/              # Main application package
│   ├── core/                # Fingerprinting algorithms
│   ├── storage/             # Database backends
│   ├── api/                 # REST API
│   ├── training/            # Indexing module
│   └── utils/               # Utilities
├── scripts/                  # CLI tools
│   ├── train_index.py       # Index songs
│   ├── benchmark.py         # Performance tests
│   ├── export_db.py         # Backup database
│   └── import_db.py         # Restore database
├── tests/                    # Test suite
├── docs/                     # Documentation
└── data/                     # Data directory (created on first run)
    ├── songs/               # Audio files
    ├── database/            # Database files
    └── logs/                # Application logs
```

## CLI Tools

### Index Songs

```bash
python scripts/train_index.py \
    --songs-dir ./data/songs \
    --storage-type sqlite \
    --workers 4
```

### Benchmark Performance

```bash
python scripts/benchmark.py \
    --audio-dir ./data/songs \
    --num-samples 10
```

### Export Database

```bash
python scripts/export_db.py \
    --storage-type sqlite \
    --output backup.json \
    --format json
```

### Import Database

```bash
python scripts/import_db.py \
    --storage-type sqlite \
    --input backup.json \
    --format json
```

## Development

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run all tests
pytest tests/

# Run with coverage
pytest --cov=fingerprint tests/

# Run specific test file
pytest tests/test_fingerprinter.py
```

### Code Structure

- `fingerprint/core/audio_processor.py` - Audio loading and preprocessing
- `fingerprint/core/fingerprinter.py` - Spectral peak detection
- `fingerprint/core/hash_generator.py` - Combinatorial hashing
- `fingerprint/core/matcher.py` - Fingerprint matching and scoring
- `fingerprint/storage/` - Storage backend implementations
- `fingerprint/api/` - Flask REST API

## Documentation

- [Architecture](docs/ARCHITECTURE.md) - System design and algorithms
- [API Reference](docs/API.md) - Complete API documentation
- [Deployment Guide](docs/DEPLOYMENT.md) - Production deployment
- [Performance](docs/PERFORMANCE.md) - Benchmarks and optimization

## Use Cases

- **Music Identification**: Identify songs from audio clips
- **Copyright Detection**: Find copyrighted content in videos
- **Duplicate Detection**: Find duplicate audio files
- **Audio Matching**: Match audio across different sources
- **Content Monitoring**: Monitor audio streams for specific content

## Limitations

- Requires 5+ seconds of audio for reliable matching
- Performance degrades with very noisy audio
- Not suitable for speech recognition
- Database size grows linearly with song count

## Comparison with Other Systems

### vs Shazam
- Shazam: Cloud-based, 70M+ songs, proprietary
- This System: Self-hosted, unlimited songs, open source

### vs Chromaprint
- Chromaprint: Chroma-based fingerprinting
- This System: Spectral peak-based (similar to Shazam)

### vs AcoustID
- AcoustID: Music identification service
- This System: Complete self-hosted solution

## Contributing

Contributions are welcome! Please feel free to submit pull requests or open issues.

## License

See [LICENSE](LICENSE) file for details.

## Acknowledgments

- Inspired by the Shazam algorithm (Avery Wang, 2003)
- Based on spectral peak fingerprinting techniques
- Uses librosa for audio processing

## Support

For questions or issues:
- Open an issue on GitHub
- Check the [documentation](docs/)
- Review the [implementation guide](IMPLEMENTATION_GUIDE.md)

## Roadmap

- [ ] WebSocket support for real-time streaming
- [ ] Web UI for drag-and-drop search
- [ ] Redis caching layer
- [ ] Distributed database sharding
- [ ] GPU acceleration for STFT
- [ ] Machine learning for parameter optimization

---

**Built with Python, Flask, NumPy, SciPy, and librosa**
