"""Combinatorial hash generation (Wang 2003, the "Shazam" landmarks), vectorised.

Each spectral peak, the anchor, is paired with the next ``fan_value`` peaks
that lie at least ``min_time_delta`` frames later. A pair is encoded as one
36-bit integer::

    hash = (f_anchor << 24) | (f_target << 12) | delta_t
             12 bits           12 bits          12 bits

so frequency bins up to 4095 and time deltas up to 4095 frames fit
(n_fft <= 8190, delta_t <= ~190 s at the default hop and sample rate). The
anchor's absolute frame index is stored next to the hash for offset voting.

Pairs of simultaneous peaks (``delta_t == 0``, the harmonics of one chord)
describe timbre rather than sequence. We keep them by default because strong
harmonics survive noise well. The matcher separately requires an alignment to
span several distinct frames, so one shared chord can never pass as a match.
Set ``min_time_delta=1`` to drop them for very repetitive material.

A hash value is a deterministic function of the audio content alone and
carries no track information. That is what makes the inverted index possible.
"""

from __future__ import annotations

import numpy as np

FREQ_BITS = 12
DELTA_BITS = 12
FREQ_MASK = (1 << FREQ_BITS) - 1
DELTA_MASK = (1 << DELTA_BITS) - 1
MAX_FREQ_BIN = FREQ_MASK
MAX_TIME_DELTA = DELTA_MASK


def encode_hash(f1: np.ndarray, f2: np.ndarray, dt: np.ndarray) -> np.ndarray:
    """Pack (anchor bin, target bin, delta frames) arrays into int64 hashes."""
    f1 = np.asarray(f1, dtype=np.int64) & FREQ_MASK
    f2 = np.asarray(f2, dtype=np.int64) & FREQ_MASK
    dt = np.asarray(dt, dtype=np.int64) & DELTA_MASK
    return (f1 << (FREQ_BITS + DELTA_BITS)) | (f2 << DELTA_BITS) | dt


def decode_hash(hash_value: int) -> tuple[int, int, int]:
    """Inverse of :func:`encode_hash` for a single value (for debugging)."""
    h = int(hash_value)
    return (h >> (FREQ_BITS + DELTA_BITS)) & FREQ_MASK, (h >> DELTA_BITS) & FREQ_MASK, h & DELTA_MASK


def generate_hashes(
    peak_times: np.ndarray,
    peak_freqs: np.ndarray,
    fan_value: int = 10,
    min_time_delta: int = 0,
    max_time_delta: int = 200,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate landmark hashes from spectral peaks.

    Args:
        peak_times: Frame index of each peak (any integer dtype).
        peak_freqs: Frequency bin of each peak.
        fan_value:  How many later peaks each anchor is paired with.
        min_time_delta: Targets must be at least this many frames after the anchor
            (0 = classic behaviour that also pairs simultaneous peaks).
        max_time_delta: Pairs further apart than this are dropped.

    Returns:
        ``(hashes, anchor_times)``: int64 hashes and the int32 frame index of each
        hash's anchor peak. Both arrays are sorted by anchor time.
    """
    t = np.asarray(peak_times, dtype=np.int64)
    f = np.asarray(peak_freqs, dtype=np.int64)
    if t.shape != f.shape:
        raise ValueError("peak_times and peak_freqs must have the same shape")
    n = t.size
    if n < 2 or fan_value < 1:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int32)

    max_time_delta = min(int(max_time_delta), MAX_TIME_DELTA)
    min_time_delta = max(int(min_time_delta), 0)

    # Deterministic ordering: by time, then by frequency.
    order = np.lexsort((f, t))
    t = t[order]
    f = f[order]

    anchors = np.arange(n)
    if min_time_delta == 0:
        first_target = anchors + 1
    else:
        # First peak at least min_time_delta frames after each anchor (peaks are time-sorted).
        first_target = np.searchsorted(t, t + min_time_delta, side="left")

    hashes: list[np.ndarray] = []
    anchor_times: list[np.ndarray] = []
    for k in range(int(fan_value)):
        target = first_target + k
        valid = target < n
        if not valid.any():
            break
        a = anchors[valid]
        b = target[valid]
        dt = t[b] - t[a]
        keep = (dt >= min_time_delta) & (dt <= max_time_delta)
        if not keep.any():
            continue
        hashes.append(encode_hash(f[a][keep], f[b][keep], dt[keep]))
        anchor_times.append(t[a][keep])

    if not hashes:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int32)

    all_hashes = np.concatenate(hashes)
    all_anchors = np.concatenate(anchor_times).astype(np.int32)
    order = np.argsort(all_anchors, kind="stable")
    return all_hashes[order], all_anchors[order]
