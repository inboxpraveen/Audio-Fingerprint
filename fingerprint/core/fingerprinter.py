"""Core fingerprinting algorithm - spectral peak extraction."""

import numpy as np
from scipy.ndimage import maximum_filter

from .audio_processor import audio_to_spectrogram


class Fingerprinter:
    """Main fingerprinting class for extracting audio fingerprints."""
    
    def __init__(self, sr=11025, n_fft=2048, hop_length=512, 
                 peak_neighborhood_size=20, min_amplitude=10):
        """
        Initialize fingerprinter.
        
        Args:
            sr: Sample rate
            n_fft: FFT window size
            hop_length: Hop length for STFT
            peak_neighborhood_size: Local maxima window size
            min_amplitude: Minimum peak amplitude threshold
        """
        self.sr = sr
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.peak_neighborhood_size = peak_neighborhood_size
        self.min_amplitude = min_amplitude
    
    def generate_fingerprint(self, audio):
        """
        Generate fingerprint from audio signal.
        
        Args:
            audio: Audio samples (numpy array)
        
        Returns:
            list: List of (time_idx, freq_idx, amplitude) tuples
        """
        # Compute spectrogram
        spectrogram = audio_to_spectrogram(
            audio, 
            sr=self.sr, 
            n_fft=self.n_fft, 
            hop_length=self.hop_length
        )
        
        # Find spectral peaks
        peaks = self._find_spectral_peaks(spectrogram)
        
        return peaks
    
    def _find_spectral_peaks(self, spectrogram):
        """
        Find spectral peaks (local maxima) in spectrogram.
        
        Args:
            spectrogram: Magnitude spectrogram (freq x time)
        
        Returns:
            list: List of (time_idx, freq_idx, amplitude) tuples
        """
        # Apply logarithmic scaling
        log_spectrogram = np.log1p(spectrogram)
        
        # Find local maxima using maximum filter
        neighborhood_size = self.peak_neighborhood_size
        local_max = maximum_filter(log_spectrogram, size=neighborhood_size) == log_spectrogram
        
        # Apply amplitude threshold
        threshold_mask = log_spectrogram > np.log1p(self.min_amplitude)
        
        # Combine masks
        peaks_mask = local_max & threshold_mask
        
        # Get peak coordinates
        freq_idx, time_idx = np.where(peaks_mask)
        amplitudes = spectrogram[freq_idx, time_idx]
        
        # Create list of peaks
        peaks = list(zip(time_idx, freq_idx, amplitudes))
        
        # Sort by time
        peaks.sort(key=lambda x: x[0])
        
        return peaks
    
    def _create_constellation_map(self, peaks):
        """
        Create constellation map from peaks (for visualization).
        
        Args:
            peaks: List of (time_idx, freq_idx, amplitude) tuples
        
        Returns:
            dict: Constellation map
        """
        constellation = {}
        for time_idx, freq_idx, amplitude in peaks:
            if time_idx not in constellation:
                constellation[time_idx] = []
            constellation[time_idx].append((freq_idx, amplitude))
        
        return constellation

