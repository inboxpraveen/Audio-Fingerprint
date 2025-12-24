"""Unit tests for matching module."""

import unittest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.core import match_fingerprint
from fingerprint.storage import MemoryStore


class TestMatcher(unittest.TestCase):
    """Test cases for fingerprint matching."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.storage = MemoryStore()
        
        # Add test data
        song1_hashes = [
            (12345, 0, 'song1'),
            (23456, 5, 'song1'),
            (34567, 10, 'song1'),
        ]
        
        song2_hashes = [
            (45678, 0, 'song2'),
            (56789, 5, 'song2'),
        ]
        
        self.storage.store_fingerprint(
            'song1',
            {'title': 'Song 1', 'artist': 'Artist 1'},
            song1_hashes
        )
        
        self.storage.store_fingerprint(
            'song2',
            {'title': 'Song 2', 'artist': 'Artist 2'},
            song2_hashes
        )
    
    def test_exact_match(self):
        """Test exact match with same hashes."""
        query_hashes = [
            (12345, 0, None),
            (23456, 5, None),
            (34567, 10, None),
        ]
        
        matches = match_fingerprint(query_hashes, self.storage, top_k=5)
        
        self.assertGreater(len(matches), 0, "Should find matches")
        
        # Best match should be song1
        best_match = matches[0]
        song_id, confidence, metadata = best_match
        
        self.assertEqual(song_id, 'song1')
        self.assertGreater(confidence, 0.5, "Confidence should be high")
    
    def test_no_match(self):
        """Test query with no matching hashes."""
        query_hashes = [
            (99999, 0, None),
            (88888, 5, None),
        ]
        
        matches = match_fingerprint(query_hashes, self.storage, top_k=5)
        
        # Should return empty list or very low confidence matches
        if matches:
            best_confidence = matches[0][1]
            self.assertLess(best_confidence, 0.1, "Confidence should be very low")
    
    def test_partial_match(self):
        """Test partial match with some common hashes."""
        query_hashes = [
            (12345, 0, None),  # Matches song1
            (99999, 5, None),  # No match
        ]
        
        matches = match_fingerprint(query_hashes, self.storage, top_k=5)
        
        self.assertGreater(len(matches), 0, "Should find some matches")
    
    def test_empty_query(self):
        """Test with empty query."""
        query_hashes = []
        
        matches = match_fingerprint(query_hashes, self.storage, top_k=5)
        
        # Should return empty list
        self.assertEqual(len(matches), 0)
    
    def test_empty_database(self):
        """Test with empty database."""
        empty_storage = MemoryStore()
        
        query_hashes = [
            (12345, 0, None),
        ]
        
        matches = match_fingerprint(query_hashes, empty_storage, top_k=5)
        
        # Should return empty list
        self.assertEqual(len(matches), 0)


if __name__ == '__main__':
    unittest.main()

