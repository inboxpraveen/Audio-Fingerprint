"""Signal-processing core: STFT/peaks, hashing, decoding, matching."""

from __future__ import annotations

import io
import subprocess

import numpy as np
import pytest
import soxr
from scipy.ndimage import maximum_filter
from scipy.signal import get_window

from fingerprint.config import Settings
from fingerprint.core import decoder
from fingerprint.core.decoder import FFmpegInfo, iter_audio_chunks, load_audio, to_mono_float32
from fingerprint.core.fingerprinter import Fingerprinter, PeakExtractor
from fingerprint.core.hash_generator import MAX_TIME_DELTA, decode_hash, encode_hash, generate_hashes
from fingerprint.core.matcher import Matcher, MatchOptions, quality_label
from fingerprint.storage import MemoryStore, TrackRecord
from fingerprint.utils.exceptions import AudioDecodeError, FFmpegNotFoundError, MatchingError, UnsupportedFormatError

from .conftest import SRC_SR, synth_signal, write_wav

SR = 11025


def reference_peaks(y: np.ndarray, n_fft=2048, hop=512, size=20, min_amp=10.0):
    """Independent whole-signal implementation (explicit frame loop) of the constellation map."""
    win = get_window("hann", n_fft, fftbins=True)
    pad = n_fft // 2
    yp = np.concatenate([np.zeros(pad), y.astype(np.float64), np.zeros(pad)])
    n_frames = 1 + (yp.size - n_fft) // hop
    spec = np.empty((n_fft // 2 + 1, n_frames), dtype=np.float32)
    for i in range(n_frames):
        spec[:, i] = np.abs(np.fft.rfft(yp[i * hop : i * hop + n_fft] * win))
    mask = (maximum_filter(spec, size=size) == spec) & (spec > min_amp)
    f, t = np.nonzero(mask)
    order = np.lexsort((f, t))
    return t[order], f[order]


# ---------------------------------------------------------------------------
# peaks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chunk", [10**9, 50_000, 4096, 2048, 777])
def test_chunked_peaks_identical_to_whole_signal(chunk):
    y = synth_signal(seed=5, seconds=12.3, sr=SR)
    rt, rf = reference_peaks(y)
    ex = PeakExtractor(2048, 512, 20, 10.0)
    for start in range(0, y.size, chunk):
        ex.push(y[start : start + chunk])
    t, f, n = ex.finish()
    assert n == y.size
    assert rt.size > 100
    assert np.array_equal(t, rt) and np.array_equal(f, rf)


def test_extractor_edge_cases():
    ex = PeakExtractor(2048, 512, 20, 10.0)
    t, f, n = ex.finish()  # nothing pushed
    assert t.size == 0 and n == 0
    ex = PeakExtractor(2048, 512, 20, 10.0)
    ex.push(np.zeros(100, dtype=np.float32))  # shorter than one frame
    t, f, n = ex.finish()
    assert n == 100 and t.size == 0
    with pytest.raises(RuntimeError):
        ex.push(np.zeros(10, dtype=np.float32))
    with pytest.raises(ValueError):
        PeakExtractor(2047, 512, 20, 10)


def test_gain_changes_threshold_not_positions():
    y = synth_signal(seed=6, seconds=6, sr=SR) * 0.05  # quiet
    s = Settings.load(dotenv=False, env={})
    fp = Fingerprinter(s)
    quiet = fp.fingerprint_array(y, normalize=False)
    normalised = fp.fingerprint_array(y, normalize=True)
    assert normalised.num_peaks > quiet.num_peaks
    assert normalised.gain == pytest.approx(1 / np.abs(y).max())
    # single-pass normalisation must equal fingerprinting the explicitly scaled signal
    scaled = fp.fingerprint_array(y / np.abs(y).max(), normalize=False)
    assert np.array_equal(normalised.peak_times, scaled.peak_times) and np.array_equal(normalised.peak_freqs, scaled.peak_freqs)
    # every quiet peak is also a normalised peak (thresholding only removes peaks)
    quiet_set = set(zip(quiet.peak_times.tolist(), quiet.peak_freqs.tolist()))
    norm_set = set(zip(normalised.peak_times.tolist(), normalised.peak_freqs.tolist()))
    assert quiet_set <= norm_set


def test_fingerprint_file_matches_array_and_is_deterministic(tmp_path):
    y = synth_signal(seed=8, seconds=20, sr=SRC_SR)
    path = write_wav(tmp_path / "x.wav", y)
    s = Settings.load(dotenv=False, env={})
    fp = Fingerprinter(s)
    a = fp.fingerprint_file(path)
    b = fp.fingerprint_file(path)
    c = fp.fingerprint_array(soxr.resample(y, SRC_SR, SR))
    assert a.num_hashes == b.num_hashes == c.num_hashes > 0
    assert np.array_equal(a.hashes, c.hashes) and np.array_equal(a.hash_times, c.hash_times)
    assert a.duration_sec == pytest.approx(20.0, abs=0.01)


# ---------------------------------------------------------------------------
# hashes
# ---------------------------------------------------------------------------


def test_hash_encoding_roundtrip():
    f1 = np.array([0, 1024, 4095, 7])
    f2 = np.array([1024, 0, 4095, 9])
    dt = np.array([0, 200, 4095, 3])
    hashes = encode_hash(f1, f2, dt)
    assert hashes.dtype == np.int64
    for h, a, b, d in zip(hashes.tolist(), f1, f2, dt):
        assert decode_hash(h) == (a, b, d)
    assert len(set(hashes.tolist())) == 4


def naive_hashes(t, f, fan, mn, mx):
    """Reference: each anchor pairs with the next `fan` peaks that are >= mn frames later."""
    pk = sorted(zip(t.tolist(), f.tolist()))
    out = []
    for i in range(len(pk)):
        candidates = [j for j in range(i + 1, len(pk)) if pk[j][0] - pk[i][0] >= mn][:fan]
        for j in candidates:
            dt = pk[j][0] - pk[i][0]
            if dt <= mx:
                out.append((int(encode_hash(np.array([pk[i][1]]), np.array([pk[j][1]]), np.array([dt]))[0]), pk[i][0]))
    return sorted(out)


@pytest.mark.parametrize("fan,mn,mx", [(10, 0, 200), (10, 1, 200), (3, 2, 50), (50, 0, MAX_TIME_DELTA)])
def test_vectorised_hashes_equal_naive(fan, mn, mx):
    rng = np.random.default_rng(1)
    t = np.sort(rng.integers(0, 3000, 800))
    f = rng.integers(0, 1025, 800)
    h, ht = generate_hashes(t, f, fan, mn, mx)
    assert ht.dtype == np.int32 and np.all(np.diff(ht) >= 0)
    assert sorted(zip(h.tolist(), ht.tolist())) == naive_hashes(t, f, fan, mn, mx)


def test_hashes_edge_cases():
    h, t = generate_hashes(np.array([1]), np.array([2]), 10)
    assert h.size == 0 and t.size == 0
    h, t = generate_hashes(np.array([0, 5000]), np.array([1, 2]), 10, 0, 200)  # dt too large
    assert h.size == 0
    # simultaneous peaks are skipped with min_time_delta=1 but the fan-out is not wasted on them
    t3 = np.array([0, 0, 0, 5, 9])
    f3 = np.array([10, 20, 30, 40, 50])
    h, t = generate_hashes(t3, f3, fan_value=2, min_time_delta=1, max_time_delta=200)
    assert h.size == 3 * 2 + 1  # three anchors at t=0 pair with t=5 and t=9; anchor t=5 pairs with t=9
    assert all(decode_hash(x)[2] >= 1 for x in h)
    with pytest.raises(ValueError):
        generate_hashes(np.array([1, 2]), np.array([1]), 10)


# ---------------------------------------------------------------------------
# decoder
# ---------------------------------------------------------------------------


def test_soundfile_streaming_equals_whole_resample(tmp_path):
    y = np.stack([synth_signal(3, 7.7), synth_signal(4, 7.7)], axis=1)  # stereo
    path = write_wav(tmp_path / "stereo.wav", y)
    chunks = list(iter_audio_chunks(path, SR, chunk_seconds=1.0))
    assert len(chunks) >= 7
    streamed = np.concatenate(chunks)
    expected = soxr.resample(y.mean(axis=1).astype(np.float32), SRC_SR, SR)
    assert streamed.shape == expected.shape
    assert np.abs(streamed - expected).max() < 1e-4


def test_decoder_truncation_and_errors(tmp_path):
    path = write_wav(tmp_path / "long.wav", synth_signal(1, 10))
    y = load_audio(path, SR, max_seconds=2.5)
    assert y.size == int(2.5 * SR)
    (tmp_path / "empty.wav").write_bytes(b"")
    with pytest.raises(AudioDecodeError, match="empty"):
        load_audio(str(tmp_path / "empty.wav"), SR)
    with pytest.raises(AudioDecodeError, match="File not found"):
        load_audio(str(tmp_path / "missing.wav"), SR)
    (tmp_path / "bad.wav").write_bytes(b"RIFF junk")
    with pytest.raises(AudioDecodeError) as exc:
        load_audio(str(tmp_path / "bad.wav"), SR, display_name="Original Name.wav")
    assert "Original Name.wav" in str(exc.value) and str(tmp_path) not in str(exc.value)
    (tmp_path / "doc.xyz").write_bytes(b"?")
    with pytest.raises(UnsupportedFormatError):
        load_audio(str(tmp_path / "doc.xyz"), SR)


def test_ffmpeg_missing_gives_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(decoder, "ffmpeg_info", lambda binary=decoder.DEFAULT_FFMPEG: FFmpegInfo(False))
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 10)
    with pytest.raises(FFmpegNotFoundError) as exc:
        load_audio(str(tmp_path / "clip.mp4"), SR)
    assert exc.value.code == "ffmpeg_not_found" and "install" in str(exc.value).lower()


class _FakeProc:
    """Stand-in for subprocess.Popen running ffmpeg."""

    def __init__(self, pcm: bytes, stderr: bytes = b"", returncode: int = 0):
        self.stdout = io.BytesIO(pcm)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self._rc = returncode
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        # Like a real process: a code set by kill() stays; otherwise the process exits with its own code.
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_ffmpeg_reader_parses_pcm_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(decoder, "ffmpeg_info", lambda binary=decoder.DEFAULT_FFMPEG: FFmpegInfo(True, "ffmpeg", "6.0"))
    src = (np.sin(np.arange(SR * 2) / 20) * 20000).astype("<i2")
    (tmp_path / "v.mp4").write_bytes(b"x")
    procs = [_FakeProc(src.tobytes() + b"\x01", stderr=b"warning: something\n")]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: procs.pop(0))
    proc = procs[0]
    y = load_audio(str(tmp_path / "v.mp4"), SR, chunk_seconds=0.5)
    assert y.size == src.size and np.abs(y - src / 32768.0).max() < 1e-6
    assert not proc.killed, "a process that finished normally must not be killed"

    procs.append(_FakeProc(b"", stderr=b"Invalid data found when processing input\n", returncode=1))
    with pytest.raises(AudioDecodeError, match="Invalid data"):
        load_audio(str(tmp_path / "v.mp4"), SR)

    procs.append(_FakeProc(b"", returncode=0))
    with pytest.raises(AudioDecodeError, match="no decodable audio"):
        load_audio(str(tmp_path / "v.mp4"), SR)

    # truncation stops reading early and does not raise even though the process is killed
    procs.append(_FakeProc(src.tobytes(), returncode=0))
    y = load_audio(str(tmp_path / "v.mp4"), SR, max_seconds=0.5, chunk_seconds=0.25)
    assert y.size == int(0.5 * SR)


