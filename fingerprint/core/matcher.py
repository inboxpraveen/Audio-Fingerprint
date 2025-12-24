"""Fingerprint matching and scoring."""

from collections import defaultdict, Counter


def match_fingerprint(query_hashes, db_store, top_k=5):
    """
    Match query fingerprints against database and score candidates.
    
    Args:
        query_hashes: List of (hash_value, time_offset, None) tuples
        db_store: Storage backend instance
        top_k: Number of top matches to return
    
    Returns:
        list: List of (song_id, confidence_score, metadata) tuples
    """
    # Group matches by song_id
    candidate_matches = defaultdict(list)
    
    # Query database for each hash
    for hash_value, query_time, _ in query_hashes:
        matches = db_store.query_hash(hash_value)
        
        # matches: list of (song_id, db_time)
        for song_id, db_time in matches:
            # Calculate time offset difference
            time_offset = db_time - query_time
            candidate_matches[song_id].append(time_offset)
    
    # Score each candidate song
    scored_matches = []
    total_query_hashes = len(query_hashes)
    
    for song_id, time_offsets in candidate_matches.items():
        # Create histogram of time offsets
        offset_histogram = Counter(time_offsets)
        
        # Score is the max aligned peak count
        max_aligned_peaks = max(offset_histogram.values())
        
        # Normalize by query hash count
        confidence_score = max_aligned_peaks / total_query_hashes if total_query_hashes > 0 else 0
        
        # Get song metadata
        metadata = db_store.get_song_metadata(song_id)
        
        scored_matches.append((song_id, confidence_score, metadata))
    
    # Sort by confidence score (descending)
    scored_matches.sort(key=lambda x: x[1], reverse=True)
    
    # Return top K matches
    return scored_matches[:top_k]


def calculate_match_score(time_offsets, total_query_hashes):
    """
    Calculate match score from time offset histogram.
    
    Args:
        time_offsets: List of time offset differences
        total_query_hashes: Total number of query hashes
    
    Returns:
        float: Confidence score (0-1)
    """
    if not time_offsets or total_query_hashes == 0:
        return 0.0
    
    # Create histogram
    histogram = Counter(time_offsets)
    
    # Get maximum aligned peaks
    max_count = max(histogram.values())
    
    # Normalize
    score = max_count / total_query_hashes
    
    return score

