"""Audio loading, preprocessing, and video audio extraction."""

import os
import subprocess
import tempfile

import numpy as np
import librosa


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".3gp", ".vob",
})

AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus",
})

SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


def is_video_file(filepath: str) -> bool:
    """Return True if the file has a recognised video extension."""
    return os.path.splitext(filepath)[1].lower() in VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# ffmpeg helper
# ---------------------------------------------------------------------------

def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def extract_audio_from_video(video_path: str, sr: int = 11025) -> tuple:
    """
    Extract the audio track from a video file using ffmpeg.

    The video stream is discarded; the audio is decoded to mono PCM WAV at
    the requested sample rate, then loaded into a numpy float32 array.

    Args:
        video_path: Path to the video file
        sr:         Target sample rate (Hz)

    Returns:
        (audio_array: np.ndarray, sample_rate: int)

    Raises:
        IOError: If ffmpeg is not installed, the file has no audio track, or
                 extraction fails for any other reason.
    """
    if not _ffmpeg_available():
        raise IOError(
            "ffmpeg is required to process video files but was not found on PATH.\n"
            "Install it with:\n"
            "  Windows : winget install ffmpeg\n"
            "  macOS   : brew install ffmpeg\n"
            "  Linux   : sudo apt install ffmpeg  (or equivalent)"
        )

    tmp_path = None
    try:
        # Write PCM WAV to a temp file so librosa can load it reliably
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vn",                    # drop all video streams
                "-acodec", "pcm_s16le",   # uncompressed PCM → always readable
                "-ar", str(sr),           # resample to target rate
                "-ac", "1",               # mono
                "-loglevel", "error",
                tmp_path,
            ],
            capture_output=True,
            timeout=600,  # generous limit for hour-long recordings
        )

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise IOError(f"ffmpeg could not extract audio from '{video_path}': {stderr}")

        audio, sample_rate = librosa.load(tmp_path, sr=sr, mono=True)
        return audio, sample_rate

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_audio(filepath: str, sr: int = 11025, mono: bool = True) -> tuple:
    """
    Load audio from any supported audio or video file.

    Video files have their audio track extracted automatically via ffmpeg
    before fingerprinting; no extra code is needed by the caller.

    Returns:
        (audio_array: np.ndarray, sample_rate: int)
    """
    if is_video_file(filepath):
        return extract_audio_from_video(filepath, sr=sr)

    try:
        audio, sample_rate = librosa.load(filepath, sr=sr, mono=mono)
        return audio, sample_rate
    except Exception as exc:
        raise IOError(f"Failed to load audio from '{filepath}': {exc}") from exc


def preprocess_audio(audio: np.ndarray, normalize: bool = True) -> np.ndarray:
    """
    Normalise audio to float32 mono in the range [-1, 1].

    Args:
        audio:     Audio samples
        normalize: Apply peak normalisation

    Returns:
        np.ndarray: Preprocessed float32 audio
    """
    audio = np.asarray(audio, dtype=np.float32)

    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    if normalize:
        peak = np.abs(audio).max()
        if peak > 0:
            audio = audio / peak

    return audio


def audio_to_spectrogram(
    audio: np.ndarray,
    sr: int = 11025,
    n_fft: int = 2048,
    hop_length: int = 512,
) -> np.ndarray:
    """
    Convert audio to a magnitude STFT spectrogram.

    Returns:
        np.ndarray: Magnitude spectrogram, shape (freq_bins, time_frames)
    """
    stft = librosa.stft(audio, n_fft=n_fft, hop_length=hop_length)
    return np.abs(stft)
