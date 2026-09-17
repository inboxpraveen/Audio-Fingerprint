"""Opt-in scale tests: run with ``pytest -m slow`` (they take a minute and need ~200 MB of temp disk)."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
import soundfile as sf

from fingerprint.config import Settings
from fingerprint.core import Fingerprinter, Matcher, MatchOptions
from fingerprint.storage import SQLiteStore, TrackRecord

psutil = pytest.importorskip("psutil")

pytestmark = pytest.mark.slow

SR = 22050


def _long_signal(seed: int, seconds: int) -> np.ndarray:
    from .conftest import synth_signal

    return synth_signal(seed=seed, seconds=seconds)


def test_hour_long_file_memory_stays_flat(tmp_path):
    seconds = 30 * 60
    path = tmp_path / "long.wav"
    with sf.SoundFile(str(path), "w", samplerate=SR, channels=1, subtype="PCM_16") as fh:
        for minute in range(seconds // 60):
            fh.write(_long_signal(seed=10_000 + minute, seconds=60))

    proc = psutil.Process()
    baseline = proc.memory_info().rss
    peak = [baseline]
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.05)

    threading.Thread(target=watch, daemon=True).start()
    settings = Settings.load(dotenv=False, env={})
    started = time.time()
    fp = Fingerprinter(settings).fingerprint_file(str(path))
    elapsed = time.time() - started
    stop.set()

    growth_mb = (peak[0] - baseline) / 1e6
    print(f"\n30-min file: {fp.num_peaks} peaks, {fp.num_hashes} hashes in {elapsed:.1f}s; RSS growth {growth_mb:.0f} MB")
    assert fp.duration_sec == pytest.approx(seconds, abs=0.1)
    assert growth_mb < 300, f"memory grew by {growth_mb:.0f} MB - streaming is broken"

    # the whole file can be stored and searched
    store = SQLiteStore(str(tmp_path / "scale.db"))
    store.add_track(TrackRecord(title="long", num_hashes=fp.num_hashes, duration=fp.duration_sec), fp.hashes, fp.hash_times)
    clip, _ = sf.read(str(path), dtype="float32", start=int(1234.5 * SR), frames=5 * SR)
    query = Fingerprinter(settings).fingerprint_array(__import__("soxr").resample(clip, SR, settings.sample_rate))
    started = time.time()
    matches = Matcher(settings).match(query, store, MatchOptions.from_settings(settings))
    print(f"search in {1000 * (time.time() - started):.0f} ms")
    assert matches and matches[0].track.title == "long"
    assert query.frames_to_seconds(matches[0].offset_frames) == pytest.approx(1234.5, abs=0.15)
    store.close()
