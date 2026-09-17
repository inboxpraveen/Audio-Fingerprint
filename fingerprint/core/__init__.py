"""Signal-processing core: decoding, peak extraction, hashing and matching."""

from .decoder import ffmpeg_available, ffmpeg_info, iter_audio_chunks, load_audio
from .fingerprinter import Fingerprint, Fingerprinter, PeakExtractor
from .hash_generator import decode_hash, encode_hash, generate_hashes
from .matcher import MODES, Matcher, MatchOptions, Occurrence, TrackMatch, quality_label

__all__ = [
    "MODES",
    "Fingerprint",
    "Fingerprinter",
    "MatchOptions",
    "Matcher",
    "Occurrence",
    "PeakExtractor",
    "TrackMatch",
    "decode_hash",
    "encode_hash",
    "ffmpeg_available",
    "ffmpeg_info",
    "generate_hashes",
    "iter_audio_chunks",
    "load_audio",
    "quality_label",
]