def test_soundfile_midstream_failure_does_not_fall_back(tmp_path, monkeypatch):
    """A file that breaks half-way must not be replayed from the start through ffmpeg."""
    path = write_wav(tmp_path / "ok.wav", synth_signal(2, 5))
    calls = {"ffmpeg": 0}

    def fake_ffmpeg(*args, **kwargs):
        calls["ffmpeg"] += 1
        yield np.zeros(10, dtype=np.float32)

    monkeypatch.setattr(decoder, "ffmpeg_available", lambda binary=decoder.DEFAULT_FFMPEG: True)
    monkeypatch.setattr(decoder, "_iter_ffmpeg", fake_ffmpeg)

    real = decoder._iter_soundfile

    def flaky(*args, **kwargs):
        for i, chunk in enumerate(real(*args, **kwargs)):
            if i == 1:
                raise AudioDecodeError("simulated mid-stream failure")
            yield chunk

    monkeypatch.setattr(decoder, "_iter_soundfile", flaky)
    with pytest.raises(AudioDecodeError, match="mid-stream"):
        list(iter_audio_chunks(path, SR, chunk_seconds=1.0))
    assert calls["ffmpeg"] == 0

    # failing before the first chunk does fall back
    def dead(*args, **kwargs):
        raise AudioDecodeError("cannot open")
        yield  # pragma: no cover

    monkeypatch.setattr(decoder, "_iter_soundfile", dead)
    assert next(iter(iter_audio_chunks(path, SR))).size == 10 and calls["ffmpeg"] == 1


