"""Filesystem helpers shared by the API, indexer and CLI."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._ \-()\[\]]+")


def sha256_file(path: str | os.PathLike, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file through SHA-256 without loading it into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


def safe_filename(name: str, fallback: str = "upload") -> str:
    """Return a filesystem-safe version of *name* that keeps its extension.

    Unlike :func:`werkzeug.utils.secure_filename`, non-ASCII names are
    transliterated where possible and never collapse to an empty string, so
    uploads named in Hindi, Chinese, etc. keep a usable name and - crucially -
    their extension (which the decoder relies on to pick a backend).
    """
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = _UNSAFE.sub("_", stem).strip(" ._")
    if not stem:
        stem = fallback
    ext = _UNSAFE.sub("", ext)
    return (stem[:150] + ext[:16]) if ext else stem[:150]


def is_within(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True if *path* resolves to a location inside *root* (or equals it)."""
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def human_size(num_bytes: float) -> str:
    """Format a byte count for humans (``1.5 MB``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def free_space_bytes(path: str | os.PathLike) -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None
