"""Streaming audio decoding.

Audio is never loaded into memory in one piece. :func:`iter_audio_chunks`
yields fixed-size float32 mono blocks at the working sample rate whatever the
source container is, so fingerprinting an hour-long call recording needs the
same few megabytes as a ten-second clip.

There are two backends. soundfile (libsndfile) handles WAV, FLAC, OGG/Opus,
MP3, AIFF and the other formats libsndfile reads on its own, with no external
binary. Resampling goes through the streaming resampler in ``soxr``, which
produces the same output bit for bit as resampling the whole signal at once.

ffmpeg handles everything else (M4A/AAC, WMA, every video container). PCM is
read from ffmpeg's stdout pipe. A helper thread drains stderr, so a chatty
decoder can't fill the pipe and deadlock the pipeline.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .. import formats
from ..utils.exceptions import AudioDecodeError, FFmpegNotFoundError, UnsupportedFormatError

logger = logging.getLogger(__name__)

DEFAULT_FFMPEG = os.environ.get("AUDIOFP_FFMPEG_BINARY", "ffmpeg")

FFMPEG_INSTALL_HINT = (
    "Install ffmpeg and make sure it is on PATH: Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg | Debian/Ubuntu: sudo apt install ffmpeg"
)


@dataclass(frozen=True)
class FFmpegInfo:
    available: bool
    path: str | None = None
    version: str | None = None


@lru_cache(maxsize=8)
def ffmpeg_info(binary: str = DEFAULT_FFMPEG) -> FFmpegInfo:
    """Locate ffmpeg once per process (cached). Use :func:`reset_ffmpeg_cache` in tests."""
    resolved = shutil.which(binary)
    if not resolved:
        return FFmpegInfo(False)
    try:
        proc = subprocess.run([resolved, "-version"], capture_output=True, text=True, timeout=15, **_popen_flags())
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover (environment specific)
        logger.warning("ffmpeg found at %s but could not be executed: %s", resolved, exc)
        return FFmpegInfo(False, resolved)
    if proc.returncode != 0:
        return FFmpegInfo(False, resolved)
    first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    version = first_line.replace("ffmpeg version", "").strip().split(" ")[0] or None
    return FFmpegInfo(True, resolved, version)


def ffmpeg_available(binary: str = DEFAULT_FFMPEG) -> bool:
    return ffmpeg_info(binary).available


def reset_ffmpeg_cache() -> None:
    ffmpeg_info.cache_clear()


def _popen_flags() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# ---------------------------------------------------------------------------
# soundfile backend
# ---------------------------------------------------------------------------

_LIBSNDFILE_PREFIX = re.compile(r"^Error opening '.*?':\s*")


def _clean_sndfile_message(exc: Exception) -> str:
    """libsndfile puts the full path in its messages. Keep only the reason."""
    return _LIBSNDFILE_PREFIX.sub("", str(exc)).strip() or type(exc).__name__


def _iter_soundfile(path: str, sample_rate: int, chunk_seconds: float, max_samples: int | None, name: str) -> Iterator[np.ndarray]:
    import soundfile as sf
    import soxr

    try:
        handle = sf.SoundFile(path)
    except Exception as exc:  # libsndfile raises several exception types depending on version
        raise AudioDecodeError(f"Could not decode '{name}': {_clean_sndfile_message(exc)}") from exc

    with handle:
        src_rate = int(handle.samplerate)
        channels = int(handle.channels)
        if src_rate <= 0 or channels <= 0:
            raise AudioDecodeError(f"'{name}' reports an invalid sample rate or channel count")

        resampler = None
        if src_rate != sample_rate:
            resampler = soxr.ResampleStream(src_rate, sample_rate, 1, dtype="float32", quality="HQ")

        block = max(int(chunk_seconds * src_rate), 4096)
        produced = 0
        try:
            for data in handle.blocks(blocksize=block, dtype="float32", always_2d=True):
                mono = data.mean(axis=1) if channels > 1 else data[:, 0]
                mono = np.ascontiguousarray(mono, dtype=np.float32)
                if resampler is not None:
                    mono = resampler.resample_chunk(mono, last=False)
                if mono.size == 0:
                    continue
                if max_samples is not None and produced + mono.size >= max_samples:
                    yield mono[: max_samples - produced]
                    return
                produced += mono.size
                yield mono
        except AudioDecodeError:
            raise
        except Exception as exc:
            raise AudioDecodeError(f"Error while decoding '{name}': {_clean_sndfile_message(exc)}") from exc

        if resampler is not None:
            tail = resampler.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
            if tail.size and (max_samples is None or produced < max_samples):
                yield tail[: None if max_samples is None else max_samples - produced]


# ---------------------------------------------------------------------------
# ffmpeg backend
# ---------------------------------------------------------------------------


def _iter_ffmpeg(
    path: str,
    sample_rate: int,
    chunk_seconds: float,
    max_samples: int | None,
    binary: str,
    timeout: float | None,
    name: str,
) -> Iterator[np.ndarray]:
    info = ffmpeg_info(binary)
    if not info.available:
        raise FFmpegNotFoundError(
            f"'{name}' needs ffmpeg to decode, but ffmpeg was not found. {FFMPEG_INSTALL_HINT}",
            details={"binary": binary},
        )

    cmd = [
        info.path or binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-vn",
        "-sn",
        "-dn",  # audio only
        "-ac",
        "1",  # mono
        "-ar",
        str(sample_rate),  # resample
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]
    if max_samples is not None:
        # tell ffmpeg where to stop, so it doesn't decode an hour we'd throw away
        cmd[-1:-1] = ["-t", f"{max_samples / sample_rate + 1:.3f}"]

    logger.debug("ffmpeg decode: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_popen_flags())
    except OSError as exc:  # pragma: no cover (environment specific)
        raise FFmpegNotFoundError(f"Could not start ffmpeg: {exc}. {FFMPEG_INSTALL_HINT}") from exc

    stderr_tail: collections.deque[str] = collections.deque(maxlen=20)

    def _drain() -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_tail.append(line)

    drain_thread = threading.Thread(target=_drain, name="ffmpeg-stderr", daemon=True)
    drain_thread.start()

    timer = None
    timed_out = threading.Event()
    if timeout:

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()

    bytes_per_chunk = max(int(chunk_seconds * sample_rate), 4096) * 2
    produced = 0
    total_bytes = 0
    finished = False  # True once ffmpeg closed its stdout (normal end of stream)
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(bytes_per_chunk)
            if not buf:
                finished = True
                break
            total_bytes += len(buf)
            if len(buf) % 2:
                buf = buf[:-1]
            samples = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
            if max_samples is not None and produced + samples.size >= max_samples:
                yield samples[: max_samples - produced]
                produced = max_samples
                return
            produced += samples.size
            yield samples
    finally:
        if timer:
            timer.cancel()
        if not finished and proc.poll() is None:
            # the consumer stopped early (truncation, or the generator was closed), so stop ffmpeg too
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover (defensive)
            proc.kill()
        drain_thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:  # pragma: no cover
                pass

    if timed_out.is_set():
        raise AudioDecodeError(f"ffmpeg timed out after {timeout:.0f}s decoding '{name}'")
    if proc.returncode not in (0, None):
        detail = " | ".join(stderr_tail) or "no error output"
        raise AudioDecodeError(
            f"ffmpeg could not decode '{name}': {detail}",
            details={"ffmpeg_exit_code": proc.returncode},
        )
    if total_bytes == 0:
        detail = " | ".join(stderr_tail)
        raise AudioDecodeError(f"'{name}' contains no decodable audio stream" + (f" ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_backend(path: str, ffmpeg_binary: str = DEFAULT_FFMPEG, display_name: str | None = None) -> str:
    """Return ``'soundfile'`` or ``'ffmpeg'`` for *path*, raising if neither can handle it."""
    name = display_name or os.path.basename(path)
    ext = formats.extension_of(name)
    if ext in formats.NATIVE_AUDIO_EXTENSIONS:
        return "soundfile"
    if formats.needs_ffmpeg(path):
        if ffmpeg_available(ffmpeg_binary):
            return "ffmpeg"
        raise FFmpegNotFoundError(
            f"'{name}' ({ext}) requires ffmpeg, which is not installed. {FFMPEG_INSTALL_HINT}",
            details={"extension": ext},
        )
    if not ext:
        # no extension: let libsndfile sniff the content, iter_audio_chunks falls back to ffmpeg if that fails
        return "soundfile"
    raise UnsupportedFormatError(
        f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(e.lstrip('.') for e in formats.SUPPORTED_EXTENSIONS))}",
        details={"extension": ext},
    )


def iter_audio_chunks(
    path: str,
    sample_rate: int,
    chunk_seconds: float = 30.0,
    *,
    max_seconds: float | None = None,
    backend: str = "auto",
    ffmpeg_binary: str = DEFAULT_FFMPEG,
    ffmpeg_timeout: float | None = 3600.0,
    display_name: str | None = None,
) -> Iterator[np.ndarray]:
    """Yield float32 mono chunks of *path* resampled to *sample_rate*.

    Args:
        path:           Any supported audio or video file.
        sample_rate:    Target sample rate.
        chunk_seconds:  Approximate chunk length in seconds.
        max_seconds:    Stop after this much audio (truncates long queries cheaply).
        backend:        ``auto`` (default), ``soundfile`` or ``ffmpeg``.
        ffmpeg_binary:  Name or path of the ffmpeg executable.
        ffmpeg_timeout: Kill ffmpeg after this many seconds (None = no limit).
        display_name:   Name used in error messages (e.g. the original upload name).

    Raises:
        AudioDecodeError, UnsupportedFormatError, FFmpegNotFoundError
    """
    name = display_name or os.path.basename(path)
    if not os.path.isfile(path):
        raise AudioDecodeError(f"File not found: {name}", code="file_not_found", http_status=404)
    if os.path.getsize(path) == 0:
        raise AudioDecodeError(f"'{name}' is empty (0 bytes)")

    max_samples = int(max_seconds * sample_rate) if max_seconds else None
    chosen = backend if backend != "auto" else select_backend(path, ffmpeg_binary, name)

    if chosen == "soundfile":
        yielded = 0
        try:
            for chunk in _iter_soundfile(path, sample_rate, chunk_seconds, max_samples, name):
                yielded += 1
                yield chunk
            return
        except AudioDecodeError as exc:
            if yielded:
                # Failed mid-stream: replaying from the start through ffmpeg would duplicate audio.
                raise
            # libsndfile builds differ (e.g. MP3 support). Fall back to ffmpeg when we can.
            if backend == "auto" and ffmpeg_available(ffmpeg_binary):
                logger.info("soundfile failed on %s (%s); retrying with ffmpeg", name, exc)
                yield from _iter_ffmpeg(path, sample_rate, chunk_seconds, max_samples, ffmpeg_binary, ffmpeg_timeout, name)
                return
            if backend == "auto":
                raise AudioDecodeError(
                    f"{exc.message.rstrip('.')}. The file may be corrupted, or in a format that needs ffmpeg. {FFMPEG_INSTALL_HINT}",
                    details=exc.details,
                ) from exc
            raise
    elif chosen == "ffmpeg":
        yield from _iter_ffmpeg(path, sample_rate, chunk_seconds, max_samples, ffmpeg_binary, ffmpeg_timeout, name)
    else:
        raise ValueError(f"unknown backend {chosen!r}")


def load_audio(path: str, sample_rate: int, *, max_seconds: float | None = None, **kwargs) -> np.ndarray:
    """Decode a whole file into memory (convenience for short clips, tests and the CLI)."""
    chunks = list(iter_audio_chunks(path, sample_rate, max_seconds=max_seconds, **kwargs))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks)


def to_mono_float32(audio: np.ndarray) -> np.ndarray:
    """Normalise an in-memory array to float32 mono, accepting (n,), (n, ch) or (ch, n)."""
    audio = np.asarray(audio)
    if audio.ndim == 2:
        # Treat the shorter axis as channels (both librosa's (ch, n) and soundfile's (n, ch) layouts).
        axis = 0 if audio.shape[0] < audio.shape[1] else 1
        audio = audio.mean(axis=axis)
    elif audio.ndim != 1:
        raise AudioDecodeError(f"expected a 1-D or 2-D array, got shape {audio.shape}")
    return np.ascontiguousarray(audio, dtype=np.float32)