def test_to_mono_float32_layouts():
    n = np.arange(10, dtype=np.float32)
    assert to_mono_float32(np.stack([n, n + 2], axis=1)).tolist() == (n + 1).tolist()  # (n, ch)
    assert to_mono_float32(np.stack([n, n + 2], axis=0)).tolist() == (n + 1).tolist()  # (ch, n)
    with pytest.raises(AudioDecodeError):
        to_mono_float32(np.zeros((2, 2, 2)))


# ---------------------------------------------------------------------------
# matcher
# ---------------------------------------------------------------------------


@pytest.fixture
def library():
    s = Settings.load(dotenv=False, env={})
    fp = Fingerprinter(s)
    store = MemoryStore()
    signals = {}
    for i, title in enumerate(["one", "two", "three"]):
        y = soxr.resample(synth_signal(seed=200 + i, seconds=20 + 4 * i), SRC_SR, SR)
        signals[title] = y
        f = fp.fingerprint_array(y)
        store.add_track(TrackRecord(title=title, duration=y.size / SR, num_hashes=f.num_hashes), f.hashes, f.hash_times)
    return s, fp, store, signals


def test_identify_clip_offset_and_rejection(library):
    s, fp, store, signals = library
    matcher = Matcher(s)
    clip = signals["two"][int(7.0 * SR) : int(11.0 * SR)]
    matches = matcher.match(fp.fingerprint_array(clip), store)
    assert len(matches) == 1 and matches[0].track.title == "two"
    best = matches[0].best
    assert fp.frames_to_seconds(best.offset_frames) == pytest.approx(7.0, abs=0.1)
    assert matches[0].confidence > 0.2 and matches[0].peak_ratio > 15
    assert quality_label(matches[0].confidence, matches[0].peak_ratio) in ("strong", "likely")

    unrelated = soxr.resample(synth_signal(seed=4242, seconds=4), SRC_SR, SR)
    assert matcher.match(fp.fingerprint_array(unrelated), store) == []

    rng = np.random.default_rng(3)
    noisy = clip + rng.normal(0, clip.std(), clip.size).astype(np.float32)
    matches = matcher.match(fp.fingerprint_array(noisy), store)
    assert matches and matches[0].track.title == "two"


