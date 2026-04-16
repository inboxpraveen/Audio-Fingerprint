"""Utility modules."""

from .logger import setup_logger
from .exceptions import (
    FingerprintException,
    AudioProcessingError,
    StorageError,
    MatchingError,
    ValidationError,
)

__all__ = [
    "setup_logger",
    "FingerprintException",
    "AudioProcessingError",
    "StorageError",
    "MatchingError",
    "ValidationError",
]
