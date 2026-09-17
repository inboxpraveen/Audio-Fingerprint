"""Response formatting: turn domain objects into the documented JSON shapes."""

from __future__ import annotations

from typing import Any

from ..core import TrackMatch, quality_label
from ..core.fingerprinter import Fingerprint
from ..jobs import Job
from ..storage import TrackRecord
from .runtime import SearchResult


def format_track(record: TrackRecord, include_path: bool = False) -> dict[str, Any]:
    data = record.to_dict(include_path=include_path)
    data["display_name"] = record.title or record.filename or record.track_id
    return data


def _occurrence(o, fp: Fingerprint) -> dict[str, Any]:
    offset_sec = fp.frames_to_seconds(o.offset_frames)
    return {
        "offset_sec": round(offset_sec, 3),
        "track_offset_sec": round(max(0.0, offset_sec), 3),
        "query_offset_sec": round(max(0.0, -offset_sec), 3),
        "query_start_sec": round(fp.frames_to_seconds(o.query_start_frames), 3),
        "query_end_sec": round(fp.frames_to_seconds(o.query_end_frames), 3),
        "track_start_sec": round(max(0.0, fp.frames_to_seconds(o.track_start_frames)), 3),
        "track_end_sec": round(max(0.0, fp.frames_to_seconds(o.track_end_frames)), 3),
        "aligned_hashes": o.aligned_hashes,
        "confidence": o.confidence,
        "peak_ratio": o.peak_ratio,
        "quality": quality_label(o.confidence, o.peak_ratio),
    }


def format_match(match: TrackMatch, fp: Fingerprint) -> dict[str, Any]:
    best = _occurrence(match.best, fp)
    item: dict[str, Any] = {
        "track_id": match.track.track_id if match.track else None,
        "title": match.track.title if match.track else "",
        "artist": match.track.artist if match.track else "",
        "filename": match.track.filename if match.track else "",
        "duration": match.track.duration if match.track else None,
        "source_type": match.track.source_type if match.track else None,
        "tags": match.track.tags if match.track else [],
        "num_hashes": match.track.num_hashes if match.track else None,
        "confidence": match.confidence,
        "aligned_hashes": match.aligned_hashes,
        "peak_ratio": match.peak_ratio,
        "quality": best["quality"],
        "matched_rows": match.matched_rows,
        **{k: best[k] for k in ("offset_sec", "track_offset_sec", "query_offset_sec", "query_start_sec", "query_end_sec", "track_start_sec", "track_end_sec")},
        # Backwards-compatible alias (1.x clients).
        "match_offset_sec": best["track_offset_sec"],
        "occurrences": [_occurrence(o, fp) for o in match.occurrences],
    }
    if match.track:
        item["display_name"] = match.track.title or match.track.filename
    return item


def format_search(result: SearchResult) -> dict[str, Any]:
    fp = result.query
    return {
        "found": bool(result.matches),
        "mode": result.options.mode,
        "matches": [format_match(m, fp) for m in result.matches],
        "query": {
            "filename": result.filename,
            "duration_sec": round(fp.duration_sec, 3),
            "num_peaks": fp.num_peaks,
            "num_hashes": fp.num_hashes,
            "truncated": result.truncated,
        },
        "thresholds": {
            "min_confidence": result.options.min_confidence,
            "min_aligned_hashes": result.options.min_aligned_hashes,
            "min_peak_ratio": result.options.min_peak_ratio,
            "top_k": result.options.top_k,
        },
        "processing_time_ms": round(result.processing_ms, 1),
        "diagnostics": result.extra.get("diagnostics", {}),
        # Backwards-compatible alias (1.x clients).
        "query_duration_sec": round(fp.duration_sec, 2),
    }


def format_job(job: Job, include_errors: bool = True) -> dict[str, Any]:
    return job.to_dict(include_errors=include_errors)


def format_page(items: list[dict[str, Any]], total: int, page: int, per_page: int) -> dict[str, Any]:
    pages = max(1, (total + per_page - 1) // per_page) if per_page else 1
    return {"items": items, "total": total, "page": page, "per_page": per_page, "pages": pages}