def test_occurrences_mode_finds_pattern_twice_and_negative_offsets(library):
    s, fp, store, signals = library
    matcher = Matcher(s)
    pattern = signals["three"][int(3 * SR) : int(6 * SR)]
    f = fp.fingerprint_array(pattern)
    store.add_track(TrackRecord(title="jingle", duration=3.0, num_hashes=f.num_hashes), f.hashes, f.hash_times)
    filler = soxr.resample(synth_signal(seed=77, seconds=40), SRC_SR, SR)
    recording = np.concatenate([filler[: 10 * SR], pattern, filler[10 * SR : 25 * SR], pattern, filler[25 * SR :]])
    opts = MatchOptions.from_settings(s, mode="occurrences", top_k=5)
    matches = matcher.match(fp.fingerprint_array(recording), store, opts)
    by_title = {m.track.title: m for m in matches}
    assert "jingle" in by_title
    occ = sorted(by_title["jingle"].occurrences, key=lambda o: o.offset_frames)
    positions = sorted(fp.frames_to_seconds(-o.offset_frames) for o in occ)
    assert len(positions) == 2
    assert positions[0] == pytest.approx(10.0, abs=0.15) and positions[1] == pytest.approx(28.0, abs=0.15)
    assert all(o.offset_frames < 0 for o in occ)  # track content found inside the query
    # spans describe where in the query the pattern lies
    assert fp.frames_to_seconds(occ[1].query_start_frames) == pytest.approx(10.0, abs=0.5)
    # the span end is anchor-based, so it runs a little short of the true end (anchors near the end pair with peaks outside the pattern)
    assert 11.5 <= fp.frames_to_seconds(occ[1].query_end_frames) <= 13.2
    # identify mode reports a single occurrence
    ident = matcher.match(fp.fingerprint_array(recording), store, MatchOptions.from_settings(s, top_k=5))
    assert len({m.track.title: m for m in ident}["jingle"].occurrences) == 1


