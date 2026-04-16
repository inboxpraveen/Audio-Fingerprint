"""Fingerprint matching and scoring."""

from collections import defaultdict, Counter


def match_fingerprint(
    query_hashes: list,
    db_store,
    top_k: int = 5,
    hop_length: int = 512,
    sr: int = 11025,
) -> list:
    """
    Match query fingerprints against the database.

    Uses a single batch hash lookup instead of N individual lookups,
    which provides a 10-100× speedup on large databases.

    Args:
        query_hashes: list of (hash_value, time_offset, None) tuples
        db_store:     storage backend
        top_k:        number of top matches to return
        hop_length:   STFT hop length (for converting frames → seconds)
        sr:           sample rate (for converting frames → seconds)

    Returns:
        list of (song_id, confidence_score, metadata, match_offset_sec) tuples
        sorted by confidence descending
    """
    if not query_hashes:
        return []

    # Build map: hash_value → [query_time, ...] (may appear more than once)
    hash_to_qtimes: dict = defaultdict(list)
    for hash_value, query_time, _ in query_hashes:
        hash_to_qtimes[hash_value].append(query_time)

    unique_hashes = list(hash_to_qtimes.keys())

    # Single batch DB query
    db_results = db_store.query_hashes_batch(unique_hashes)

    if not db_results:
        return []

    # Accumulate time-offset differences per candidate song
    candidate_offsets: dict = defaultdict(list)
    for hash_value, song_id, db_time in db_results:
        for query_time in hash_to_qtimes[hash_value]:
            candidate_offsets[song_id].append(db_time - query_time)

    total_query_hashes = len(query_hashes)
    scored: list = []

    for song_id, offsets in candidate_offsets.items():
        histogram = Counter(offsets)
        best_offset, max_aligned = histogram.most_common(1)[0]

        # Fraction of query hashes that align at the best time offset
        confidence = max_aligned / total_query_hashes

        # Convert STFT frame offset → wall-clock seconds in the original song
        match_offset_sec = max(0.0, best_offset * hop_length / sr)

        metadata = db_store.get_song_metadata(song_id)
        scored.append((song_id, confidence, metadata, match_offset_sec))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def calculate_match_score(time_offsets: list, total_query_hashes: int) -> float:
    """Utility: score from a pre-computed list of time offsets."""
    if not time_offsets or total_query_hashes == 0:
        return 0.0
    return max(Counter(time_offsets).values()) / total_query_hashes
