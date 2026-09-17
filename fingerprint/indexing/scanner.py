"""Discover media files on disk and derive metadata from file names."""

from __future__ import annotations

import os
from collections.abc import Iterator

from .. import formats

# Folders that are never worth scanning.
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "$RECYCLE.BIN", "System Volume Information"})


def iter_media_files(directory: str, recursive: bool = True, follow_symlinks: bool = False) -> Iterator[str]:
    """Yield absolute paths of supported media files under *directory* (sorted per folder)."""
    directory = os.path.abspath(directory)
    if not recursive:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and formats.is_supported(name):
                yield path
        return

    for root, dirs, files in os.walk(directory, followlinks=follow_symlinks, onerror=lambda _e: None):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if formats.is_supported(name):
                yield os.path.join(root, name)


def find_media_files(directory: str, recursive: bool = True, limit: int | None = None) -> list[str]:
    """Materialised version of :func:`iter_media_files`."""
    out: list[str] = []
    for path in iter_media_files(directory, recursive=recursive):
        out.append(path)
        if limit and len(out) >= limit:
            break
    return out


def metadata_from_filename(path: str) -> dict[str, str]:
    """Best-effort ``{"artist": ..., "title": ...}`` from ``Artist - Title.ext``.

    Anything that does not follow the convention gets the file stem as its title
    and an empty artist (the UI shows the filename in that case).
    """
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        artist, title = artist.strip(), title.strip()
        if artist and title:
            return {"artist": artist, "title": title}
    return {"artist": "", "title": stem}
