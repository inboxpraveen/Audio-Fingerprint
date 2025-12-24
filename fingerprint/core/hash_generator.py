"""Combinatorial hash generation from fingerprint peaks."""


def generate_hashes(peaks, song_id=None, fan_value=5):
    """
    Generate combinatorial hashes from fingerprint peaks (Shazam-style).
    
    For each peak (anchor), pair with next fan_value peaks to create hashes.
    Hash format: hash(freq1, freq2, time_delta)
    
    Args:
        peaks: List of (time_idx, freq_idx, amplitude) tuples
        song_id: Optional song identifier
        fan_value: Number of subsequent peaks to pair with each anchor
    
    Returns:
        list: List of (hash_value, time_offset, song_id) tuples
    """
    hashes = []
    
    # For each anchor point
    for i in range(len(peaks)):
        anchor_time, anchor_freq, _ = peaks[i]
        
        # Pair with next fan_value peaks
        for j in range(1, fan_value + 1):
            if i + j < len(peaks):
                target_time, target_freq, _ = peaks[i + j]
                
                # Calculate time delta
                time_delta = target_time - anchor_time
                
                # Skip if time delta is too large (> 1023 for 10-bit encoding)
                if time_delta > 1023:
                    continue
                
                # Generate hash: (f1 << 20) | (f2 << 10) | (Δt & 0x3FF)
                hash_value = (
                    (int(anchor_freq) << 20) | 
                    (int(target_freq) << 10) | 
                    (int(time_delta) & 0x3FF)
                )
                
                # Store (hash, time_offset, song_id)
                hashes.append((hash_value, anchor_time, song_id))
    
    return hashes


def decode_hash(hash_value):
    """
    Decode hash back to its components (for debugging).
    
    Args:
        hash_value: Encoded hash integer
    
    Returns:
        tuple: (freq1, freq2, time_delta)
    """
    freq1 = (hash_value >> 20) & 0xFFF
    freq2 = (hash_value >> 10) & 0x3FF
    time_delta = hash_value & 0x3FF
    
    return freq1, freq2, time_delta

