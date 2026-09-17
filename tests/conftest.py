"""Shared fixtures: synthetic audio, settings, storage and a Flask test client.

All audio is generated on the fly (no binary fixtures in the repo).  The
"diverse" generator mimics speech/music-like material - random pitch glides
with harmonics and noise bursts - so hashes are varied and chance matches rare.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fingerprint.config import Settings  # noqa: E402

SRC_SR = 22050


def synth_signal(seed: int, seconds: float, sr: int = SRC_SR) -> np.ndarray:
    """Deterministic pseudo-musical signal: glides + harmonics + noise, peak-normalised."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    out = np.zeros(n, dtype=np.float32)
    t = 0
    while t < n:
        dur = int(rng.uniform(0.08, 0.4) * sr)
        seg = np.arange(min(dur, n - t)) / sr
        if seg.size == 0:
            break
        f0 = rng.uniform(90, 500)
        glide = rng.uniform(-0.3, 0.3)
        phase = 2 * np.pi * np.cumsum(f0 * (1 + glide * seg / max(seg[-1], 1e-6))) / sr
        s = np.zeros_like(seg)
        for h in range(1, 10):
            s += (0.5 / h) * np.sin(h * phase + rng.uniform(0, 2 * np.pi))
        s += rng.normal(0, 0.05, seg.size)
        env = np.minimum(1, seg * 60) * np.exp(-seg * rng.uniform(2, 8))
        out[t : t + seg.size] += (s * env).astype(np.float32)
        t += seg.size
    peak = np.abs(out).max()
    return (out / peak * 0.8).astype(np.float32) if peak > 0 else out


def write_wav(path: Path, signal: np.ndarray, sr: int = SRC_SR) -> str:
    sf.write(str(path), signal, sr)
    return str(path)


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory) -> Path:
    """Three ~25 s tracks + query clips, generated once per session."""
    d = tmp_path_factory.mktemp("audio")
    for i, name in enumerate(["Alpha Band - First Song", "Beta - Second Song", "Gamma Call"]):
        write_wav(d / f"{name}.wav", synth_signal(seed=100 + i, seconds=25 + 5 * i))
    src = synth_signal(seed=101, seconds=30)  # == "Beta - Second Song"
    clip = src[int(12.0 * SRC_SR) : int(16.0 * SRC_SR)]
    write_wav(d / "clip_second_song_at_12s.wav", clip)
    rng = np.random.default_rng(7)
    noisy = clip + rng.normal(0, clip.std(), clip.size).astype(np.float32)
    write_wav(d / "clip_noisy.wav", (noisy / np.abs(noisy).max()).astype(np.float32))
    write_wav(d / "clip_unrelated.wav", synth_signal(seed=999, seconds=4))
    (d / "notes.txt").write_text("not audio")
    (d / "broken.wav").write_bytes(b"RIFF----WAVEfmt broken")
    return d


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.load(dotenv=False, env={}, profile="testing", data_dir=str(tmp_path / "data"), storage_type="memory", log_level="WARNING")


@pytest.fixture
def sqlite_settings(tmp_path) -> Settings:
    return Settings.load(
        dotenv=False,
        env={},
        profile="testing",
        data_dir=str(tmp_path / "data"),
        storage_type="sqlite",
        sqlite_path=str(tmp_path / "data" / "fp.db"),
        log_level="WARNING",
        persist_jobs=True,
    )


@pytest.fixture
def app(sqlite_settings):
    from fingerprint.api import create_app

    application = create_app(settings=sqlite_settings, configure_logs=False)
    yield application
    application.extensions["audiofp"].close()


@pytest.fixture
def client(app):
    return app.test_client()


def wait_for_job(client, job_id: str, timeout: float = 30.0, headers=None) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}", headers=headers or {}).get_json()
        if job["status"] in ("completed", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def upload(client, path: str, name: str | None = None, headers=None, **fields) -> dict:
    with open(path, "rb") as fh:
        data = {"audio": (fh, name or os.path.basename(path)), **fields}
        response = client.post("/api/v1/tracks", data=data, content_type="multipart/form-data", headers=headers or {})
    assert response.status_code == 202, response.get_json()
    return wait_for_job(client, response.get_json()["job_id"], headers=headers)
