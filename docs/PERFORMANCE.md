# Performance

## Search Latency

Typical end-to-end search time (from HTTP request to JSON response):

| Library size | 5-second clip | 10-second clip |
|-------------|--------------|----------------|
| 100 songs   | ~80 ms       | ~120 ms        |
| 1 000 songs | ~150 ms      | ~220 ms        |
| 10 000 songs | ~400 ms     | ~600 ms        |

*Measured on a mid-range laptop with SQLite on an SSD. Results vary by audio complexity and hardware.*

The dominant cost is the batch SQL lookup. The covering index means SQLite never touches the table heap — it reads `(hash_value, song_id, time_offset)` directly from the index B-tree.

---

## Indexing Throughput

Default settings: `SAMPLE_RATE=11025`, `FAN_VALUE=10`, `num_workers=4`.

| File type | Duration | Indexing time |
|-----------|----------|--------------|
| MP3 (128 kbps) | 3 min | ~2 s |
| WAV (44.1 kHz stereo) | 3 min | ~3 s |
| FLAC | 3 min | ~2.5 s |

Indexing is CPU-bound (STFT + peak extraction). The ThreadPoolExecutor in `Indexer.index_directory` parallelises across files, so a 4-worker run on a quad-core machine is near-linear up to the file count.

---

## Storage Estimates

At default settings (`FAN_VALUE=10`, `SR=11025`):

| Duration | Approx. peaks | Approx. hashes | DB rows |
|----------|--------------|----------------|---------|
| 3-minute song | ~15 K | ~100 K | ~100 K |
| 10-minute podcast | ~50 K | ~350 K | ~350 K |
| 1-hour file | ~300 K | ~2 M | ~2 M |

Each fingerprint row in SQLite occupies ~24 bytes (3 × 8-byte integers). 1 million hashes ≈ 24 MB on disk. A library of 1 000 three-minute songs ≈ 2.4 GB.

---

## Tuning

### Accuracy vs. storage trade-off

| Setting | Effect |
|---------|--------|
| `FAN_VALUE` ↑ | More hashes per song → higher recall on noisy clips, more storage |
| `PEAK_NEIGHBORHOOD_SIZE` ↓ | More peaks extracted → denser fingerprint, slower but more robust |
| `MIN_AMPLITUDE` ↓ | More peaks from quiet regions → better coverage, noisier DB |

### Speed trade-off

| Setting | Effect |
|---------|--------|
| `SAMPLE_RATE` ↓ (e.g. 8000) | Faster STFT, slightly lower accuracy on high-frequency content |
| `N_FFT` ↓ (e.g. 1024) | Faster STFT, less frequency resolution |
| `HOP_LENGTH` ↑ (e.g. 1024) | Fewer frames, faster but lower temporal resolution |

### SQLite cache

For very large libraries (> 500 K songs), increase the in-memory page cache:

```python
# fingerprint/storage/sqlite_store.py — _get_conn()
conn.execute("PRAGMA cache_size=-262144")  # 256 MB
conn.execute("PRAGMA mmap_size=1073741824")  # 1 GB
```

This keeps the hot part of the covering index resident in memory, reducing disk I/O on repeated searches.
