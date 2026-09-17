"""Offset-histogram matching (Wang 2003) with two search modes.

Every hash shared between the query and an indexed track "votes" for the time
offset ``track_frame - query_frame``.  Audio that is genuinely the same lines
up at one offset and produces a sharp spike in the vote histogram; unrelated
audio with coincidental hash collisions produces a flat, noisy histogram.

Three complementary scores are computed per candidate:

``aligned_hashes``
    Number of **distinct** query hashes voting for the best offset (after
    merging +/- ``offset_tolerance_frames`` neighbouring bins, because a clip
    rarely starts exactly on a frame boundary).  Counting distinct hashes
    rather than raw votes neutralises sustained tones, which repeat one hash
    for many frames and would otherwise fake an alignment.
``confidence``
    ``aligned_hashes / distinct query hashes inside the matched span`` clamped
    to [0, 1]: how much of the query region that overlaps the track is actually
    explained by the alignment.  Normalising by the *matched region* (not the
    whole query) keeps the score meaningful for "short clip vs long track",
    "short indexed pattern vs long recording" and "two long recordings sharing
    a segment".
``peak_ratio``
    ``aligned_hashes / mean votes per non-empty offset bin away from the spike``
    - the sharpness of the spike.  Chance matches sit around 3-9 regardless of
    library size; true matches are typically well above 10.

Modes:

``identify``
    Best alignment per track (classic "what is this clip?").
``occurrences``
    *Every* spike above the thresholds per track.  Use it to find all places a
    jingle, disclaimer or hold-music pattern appears - the building block for
    QA-style pattern search on call recordings.  Offsets may be **negative**: a
    negative offset means the indexed track starts *after* the query does, i.e.
    the track's content is found inside the query at ``-offset`` (index short
    patterns, search with a long recording).

Every occurrence carries the span of query frames covered by its aligned
hashes, so callers can say "pattern X covers 30.0-34.0 s of this recording".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from ..config import Settings
from ..utils.exceptions import MatchingError

if TYPE_CHECKING:  # pragma: no cover
    from ..storage.base import StorageBackend, TrackRecord
    from .fingerprinter import Fingerprint

logger = logging.getLogger(__name__)

MODES = ("identify", "occurrences")

# A genuine alignment spans time: the aligned hashes must come from at least this
# many distinct query frames.  Guards against a single shared instant (a chord, a
# click) producing many simultaneous hash hits.
MIN_ALIGNED_FRAMES = 3

# Offset bins with at least this many (smoothed) votes are treated as potential
# alignments and excluded from the background estimate.  Deliberately a constant,
# not ``min_aligned_hashes``: lowering that threshold must not silently change how
# ``peak_ratio`` is measured.
SPIKE_VOTES = 10

# Upper bound on offset bins examined per track when looking for occurrences.  Very
# repetitive material (hold music, synthetic tones) can produce thousands of bins
# above the vote floor; the strongest ones are examined first, so real matches are
# never lost by the cap.
MAX_CANDIDATE_BINS = 400


@dataclass
class Occurrence:
    offset_frames: int  # track_frame - query_frame at this alignment
    aligned_hashes: int
    confidence: float
    peak_ratio: float
    query_start_frames: int  # first / last query frame with an aligned hash (outliers trimmed)
    query_end_frames: int

    @property
    def track_start_frames(self) -> int:
        return self.query_start_frames + self.offset_frames

    @property
    def track_end_frames(self) -> int:
        return self.query_end_frames + self.offset_frames


@dataclass
class TrackMatch:
    track_ref: int
    aligned_hashes: int
    confidence: float
    peak_ratio: float
    offset_frames: int
    matched_rows: int
    occurrences: list[Occurrence] = field(default_factory=list)
    track: TrackRecord | None = None

    @property
    def best(self) -> Occurrence:
        return self.occurrences[0]


@dataclass
class MatchOptions:
    mode: str = "identify"
    top_k: int = 5
    min_aligned_hashes: int = 10
    min_confidence: float = 0.02
    min_peak_ratio: float = 12.0
    offset_tolerance_frames: int = 1
    max_occurrences_per_track: int = 25
    max_rows_per_hash: int = 2000
    max_search_votes: int = 5_000_000

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> MatchOptions:
        opts = cls(
            top_k=settings.top_k,
            min_aligned_hashes=settings.min_aligned_hashes,
            min_confidence=settings.min_confidence,
            min_peak_ratio=settings.min_peak_ratio,
            offset_tolerance_frames=settings.offset_tolerance_frames,
            max_occurrences_per_track=settings.max_occurrences_per_track,
            max_rows_per_hash=settings.max_rows_per_hash,
            max_search_votes=settings.max_search_votes,
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(opts, key, value)
        if opts.mode not in MODES:
            raise MatchingError(f"mode must be one of {MODES}", code="validation_error", http_status=400)
        opts.top_k = max(1, min(int(opts.top_k), settings.max_top_k))
        opts.min_aligned_hashes = max(1, int(opts.min_aligned_hashes))
        opts.min_confidence = min(1.0, max(0.0, float(opts.min_confidence)))
        opts.min_peak_ratio = max(0.0, float(opts.min_peak_ratio))
        opts.offset_tolerance_frames = max(0, int(opts.offset_tolerance_frames))
        opts.max_occurrences_per_track = max(1, int(opts.max_occurrences_per_track))
        return opts


@dataclass
class _Votes:
    """All votes of one candidate track."""

    ref: int
    offsets: np.ndarray  # track_frame - query_frame per vote
    qtimes: np.ndarray  # query anchor frame per vote
    qhashes: np.ndarray  # query hash value per vote


@dataclass
class _QueryIndex:
    """Query hashes sorted by anchor time, for distinct-hash counting inside a span."""

    times: np.ndarray
    hashes: np.ndarray

    def distinct_in_span(self, start: int, end: int) -> int:
        lo = int(np.searchsorted(self.times, start, side="left"))
        hi = int(np.searchsorted(self.times, end, side="right"))
        return _count_distinct(self.hashes[lo:hi]) if hi > lo else 0


def _count_distinct(values: np.ndarray) -> int:
    """Number of distinct values (sort + diff; several times faster than np.unique on large arrays)."""
    if values.size == 0:
        return 0
    ordered = np.sort(values)
    return int(np.count_nonzero(np.diff(ordered))) + 1


@dataclass
class MatchDiagnostics:
    """What the matcher did for one query (exposed as ``diagnostics`` in API responses)."""

    query_hashes: int = 0
    db_rows: int = 0
    votes: int = 0
    candidate_tracks: int = 0
    scored_tracks: int = 0
    skipped_common_hashes: int = 0
    dropped_for_vote_cap: int = 0

    def to_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


class Matcher:
    """Match a query :class:`Fingerprint` against a storage backend."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_diagnostics = MatchDiagnostics()

    def match(self, query: Fingerprint, store: StorageBackend, options: MatchOptions | None = None) -> list[TrackMatch]:
        opts = options or MatchOptions.from_settings(self.settings)
        diag = self.last_diagnostics = MatchDiagnostics(query_hashes=query.num_hashes)
        if query.num_hashes == 0:
            return []

        # Sort the query by hash so each DB row can be joined to its query anchors with searchsorted.
        order = np.argsort(query.hashes, kind="stable")
        q_hashes_sorted = query.hashes[order]
        q_times_sorted = query.hash_times.astype(np.int64)[order]
        unique_hashes = np.unique(q_hashes_sorted)

        store_stats: dict = {}
        db_hashes, db_refs, db_times = store.query_hashes(unique_hashes, max_rows_per_hash=opts.max_rows_per_hash or None, stats=store_stats)
        diag.db_rows = int(db_hashes.size)
        diag.skipped_common_hashes = int(store_stats.get("skipped_hashes", 0))
        if db_hashes.size == 0:
            return []

        # Join: every DB row x every query anchor sharing its hash.
        left = np.searchsorted(q_hashes_sorted, db_hashes, side="left")
        right = np.searchsorted(q_hashes_sorted, db_hashes, side="right")
        counts = right - left
        if opts.max_search_votes and int(counts.sum()) > opts.max_search_votes:
            # Drop the most vote-heavy hashes until the join fits the budget (they carry the least information).
            keep = self._cap_votes(db_hashes, counts, opts.max_search_votes)
            diag.dropped_for_vote_cap = int(np.count_nonzero(~keep))
            db_hashes, db_refs, db_times, counts, left = db_hashes[keep], db_refs[keep], db_times[keep], counts[keep], left[keep]
            logger.warning(
                "Search vote cap hit: dropped %d rows of very common hashes (raise AUDIOFP_MAX_SEARCH_VOTES to keep them)", diag.dropped_for_vote_cap
            )
        total = int(counts.sum())
        diag.votes = total
        if total == 0:
            return []
        rep_refs = np.repeat(db_refs.astype(np.int64), counts)
        rep_db_times = np.repeat(db_times.astype(np.int64), counts)
        within = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        q_index = np.repeat(left, counts) + within
        rep_q_times = q_times_sorted[q_index]
        rep_q_hashes = q_hashes_sorted[q_index]
        offsets = rep_db_times - rep_q_times

        # Group votes by track.
        group_order = np.argsort(rep_refs, kind="stable")
        refs_sorted = rep_refs[group_order]
        boundaries = np.flatnonzero(np.diff(refs_sorted)) + 1
        starts = np.concatenate(([0], boundaries))
        ends = np.concatenate((boundaries, [refs_sorted.size]))
        diag.candidate_tracks = int(starts.size)

        # Vectorised prefilter: a track can only pass if some offset bin, even after +/-tolerance
        # smoothing, could reach min_aligned_hashes. Computed for all tracks at once so that a big
        # library full of chance coincidences costs a few numpy calls, not a Python loop per track.
        offsets_sorted = offsets[group_order]
        span = int(offsets_sorted.max() - offsets_sorted.min()) + 1
        keys = refs_sorted * span + (offsets_sorted - int(offsets_sorted.min()))
        _bins, bin_counts = np.unique(keys, return_counts=True)
        bin_refs = _bins // span
        bin_starts = np.concatenate(([0], np.flatnonzero(np.diff(bin_refs)) + 1))
        max_raw_per_track = np.maximum.reduceat(bin_counts, bin_starts)
        # Bins are ordered by (ref, offset), so the per-track maxima line up with ``starts``/``ends``.
        possible = max_raw_per_track * (2 * opts.offset_tolerance_frames + 1) >= opts.min_aligned_hashes

        time_order = np.argsort(query.hash_times, kind="stable")
        qindex = _QueryIndex(query.hash_times.astype(np.int64)[time_order], query.hashes[time_order])

        results: list[TrackMatch] = []
        for s, e in zip(starts[possible].tolist(), ends[possible].tolist()):
            ref = int(refs_sorted[s])
            diag.scored_tracks += 1
            sel = group_order[s:e]
            votes = _Votes(ref, offsets[sel], rep_q_times[sel], rep_q_hashes[sel])
            match = self._score_track(votes, qindex, opts)
            if match is not None:
                results.append(match)
        if not results:
            return []

        results.sort(key=lambda m: (m.aligned_hashes, m.peak_ratio), reverse=True)
        results = results[: opts.top_k]
        records = store.get_tracks_by_ref([m.track_ref for m in results])
        for match in results:
            match.track = records.get(match.track_ref)
        return results

    @staticmethod
    def _cap_votes(db_hashes: np.ndarray, counts: np.ndarray, budget: int) -> np.ndarray:
        """Boolean mask keeping DB rows so that the total join size stays within *budget*.

        Rows are grouped by hash; the hashes contributing the most votes are dropped first.
        """
        uniq, inverse = np.unique(db_hashes, return_inverse=True)
        per_hash = np.bincount(inverse, weights=counts).astype(np.int64)
        order = np.argsort(per_hash, kind="stable")  # lightest hashes first
        cumulative = np.cumsum(per_hash[order])
        allowed = np.zeros(uniq.size, dtype=bool)
        allowed[order[cumulative <= budget]] = True
        return allowed[inverse]

    # ------------------------------------------------------------------ scoring
    @staticmethod
    def _smoothed_histogram(offsets: np.ndarray, tolerance: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (bins, raw_counts, smoothed_counts) with +/- tolerance bins merged."""
        bins, raw = np.unique(offsets, return_counts=True)
        if tolerance <= 0 or bins.size == 1:
            return bins, raw, raw.astype(np.int64)
        smoothed = raw.astype(np.int64).copy()
        for delta in range(1, tolerance + 1):
            for sign in (-1, 1):
                shifted = bins + sign * delta
                idx = np.minimum(np.searchsorted(bins, shifted), bins.size - 1)
                hit = bins[idx] == shifted
                smoothed[hit] += raw[idx[hit]]
        return bins, raw, smoothed

    @staticmethod
    def _background(bins: np.ndarray, raw: np.ndarray, smoothed: np.ndarray, best_offset: int, tolerance: int) -> float:
        """Average votes per non-empty offset bin away from every candidate spike (floor 1).

        The best spike and every bin that could itself be an alignment (smoothed
        votes >= SPIKE_VOTES) are excluded together with a few frames around
        them: those bins hold the match and its jitter votes (noise shifts peaks
        by a frame or two).  In a clean library nearly every vote sits on the
        true offset, and a pattern that occurs several times produces several
        spikes - a plain mean over all bins would be dominated by the matches
        themselves and hide them.
        """
        exclusion = 2 * tolerance + 2
        strong = np.union1d(bins[smoothed >= SPIKE_VOTES], np.array([best_offset], dtype=bins.dtype))
        # Distance from each bin to the nearest strong bin (strong is sorted because bins is).
        idx = np.searchsorted(strong, bins)
        left = strong[np.maximum(idx - 1, 0)]
        right = strong[np.minimum(idx, strong.size - 1)]
        nearest = np.minimum(np.abs(bins - left), np.abs(bins - right))
        away = nearest > exclusion
        if not away.any():
            return 1.0
        return max(1.0, float(raw[away].mean()))

    def _score_track(self, votes: _Votes, qindex: _QueryIndex, opts: MatchOptions) -> TrackMatch | None:
        bins, raw, smoothed = self._smoothed_histogram(votes.offsets, opts.offset_tolerance_frames)
        best_i = int(np.argmax(smoothed))
        # Raw votes bound the distinct count, so this is a cheap early rejection.
        if int(smoothed[best_i]) < opts.min_aligned_hashes:
            return None
        background = self._background(bins, raw, smoothed, int(bins[best_i]), opts.offset_tolerance_frames)
        occurrences = self._find_occurrences(votes, bins, smoothed, background, qindex, opts)
        if not occurrences:
            return None
        best = occurrences[0]
        return TrackMatch(
            track_ref=votes.ref,
            aligned_hashes=best.aligned_hashes,
            confidence=best.confidence,
            peak_ratio=best.peak_ratio,
            offset_frames=best.offset_frames,
            matched_rows=int(votes.offsets.size),
            occurrences=occurrences,
        )

    @staticmethod
    def _find_occurrences(votes: _Votes, bins, smoothed, background: float, qindex: _QueryIndex, opts: MatchOptions) -> list[Occurrence]:
        """Strongest spikes first; a spike is dropped only if it re-matches the same query AND track audio.

        Cost is bounded: only bins whose *raw* vote count could possibly pass the
        thresholds are examined (raw votes bound the distinct-hash count), at most
        MAX_CANDIDATE_BINS of them, and each candidate reads its votes through a
        sorted-offset slice instead of scanning every vote of the track.
        """
        limit = opts.max_occurrences_per_track if opts.mode == "occurrences" else 1
        tol = opts.offset_tolerance_frames
        # Cheap upper-bound filter: smoothed raw votes >= distinct aligned hashes.
        floor = max(opts.min_aligned_hashes, int(np.ceil(opts.min_peak_ratio * background)))
        candidate_idx = np.flatnonzero(smoothed >= floor)
        if candidate_idx.size == 0:
            return []
        order = candidate_idx[np.argsort(smoothed[candidate_idx], kind="stable")[::-1]][:MAX_CANDIDATE_BINS]

        # Votes sorted by offset so each candidate bin is a contiguous slice.
        by_offset = np.argsort(votes.offsets, kind="stable")
        off_sorted = votes.offsets[by_offset]
        qt_sorted = votes.qtimes[by_offset]
        qh_sorted = votes.qhashes[by_offset]

        kept: list[Occurrence] = []
        for i in order:
            offset = int(bins[i])
            lo = int(np.searchsorted(off_sorted, offset - tol, side="left"))
            hi = int(np.searchsorted(off_sorted, offset + tol, side="right"))
            aligned = _count_distinct(qh_sorted[lo:hi])
            ratio = aligned / background
            if aligned < opts.min_aligned_hashes or ratio < opts.min_peak_ratio:
                continue
            aligned_times = qt_sorted[lo:hi]
            if _count_distinct(aligned_times) < MIN_ALIGNED_FRAMES:
                continue  # one instant cannot be an alignment (e.g. the same chord in two recordings)
            q_start, q_end = _robust_span(aligned_times)
            conf = min(1.0, aligned / max(qindex.distinct_in_span(q_start, q_end), 1))
            if conf < opts.min_confidence:
                continue
            candidate = Occurrence(offset, aligned, round(conf, 4), round(ratio, 2), q_start, q_end)
            # Same event: an offset within the jitter window of a kept spike, or the same query
            # region matched to the same track region.
            if any(abs(offset - k.offset_frames) <= 2 * tol + 1 or _same_region(candidate, k) for k in kept):
                continue
            kept.append(candidate)
            if len(kept) >= limit:
                break
        if len(kept) > 1:
            strongest = max(kept, key=lambda o: (o.aligned_hashes, o.peak_ratio))
            rest = sorted((o for o in kept if o is not strongest), key=lambda o: o.offset_frames)
            kept = [strongest] + rest
        return kept


def _robust_span(times: np.ndarray) -> tuple[int, int]:
    """Frame span covered by the bulk of the votes (2nd-98th percentile trims stray coincidences)."""
    if times.size <= 20:
        return int(times.min()), int(times.max())
    lo, hi = np.percentile(times, [2, 98])
    return int(lo), int(hi)


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    """True when the shorter span is more than half covered by the other."""
    inter = min(a1, b1) - max(a0, b0)
    if inter < 0:
        return False
    shortest = max(1, min(a1 - a0, b1 - b0))
    return inter / shortest > 0.5


def _same_region(a: Occurrence, b: Occurrence) -> bool:
    """Same query audio matched to the same track audio (i.e. offset jitter, not a new occurrence)."""
    return _overlaps(a.query_start_frames, a.query_end_frames, b.query_start_frames, b.query_end_frames) and _overlaps(
        a.track_start_frames, a.track_end_frames, b.track_start_frames, b.track_end_frames
    )


def quality_label(confidence: float, peak_ratio: float) -> str:
    """Human-friendly bucket used by the UI and CLI."""
    if confidence >= 0.15 and peak_ratio >= 30:
        return "strong"
    if confidence >= 0.05 and peak_ratio >= 18:
        return "likely"
    return "weak"
