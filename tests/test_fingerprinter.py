"""Unit tests for fingerprinting module."""

import unittest
import numpy as np
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.core import Fingerprinter, generate_hashes


class TestFingerprinter(unittest.TestCase):
    """Test cases for Fingerprinter class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.fingerprinter = Fingerprinter()
    
    def test_generate_fingerprint(self):
        """Test fingerprint generation from audio."""
        # Create synthetic audio signal
        duration = 2.0  # seconds
        sr = 11025
        t = np.linspace(0, duration, int(sr * duration))
        audio = np.sin(2 * np.pi * 440 * t)  # 440 Hz sine wave
        
        # Generate fingerprint
        peaks = self.fingerprinter.generate_fingerprint(audio)
        
        # Assertions
        self.assertIsInstance(peaks, list)
        self.assertGreater(len(peaks), 0, "Should find some peaks")
        
        # Check peak structure
        if peaks:
            time_idx, freq_idx, amplitude = peaks[0]
            self.assertIsInstance(time_idx, (int, np.integer))
            self.assertIsInstance(freq_idx, (int, np.integer))
            self.assertIsInstance(amplitude, (float, np.floating))
    
    def test_find_spectral_peaks(self):
        """Test spectral peak detection."""
        # Create simple spectrogram with known peaks
        spectrogram = np.random.rand(100, 100) * 5
        
        # Add some strong peaks
        spectrogram[50, 50] = 100
        spectrogram[25, 75] = 80
        
        peaks = self.fingerprinter._find_spectral_peaks(spectrogram)
        
        self.assertIsInstance(peaks, list)
        self.assertGreater(len(peaks), 0)
    
    def test_hash_generation(self):
        """Test hash generation from peaks."""
        # Create synthetic peaks
        peaks = [
            (0, 100, 50.0),
            (5, 150, 45.0),
            (10, 200, 40.0),
            (15, 250, 35.0),
        ]
        
        hashes = generate_hashes(peaks, song_id='test_song', fan_value=3)
        
        # Assertions
        self.assertIsInstance(hashes, list)
        self.assertGreater(len(hashes), 0, "Should generate hashes")
        
        # Check hash structure
        if hashes:
            hash_value, time_offset, song_id = hashes[0]
            self.assertIsInstance(hash_value, int)
            self.assertIsInstance(time_offset, (int, np.integer))
            self.assertEqual(song_id, 'test_song')
    
    def test_empty_audio(self):
        """Test handling of empty audio."""
        audio = np.array([])
        
        # Should not crash
        try:
            peaks = self.fingerprinter.generate_fingerprint(audio)
            # May return empty list or raise exception
            self.assertIsInstance(peaks, list)
        except Exception:
            # Exception is acceptable for empty audio
            pass
    
    def test_silent_audio(self):
        """Test handling of silent audio."""
        audio = np.zeros(11025)  # 1 second of silence
        
        peaks = self.fingerprinter.generate_fingerprint(audio)
        
        # Should return few or no peaks for silent audio
        self.assertIsInstance(peaks, list)


if __name__ == '__main__':
    unittest.main()

