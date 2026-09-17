"""Single source of truth for the media formats AudioFP accepts.

Every other module (validators, scanner, decoder, UI hints) imports from here so
the supported-format list can never drift between the API, the indexer and the
documentation.
"""

from __future__ import annotations

import os

# Formats libsndfile (via the ``soundfile`` package) can decode natively - no
# ffmpeg required.  MP3 support needs libsndfile >= 1.1.0, which the soundfile
# wheels have bundled since soundfile 0.12.
NATIVE_AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".wav", ".wave", ".flac", ".ogg", ".oga", ".opus", ".mp3", ".aiff", ".aif", ".aifc", ".au", ".caf", ".w64"}
)

# Audio formats that require ffmpeg to decode.
FFMPEG_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".m4a", ".aac", ".wma", ".amr", ".ac3", ".dts", ".mka", ".weba"})

# Container formats whose audio track is extracted with ffmpeg.
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".3gp", ".vob"})

AUDIO_EXTENSIONS: frozenset[str] = NATIVE_AUDIO_EXTENSIONS | FFMPEG_AUDIO_EXTENSIONS
SUPPORTED_EXTENSIONS: frozenset[str] = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def extension_of(path_or_name: str) -> str:
    """Return the lower-cased extension of *path_or_name* including the dot (empty if none)."""
    return os.path.splitext(str(path_or_name))[1].lower()


def is_supported(path_or_name: str) -> bool:
    return extension_of(path_or_name) in SUPPORTED_EXTENSIONS


def is_video(path_or_name: str) -> bool:
    return extension_of(path_or_name) in VIDEO_EXTENSIONS


def needs_ffmpeg(path_or_name: str) -> bool:
    """True when the file can only be decoded through ffmpeg."""
    ext = extension_of(path_or_name)
    return ext in VIDEO_EXTENSIONS or ext in FFMPEG_AUDIO_EXTENSIONS


def source_type_of(path_or_name: str) -> str:
    return "video" if is_video(path_or_name) else "audio"


def describe_formats() -> dict:
    """Machine-readable format summary used by ``/api/v1/info`` and ``audiofp doctor``."""
    return {
        "native_audio": sorted(e.lstrip(".") for e in NATIVE_AUDIO_EXTENSIONS),
        "ffmpeg_audio": sorted(e.lstrip(".") for e in FFMPEG_AUDIO_EXTENSIONS),
        "video": sorted(e.lstrip(".") for e in VIDEO_EXTENSIONS),
    }
