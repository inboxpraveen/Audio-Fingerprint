"""Audio loading and preprocessing utilities."""

import numpy as np
import librosa
import soundfile as sf


def load_audio(filepath, sr=11025, mono=True):
    """
    Load audio file and return audio samples.
    
    Args:
        filepath: Path to audio file
        sr: Target sample rate (default: 11025 Hz)
        mono: Convert to mono if True (default: True)
    
    Returns:
        tuple: (audio_samples, sample_rate)
    """
    try:
        audio, sample_rate = librosa.load(filepath, sr=sr, mono=mono)
        return audio, sample_rate
    except Exception as e:
        raise IOError(f"Failed to load audio file {filepath}: {str(e)}")


def preprocess_audio(audio, normalize=True):
    """
    Preprocess audio signal.
    
    Args:
        audio: Audio samples (numpy array)
        normalize: Apply normalization to [-1, 1] range
    
    Returns:
        numpy array: Preprocessed audio
    """
    # Ensure audio is numpy array
    audio = np.asarray(audio, dtype=np.float32)
    
    # Convert stereo to mono if needed
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)
    
    # Normalize to [-1, 1] range
    if normalize:
        max_val = np.abs(audio).max()
        if max_val > 0:
            audio = audio / max_val
    
    return audio


def audio_to_spectrogram(audio, sr=11025, n_fft=2048, hop_length=512):
    """
    Convert audio signal to STFT spectrogram.
    
    Args:
        audio: Audio samples
        sr: Sample rate
        n_fft: FFT window size
        hop_length: Number of samples between successive frames
    
    Returns:
        numpy array: Magnitude spectrogram (frequency x time)
    """
    # Compute Short-Time Fourier Transform
    stft = librosa.stft(audio, n_fft=n_fft, hop_length=hop_length)
    
    # Get magnitude spectrogram
    spectrogram = np.abs(stft)
    
    return spectrogram

