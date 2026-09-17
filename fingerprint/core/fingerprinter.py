"""Spectral peak extraction (the "constellation map") with flat memory usage.

The STFT is computed with numpy over a stream of audio chunks. It uses a
periodic Hann window and ``center=True`` style zero padding, so the result is
identical to ``librosa.stft`` with default arguments. Peaks are local maxima of
the magnitude spectrogram inside a ``peak_neighborhood_size`` square window
that exceed ``min_amplitude``.

:class:`PeakExtractor` keeps just enough spectrogram columns from the previous
chunk to give every frame full context on both sides. Chunked processing
therefore yields exactly the same peaks as processing the whole signal at
once, and a test asserts that.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy.ndimage import maximum_filter
from scipy.signal import get_window

from ..config import Settings
from ..utils.exceptions import AudioProcessingError
from . import decoder
from .hash_generator import generate_hashes

logger = logging.getLogger(__name__)


@dataclass
class Fingerprint:
    """Everything the fingerprinting pipeline produces for one piece of audio."""

    peak_times: np.ndarray  # int32 frame indices
    peak_freqs: np.ndarray  # int32 frequency bins
    hashes: np.ndarray  # int64
    hash_times: np.ndarray  # int32 anchor frame per hash
    num_samples: int
    sample_rate: int
    hop_length: int
    gain: float = 1.0
    extra: dict = field(default_factory=dict)

    @property
    def num_peaks(self) -> int:
        return int(self.peak_times.size)

    @property
    def num_hashes(self) -> int:
        return int(self.hashes.size)

    @property
    def duration_sec(self) -> float:
        return self.num_samples / float(self.sample_rate)

    @property
    def num_frames(self) -> int:
        return int(self.peak_times.max()) + 1 if self.peak_times.size else 0

    def frames_to_seconds(self, frames: float) -> float:
        return float(frames) * self.hop_length / self.sample_rate


class PeakExtractor:
    """Incremental STFT and local-maximum peak picking over a chunk stream."""

    def __init__(self, n_fft: int, hop_length: int, neighborhood: int, min_amplitude: float, normalize: bool = False):
        if n_fft <= 0 or n_fft % 2:
            raise ValueError("n_fft must be a positive even number")
        if not (0 < hop_length <= n_fft):
            raise ValueError("hop_length must be in (0, n_fft]")
        self.n_fft = int(n_fft)
        self.hop = int(hop_length)
        self.neighborhood = int(neighborhood)
        self.min_amplitude = float(min_amplitude)
        self.pad = self.n_fft // 2
        # scipy's max filter looks size//2 back and (size-1)//2 ahead; keep a full window as margin.
        self.margin = max(self.neighborhood, 1)
        self.window = get_window("hann", self.n_fft, fftbins=True).astype(np.float32)
        # Peak normalisation in a single pass. Local maxima don't depend on gain, only the amplitude
        # threshold does, and the peak sample isn't known until the end. So candidates are collected
        # against the running peak (a superset of the final set) and finish() applies the exact
        # threshold min_amplitude * final_peak.
        self.normalize = bool(normalize)
        self._peak_sample = 0.0
        self._peaks_mag: list[np.ndarray] = []

        self._buf = np.zeros(self.pad, dtype=np.float32)  # left zero padding (center=True)
        self._ctx = np.zeros((self.n_fft // 2 + 1, 0), dtype=np.float32)  # finalised columns kept as context
        self._pending = np.zeros((self.n_fft // 2 + 1, 0), dtype=np.float32)  # not yet finalised
        self._pending_start = 0  # absolute frame index of self._pending[:, 0]
        self._num_samples = 0
        self._peaks_t: list[np.ndarray] = []
        self._peaks_f: list[np.ndarray] = []
        self._finished = False

    # ------------------------------------------------------------------ STFT
    def _spectrogram_columns(self, samples: np.ndarray) -> np.ndarray:
        """Magnitude columns for every complete frame in *samples* (shape: bins x frames)."""
        if samples.size < self.n_fft:
            return np.zeros((self.n_fft // 2 + 1, 0), dtype=np.float32)
        frames = sliding_window_view(samples, self.n_fft)[:: self.hop]
        spec = np.fft.rfft(frames * self.window, axis=1)
        return np.abs(spec).T.astype(np.float32, copy=False)

    def _consume_buffer(self, final: bool) -> np.ndarray:
        """Frame as much of the buffer as possible; return new spectrogram columns."""
        n_frames = 0 if self._buf.size < self.n_fft else (self._buf.size - self.n_fft) // self.hop + 1
        if n_frames == 0:
            return np.zeros((self.n_fft // 2 + 1, 0), dtype=np.float32)
        usable = (n_frames - 1) * self.hop + self.n_fft
        cols = self._spectrogram_columns(self._buf[:usable])
        self._buf = self._buf[n_frames * self.hop :]
        return cols

    # ------------------------------------------------------------------ peaks
    def _threshold(self) -> float:
        return self.min_amplitude * self._peak_sample if self.normalize else self.min_amplitude

    def _pick(self, block: np.ndarray, first: int, last: int, block_start: int) -> None:
        """Emit peaks for columns [first, last) of *block* whose context is complete."""
        if last <= first:
            return
        local_max = maximum_filter(block, size=self.neighborhood, mode="reflect") == block
        window = block[:, first:last]
        mask = local_max[:, first:last] & (window > self._threshold())
        f_idx, t_idx = np.nonzero(mask)
        if f_idx.size:
            self._peaks_t.append((t_idx + block_start + first).astype(np.int32))
            self._peaks_f.append(f_idx.astype(np.int32))
            if self.normalize:
                self._peaks_mag.append(window[f_idx, t_idx])

    def push(self, chunk: np.ndarray) -> None:
        """Feed the next chunk of float32 mono samples."""
        if self._finished:
            raise RuntimeError("PeakExtractor already finished")
        chunk = decoder.to_mono_float32(chunk)
        if self.normalize and chunk.size:
            self._peak_sample = max(self._peak_sample, float(np.abs(chunk).max()))
        self._num_samples += int(chunk.size)
        self._buf = np.concatenate([self._buf, chunk]) if self._buf.size else chunk
        self._process(final=False)

    def _process(self, final: bool) -> None:
        new_cols = self._consume_buffer(final)
        block = np.concatenate([self._ctx, self._pending, new_cols], axis=1)
        n_ctx = self._ctx.shape[1]
        block_start = self._pending_start - n_ctx
        total = block.shape[1]

        if final:
            self._pick(block, n_ctx, total, block_start)
            self._pending = block[:, total:]
            self._pending_start = block_start + total
            self._ctx = block[:, :0]
            return

        last = total - self.margin
        if last <= n_ctx:
            # Not enough look-ahead yet: keep everything pending.
            self._pending = block[:, n_ctx:]
            self._pending_start = block_start + n_ctx
            return

        self._pick(block, n_ctx, last, block_start)
        ctx_from = max(0, last - self.margin)
        self._ctx = np.ascontiguousarray(block[:, ctx_from:last])
        self._pending = np.ascontiguousarray(block[:, last:])
        self._pending_start = block_start + last

    @property
    def gain(self) -> float:
        """Normalisation gain that was (implicitly) applied: 1 / peak sample."""
        return 1.0 / self._peak_sample if (self.normalize and self._peak_sample > 0) else 1.0

    def finish(self) -> tuple[np.ndarray, np.ndarray, int]:
        """Flush remaining frames. Returns ``(peak_times, peak_freqs, num_samples)``."""
        if not self._finished:
            self._finished = True
            self._buf = np.concatenate([self._buf, np.zeros(self.pad, dtype=np.float32)])
            self._process(final=True)
        if self._peaks_t:
            t = np.concatenate(self._peaks_t)
            f = np.concatenate(self._peaks_f)
            if self.normalize:
                # Exact final threshold now that the peak sample is known (equivalent to scaling the
                # audio by 1/peak before thresholding).
                mag = np.concatenate(self._peaks_mag)
                keep = mag > self._threshold()
                t, f = t[keep], f[keep]
            order = np.lexsort((f, t))
            t, f = t[order], f[order]
        else:
            t = np.zeros(0, dtype=np.int32)
            f = np.zeros(0, dtype=np.int32)
        return t, f, self._num_samples


class Fingerprinter:
    """Turns a file, an in-memory array or a chunk stream into a :class:`Fingerprint`."""

    def __init__(self, settings: Settings | None = None, **overrides):
        self.settings = settings or Settings.load(dotenv=False, **overrides)
        s = self.settings
        self.sample_rate = s.sample_rate
        self.n_fft = s.n_fft
        self.hop_length = s.hop_length

    # ------------------------------------------------------------------ helpers
    def _extractor(self, normalize: bool) -> PeakExtractor:
        s = self.settings
        return PeakExtractor(s.n_fft, s.hop_length, s.peak_neighborhood_size, s.min_amplitude, normalize=normalize)

    def _finish(self, extractor: PeakExtractor, extra: dict | None = None) -> Fingerprint:
        s = self.settings
        t, f, num_samples = extractor.finish()
        gain = extractor.gain
        hashes, hash_times = generate_hashes(t, f, s.fan_value, s.min_hash_time_delta, s.max_hash_time_delta)
        return Fingerprint(
            peak_times=t,
            peak_freqs=f,
            hashes=hashes,
            hash_times=hash_times,
            num_samples=num_samples,
            sample_rate=s.sample_rate,
            hop_length=s.hop_length,
            gain=gain,
            extra=extra or {},
        )

    def frames_to_seconds(self, frames: float) -> float:
        return float(frames) * self.hop_length / self.sample_rate

    def seconds_to_frames(self, seconds: float) -> int:
        return round(float(seconds) * self.sample_rate / self.hop_length)

    # ------------------------------------------------------------------ entry points
    def fingerprint_chunks(self, chunks: Iterable[np.ndarray], normalize: bool = True, extra: dict | None = None) -> Fingerprint:
        """Fingerprint a stream of float32 mono chunks already at the working sample rate.

        With ``normalize=True`` the result equals fingerprinting the peak-normalised
        signal, computed in a single pass (see :class:`PeakExtractor`).
        """
        extractor = self._extractor(normalize)
        for chunk in chunks:
            extractor.push(chunk)
        return self._finish(extractor, extra)

    def fingerprint_array(self, audio: np.ndarray, normalize: bool = True) -> Fingerprint:
        """Fingerprint an in-memory signal (assumed to be at ``settings.sample_rate``)."""
        audio = decoder.to_mono_float32(audio)
        chunk = max(int(self.settings.chunk_seconds * self.sample_rate), self.n_fft)
        return self.fingerprint_chunks(self._slices(audio, chunk), normalize=normalize)

    @staticmethod
    def _slices(audio: np.ndarray, size: int) -> Iterator[np.ndarray]:
        for start in range(0, audio.size, size):
            yield audio[start : start + size]

    def fingerprint_file(
        self,
        path: str,
        *,
        max_seconds: float | None = None,
        normalize: bool = True,
        backend: str = "auto",
        display_name: str | None = None,
    ) -> Fingerprint:
        """Decode and fingerprint *path* in one streaming pass with flat memory usage.

        With ``normalize=True`` (the default) you get the fingerprint of the
        peak-normalised recording without decoding it twice.
        """
        s = self.settings
        name = display_name or os.path.basename(path)
        kwargs = {"max_seconds": max_seconds, "backend": backend, "display_name": name}
        try:
            fp = self.fingerprint_chunks(
                decoder.iter_audio_chunks(path, s.sample_rate, s.chunk_seconds, **kwargs),
                normalize=normalize,
                extra={"path": path, "name": name},
            )
        except MemoryError as exc:  # pragma: no cover (environment specific)
            raise AudioProcessingError(f"Out of memory while fingerprinting '{name}'") from exc
        if fp.num_samples == 0:
            raise AudioProcessingError(f"'{name}' decoded to zero samples. The file may be empty or truncated.", code="empty_audio")
        return fp
