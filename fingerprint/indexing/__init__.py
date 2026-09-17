"""Scanning folders and indexing media files into the fingerprint store."""

from .indexer import OUTCOME_DUPLICATE, OUTCOME_FAILED, OUTCOME_INDEXED, Indexer, IndexOutcome, IndexSummary
from .progress import ProgressSnapshot, ProgressTracker, print_progress, render_bar
from .scanner import find_media_files, iter_media_files, metadata_from_filename

__all__ = [
    "OUTCOME_DUPLICATE",
    "OUTCOME_FAILED",
    "OUTCOME_INDEXED",
    "IndexOutcome",
    "IndexSummary",
    "Indexer",
    "ProgressSnapshot",
    "ProgressTracker",
    "find_media_files",
    "iter_media_files",
    "metadata_from_filename",
    "print_progress",
    "render_bar",
]