def test_match_options_validation_and_thresholds(library):
    s, fp, store, signals = library
    with pytest.raises(MatchingError):
        MatchOptions.from_settings(s, mode="fuzzy")
    opts = MatchOptions.from_settings(s, top_k=10_000, min_confidence=5, min_peak_ratio=-1)
    assert opts.top_k == s.max_top_k and opts.min_confidence == 1.0 and opts.min_peak_ratio == 0.0
    clip = signals["one"][int(2 * SR) : int(6 * SR)]
    query = fp.fingerprint_array(clip)
    assert Matcher(s).match(query, store, MatchOptions.from_settings(s, min_confidence=0.999)) == []
    assert Matcher(s).match(query, store, MatchOptions.from_settings(s, min_aligned_hashes=10**6)) == []
    assert Matcher(s).match(query, store, MatchOptions.from_settings(s, top_k=1))[0].track.title == "one"
    empty = fp.fingerprint_array(np.zeros(SR, dtype=np.float32))
    assert Matcher(s).match(empty, store) == []


def test_common_hash_cap_and_vote_budget(library):
    """Stop-word hashes are skipped and the vote budget bounds the join, without losing a clear match."""
    s, fp, store, signals = library
    clip = signals["one"][int(3 * SR) : int(7 * SR)]
    query = fp.fingerprint_array(clip)
    strict = MatchOptions.from_settings(s, max_rows_per_hash=1)  # every hash shared by >1 rows is a "stop word"
    stats: dict = {}
    h, _r, _t = store.query_hashes(np.unique(query.hashes), max_rows_per_hash=1, stats=stats)
    assert stats["skipped_hashes"] >= 0 and (np.unique(h, return_counts=True)[1] <= 1).all()
    matcher = Matcher(s)
    assert matcher.match(query, store, strict)  # still identifies with only unique hashes
    tiny_budget = MatchOptions.from_settings(s, max_search_votes=10_000)
    matches = matcher.match(query, store, tiny_budget)
    diag = matcher.last_diagnostics
    assert diag.votes <= 10_000 and matches and matches[0].track.title == "one"
    assert diag.candidate_tracks >= diag.scored_tracks >= 1


def test_prefilter_skips_hopeless_tracks(library):
    s, fp, store, signals = library
    clip = signals["two"][int(2 * SR) : int(6 * SR)]
    matcher = Matcher(s)
    matcher.match(fp.fingerprint_array(clip), store)
    diag = matcher.last_diagnostics
    assert diag.scored_tracks < diag.candidate_tracks or diag.candidate_tracks == 1


def test_occurrences_on_highly_repetitive_audio_is_bounded():
    """A looped pattern creates thousands of strong offset bins; scoring must stay fast and capped."""
    import time

    s = Settings.load(dotenv=False, env={})
    fp = Fingerprinter(s)
    store = MemoryStore()
    loop = synth_signal(seed=55, seconds=2.0, sr=SR)
    track = np.tile(loop, 150)  # 5 minutes of the same 2-second loop
    f = fp.fingerprint_array(track)
    store.add_track(TrackRecord(title="loop", duration=track.size / SR, num_hashes=f.num_hashes), f.hashes, f.hash_times)
    query = fp.fingerprint_array(np.tile(loop, 15))  # 30 s of the loop
    started = time.time()
    matches = Matcher(s).match(query, store, MatchOptions.from_settings(s, mode="occurrences", top_k=3))
    elapsed = time.time() - started
    assert elapsed < 5.0, f"occurrence search took {elapsed:.1f}s"
    assert matches and matches[0].track.title == "loop"
    assert 1 <= len(matches[0].occurrences) <= s.max_occurrences_per_track
