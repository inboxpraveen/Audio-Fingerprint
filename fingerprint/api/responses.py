"""Response formatting utilities."""

from flask import jsonify


def format_search_response(matches: list, processing_time_ms: float, query_duration_sec: float):
    """
    Format the search response.

    matches: list of (song_id, confidence_score, metadata, match_offset_sec)
    """
    formatted = []

    for song_id, confidence, metadata, match_offset_sec in matches:
        item = {
            "song_id": song_id,
            "confidence": round(confidence, 4),
            "match_offset_sec": round(match_offset_sec, 2),
        }
        if metadata:
            # Prefer explicit title; fall back to filename stem
            item["title"] = metadata.get("title") or metadata.get("filename", "Unknown")
            item["artist"] = metadata.get("artist", "")
            item["duration"] = metadata.get("duration")
            item["filename"] = metadata.get("filename", "")
            item["num_hashes"] = metadata.get("num_hashes", 0)

        formatted.append(item)

    return jsonify({
        "matches": formatted,
        "query_duration_sec": round(query_duration_sec, 2),
        "processing_time_ms": round(processing_time_ms, 2),
        "found": len(formatted) > 0,
    })


def format_error_response(error_message: str, status_code: int = 400):
    """Return a standard JSON error response."""
    return jsonify({"error": error_message, "status": status_code}), status_code
