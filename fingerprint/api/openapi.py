"""OpenAPI 3.0 description of the REST API (served at ``/api/v1/openapi.json``).

Hand-maintained so it can carry real explanations; ``tests/test_api.py`` checks
that every registered route is documented here and vice versa.
"""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..config import Settings


def _err(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _json(schema_ref: str, description: str = "OK") -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


def build_openapi(settings: Settings) -> dict[str, Any]:
    track_id_param = {"name": "track_id", "in": "path", "required": True, "schema": {"type": "string"}, "description": "Opaque track id (UUID)."}
    job_id_param = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}
    security = [{"ApiKeyHeader": []}, {"BearerToken": []}] if settings.api_key else []

    paths: dict[str, Any] = {
        "/search": {
            "post": {
                "tags": ["Search"],
                "summary": "Identify a clip, or find every occurrence of a pattern",
                "description": (
                    "Upload an audio/video clip. In `identify` mode the best alignment per track is returned "
                    '("what is this?"). In `occurrences` mode every alignment above the thresholds is returned per '
                    "track, with the matched time spans - use it to find all places a jingle, disclaimer or hold-music "
                    "pattern appears. Offsets are signed: a negative `offset_sec` means the indexed track starts after "
                    "the query does, i.e. the track's content occurs inside the query at `query_offset_sec`."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["audio"],
                                "properties": {
                                    "audio": {"type": "string", "format": "binary"},
                                    "mode": {"type": "string", "enum": ["identify", "occurrences"], "default": "identify"},
                                    "top_k": {"type": "integer", "minimum": 1, "maximum": settings.max_top_k},
                                    "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                    "min_aligned_hashes": {"type": "integer", "minimum": 1},
                                    "min_peak_ratio": {"type": "number", "minimum": 0},
                                    "max_occurrences": {"type": "integer", "minimum": 1, "maximum": 500},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": _json("SearchResponse"),
                    "400": _err("Bad input"),
                    "415": _err("Unsupported format"),
                    "422": _err("Could not decode the audio"),
                },
            }
        },
        "/tracks": {
            "get": {
                "tags": ["Tracks"],
                "summary": "List tracks",
                "parameters": [
                    {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Case-insensitive search in title, artist, filename and tags."},
                    {
                        "name": "sort",
                        "in": "query",
                        "schema": {"type": "string", "enum": ["indexed_at", "title", "artist", "duration", "filename", "num_hashes"]},
                    },
                    {"name": "order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
                    {"name": "source_type", "in": "query", "schema": {"type": "string", "enum": ["audio", "video"]}},
                    {"name": "tag", "in": "query", "schema": {"type": "string"}},
                    {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1, "default": 1}},
                    {"name": "per_page", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
                ],
                "responses": {"200": _json("TrackPage")},
            },
            "post": {
                "tags": ["Tracks"],
                "summary": "Upload a file and index it in the background",
                "requestBody": {
                    "required": True,
                    "content": {
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["audio"],
                                "properties": {
                                    "audio": {"type": "string", "format": "binary"},
                                    "title": {"type": "string"},
                                    "artist": {"type": "string"},
                                    "tags": {"type": "string", "description": "Comma-separated"},
                                    "metadata": {"type": "string", "description": "JSON object with custom fields"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "202": _json("JobAccepted", "Accepted - poll the job"),
                    "400": _err("Bad input"),
                    "413": _err("Too large"),
                    "415": _err("Unsupported format"),
                },
            },
        },
        "/tracks/index-directory": {
            "post": {
                "tags": ["Tracks"],
                "summary": "Index every supported file below a server-side folder",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["directory_path"],
                                "properties": {
                                    "directory_path": {"type": "string"},
                                    "recursive": {"type": "boolean", "default": True},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "202": _json("JobAccepted", "Accepted"),
                    "400": _err("Bad path / no media files"),
                    "403": _err("Path outside AUDIOFP_INDEX_ROOTS or feature disabled"),
                },
            }
        },
        "/tracks/bulk-delete": {
            "post": {
                "tags": ["Tracks"],
                "summary": "Delete many tracks",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["track_ids"],
                                "properties": {
                                    "track_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 1000},
                                    "delete_files": {"type": "boolean", "default": False, "description": "Also remove uploaded files from disk"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"deleted": {"type": "integer"}, "requested": {"type": "integer"}, "files_removed": {"type": "integer"}},
                                }
                            }
                        },
                    }
                },
            }
        },
        "/tracks/{track_id}": {
            "get": {
                "tags": ["Tracks"],
                "summary": "Get one track",
                "parameters": [track_id_param],
                "responses": {"200": _json("Track"), "404": _err("Not found")},
            },
            "patch": {
                "tags": ["Tracks"],
                "summary": "Edit title, artist, tags or custom metadata",
                "parameters": [track_id_param],
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TrackEdit"}}}},
                "responses": {"200": _json("Track"), "400": _err("Bad input"), "404": _err("Not found")},
            },
            "delete": {
                "tags": ["Tracks"],
                "summary": "Delete a track and its fingerprints",
                "parameters": [
                    track_id_param,
                    {
                        "name": "delete_file",
                        "in": "query",
                        "schema": {"type": "boolean", "default": False},
                        "description": "Also remove the uploaded file (only files inside the upload folder are ever removed).",
                    },
                ],
                "responses": {"200": {"description": "Deleted"}, "404": _err("Not found")},
            },
        },
        "/tracks/{track_id}/audio": {
            "get": {
                "tags": ["Tracks"],
                "summary": "Stream the original file (supports Range requests)",
                "parameters": [
                    track_id_param,
                    {"name": "download", "in": "query", "schema": {"type": "boolean", "default": False}},
                    {
                        "name": "token",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Short-lived stream token from GET /tracks/{track_id}/stream-token - how <audio src> elements authenticate when an API key is required.",
                    },
                ],
                "responses": {"200": {"description": "Audio bytes"}, "206": {"description": "Partial content"}, "404": _err("Track or file not found")},
            }
        },
        "/tracks/{track_id}/stream-token": {
            "get": {
                "tags": ["Tracks"],
                "summary": "Mint a short-lived, track-scoped token for audio streaming",
                "description": "Only needed when the server requires an API key: <audio> elements cannot send headers. The token expires after an hour and grants access to this track's audio only.",
                "parameters": [track_id_param],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "token": {"type": "string", "nullable": True},
                                        "expires_at": {"type": "number", "nullable": True},
                                        "url": {"type": "string"},
                                        "auth_required": {"type": "boolean"},
                                    },
                                }
                            }
                        },
                    },
                    "404": _err("Not found"),
                },
            }
        },
        "/tags": {"get": {"tags": ["Tracks"], "summary": "Distinct tags with counts", "responses": {"200": {"description": "OK"}}}},
        "/jobs": {
            "get": {
                "tags": ["Jobs"],
                "summary": "List background jobs",
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "`active`, or comma-separated: pending, running, completed, failed, cancelled, interrupted",
                    },
                    {"name": "type", "in": "query", "schema": {"type": "string", "enum": ["upload", "directory"]}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 100}},
                    {"name": "include_errors", "in": "query", "schema": {"type": "boolean", "default": False}},
                ],
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/jobs/{job_id}": {
            "get": {
                "tags": ["Jobs"],
                "summary": "Job status and progress",
                "parameters": [job_id_param],
                "responses": {"200": _json("Job"), "404": _err("Not found")},
            },
            "delete": {
                "tags": ["Jobs"],
                "summary": "Remove a finished job from history",
                "parameters": [job_id_param],
                "responses": {"200": {"description": "Removed"}, "400": _err("Still running")},
            },
        },
        "/jobs/{job_id}/cancel": {
            "post": {
                "tags": ["Jobs"],
                "summary": "Cancel a queued or running job",
                "parameters": [job_id_param],
                "responses": {"200": _json("Job"), "400": _err("Already finished")},
            }
        },
        "/health": {
            "get": {
                "tags": ["System"],
                "summary": "Liveness/readiness probe (no auth)",
                "security": [],
                "responses": {"200": {"description": "Healthy"}, "503": {"description": "Storage unavailable"}},
            }
        },
        "/info": {"get": {"tags": ["System"], "summary": "Capabilities, formats, limits, defaults", "responses": {"200": {"description": "OK"}}}},
        "/stats": {"get": {"tags": ["System"], "summary": "Library statistics", "responses": {"200": {"description": "OK"}}}},
        "/settings": {
            "get": {"tags": ["System"], "summary": "Runtime search defaults", "responses": {"200": {"description": "OK"}}},
            "put": {
                "tags": ["System"],
                "summary": "Update runtime search defaults (persisted)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "top_k": {"type": "integer"},
                                    "min_confidence": {"type": "number"},
                                    "min_aligned_hashes": {"type": "integer"},
                                    "min_peak_ratio": {"type": "number"},
                                    "mode": {"type": "string", "enum": ["identify", "occurrences"]},
                                },
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}, "400": _err("Bad input")},
            },
        },
        "/openapi.json": {"get": {"tags": ["System"], "summary": "This document", "responses": {"200": {"description": "OK"}}}},
    }

    deprecated = {
        "/upload": ("post", "/tracks"),
        "/index": ("post", "/tracks/index-directory"),
        "/songs": ("get", "/tracks"),
        "/songs/{track_id}": ("get", "/tracks/{track_id}"),
        "/songs/{track_id}/play": ("get", "/tracks/{track_id}/audio"),
        "/tracks/{track_id}/play": ("get", "/tracks/{track_id}/audio"),
    }
    for path, (method, target) in deprecated.items():
        params = [track_id_param] if "{track_id}" in path else []
        paths[path] = {
            method: {
                "tags": ["Deprecated"],
                "deprecated": True,
                "summary": f"Alias of {method.upper()} {target}",
                "parameters": params,
                "responses": {"200": {"description": "See the target endpoint"}},
            }
        }
    paths["/songs/{track_id}"]["delete"] = {
        "tags": ["Deprecated"],
        "deprecated": True,
        "summary": "Alias of DELETE /tracks/{track_id}",
        "parameters": [track_id_param],
        "responses": {"200": {"description": "Deleted"}},
    }
    # Method aliases.
    paths["/tracks/{track_id}"]["put"] = {**paths["/tracks/{track_id}"]["patch"], "summary": "Alias of PATCH /tracks/{track_id}"}
    paths["/settings"]["patch"] = {**paths["/settings"]["put"], "summary": "Alias of PUT /settings"}

    occurrence_schema = {
        "type": "object",
        "properties": {
            "offset_sec": {"type": "number", "description": "track time - query time (signed)"},
            "track_offset_sec": {"type": "number", "description": "Where the aligned query audio starts inside the track (>= 0)"},
            "query_offset_sec": {"type": "number", "description": "Where the track's content starts inside the query (>= 0)"},
            "query_start_sec": {"type": "number"},
            "query_end_sec": {"type": "number"},
            "track_start_sec": {"type": "number"},
            "track_end_sec": {"type": "number"},
            "aligned_hashes": {"type": "integer"},
            "confidence": {"type": "number"},
            "peak_ratio": {"type": "number"},
            "quality": {"type": "string", "enum": ["strong", "likely", "weak"]},
        },
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AudioFP API",
            "version": __version__,
            "description": "Self-hosted audio fingerprinting: identify clips and find audio patterns across a private library.",
        },
        "servers": [{"url": "/api/v1"}],
        "security": security,
        "tags": [{"name": "Search"}, {"name": "Tracks"}, {"name": "Jobs"}, {"name": "System"}, {"name": "Deprecated"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
                "BearerToken": {"type": "http", "scheme": "bearer"},
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string", "description": "Human readable message"},
                        "code": {"type": "string", "description": "Stable machine code, e.g. validation_error, unsupported_format, ffmpeg_not_found"},
                        "status": {"type": "integer"},
                        "details": {"type": "object"},
                        "request_id": {"type": "string"},
                    },
                },
                "Occurrence": occurrence_schema,
                "Match": {
                    "type": "object",
                    "properties": {
                        "track_id": {"type": "string"},
                        "title": {"type": "string"},
                        "artist": {"type": "string"},
                        "filename": {"type": "string"},
                        "duration": {"type": "number"},
                        "source_type": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "description": "aligned hashes / query hashes inside the matched span (0-1)"},
                        "aligned_hashes": {"type": "integer"},
                        "peak_ratio": {"type": "number", "description": "spike height relative to the histogram background"},
                        "quality": {"type": "string", "enum": ["strong", "likely", "weak"]},
                        "offset_sec": {"type": "number"},
                        "track_offset_sec": {"type": "number"},
                        "query_offset_sec": {"type": "number"},
                        "query_start_sec": {"type": "number"},
                        "query_end_sec": {"type": "number"},
                        "track_start_sec": {"type": "number"},
                        "track_end_sec": {"type": "number"},
                        "match_offset_sec": {"type": "number", "deprecated": True},
                        "occurrences": {"type": "array", "items": {"$ref": "#/components/schemas/Occurrence"}},
                    },
                },
                "SearchResponse": {
                    "type": "object",
                    "properties": {
                        "found": {"type": "boolean"},
                        "mode": {"type": "string"},
                        "matches": {"type": "array", "items": {"$ref": "#/components/schemas/Match"}},
                        "query": {
                            "type": "object",
                            "properties": {
                                "filename": {"type": "string"},
                                "duration_sec": {"type": "number"},
                                "num_peaks": {"type": "integer"},
                                "num_hashes": {"type": "integer"},
                                "truncated": {"type": "boolean"},
                            },
                        },
                        "thresholds": {"type": "object"},
                        "processing_time_ms": {"type": "number"},
                        "diagnostics": {
                            "type": "object",
                            "description": "query_hashes, db_rows, votes, candidate_tracks, scored_tracks, skipped_common_hashes, dropped_for_vote_cap",
                        },
                    },
                },
                "Track": {
                    "type": "object",
                    "properties": {
                        "track_id": {"type": "string"},
                        "title": {"type": "string"},
                        "artist": {"type": "string"},
                        "filename": {"type": "string"},
                        "filepath": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "duration": {"type": "number"},
                        "num_peaks": {"type": "integer"},
                        "num_hashes": {"type": "integer"},
                        "source_type": {"type": "string"},
                        "file_size": {"type": "integer"},
                        "indexed_at": {"type": "number"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                        "display_name": {"type": "string"},
                    },
                },
                "TrackEdit": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "artist": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                },
                "TrackPage": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"$ref": "#/components/schemas/Track"}},
                        "total": {"type": "integer"},
                        "page": {"type": "integer"},
                        "per_page": {"type": "integer"},
                        "pages": {"type": "integer"},
                    },
                },
                "Job": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "type": {"type": "string"},
                        "label": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "running", "completed", "failed", "cancelled", "interrupted"]},
                        "created_at": {"type": "number"},
                        "started_at": {"type": "number", "nullable": True},
                        "finished_at": {"type": "number", "nullable": True},
                        "total": {"type": "integer"},
                        "completed": {"type": "integer"},
                        "succeeded": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "skipped": {"type": "integer", "description": "duplicates"},
                        "current_item": {"type": "string", "nullable": True},
                        "percent": {"type": "number"},
                        "elapsed_sec": {"type": "number"},
                        "eta_sec": {"type": "number", "nullable": True},
                        "rate_per_sec": {"type": "number"},
                        "error_count": {"type": "integer"},
                        "errors": {"type": "array", "items": {"type": "object"}},
                        "result": {"type": "object", "nullable": True},
                        "error": {"type": "string", "nullable": True},
                        "cancel_requested": {"type": "boolean"},
                        "meta": {"type": "object"},
                    },
                },
                "JobAccepted": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "job": {"$ref": "#/components/schemas/Job"},
                        "message": {"type": "string"},
                        "total_files": {"type": "integer"},
                        "filename": {"type": "string"},
                    },
                },
            },
        },
    }
