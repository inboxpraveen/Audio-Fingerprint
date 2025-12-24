"""Unit tests for storage backends."""

import unittest
import tempfile
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.storage import MemoryStore, SQLiteStore


class TestMemoryStore(unittest.TestCase):
    """Test cases for MemoryStore."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.storage = MemoryStore()
    
    def test_store_and_retrieve(self):
        """Test storing and retrieving fingerprints."""
        song_id = 'test_song_1'
        metadata = {'title': 'Test Song', 'artist': 'Test Artist'}
        hashes = [
            (12345, 0, song_id),
            (23456, 5, song_id),
            (34567, 10, song_id),
        ]
        
        # Store
        self.storage.store_fingerprint(song_id, metadata, hashes)
        
        # Retrieve metadata
        retrieved_metadata = self.storage.get_song_metadata(song_id)
        self.assertEqual(retrieved_metadata['title'], metadata['title'])
        
        # Query hash
        results = self.storage.query_hash(12345)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0][0], song_id)
    
    def test_get_all_songs(self):
        """Test getting all songs."""
        # Add multiple songs
        for i in range(3):
            self.storage.store_fingerprint(
                f'song_{i}',
                {'title': f'Song {i}'},
                [(i * 1000, 0, f'song_{i}')]
            )
        
        songs = self.storage.get_all_songs()
        self.assertEqual(len(songs), 3)
    
    def test_delete_song(self):
        """Test deleting a song."""
        song_id = 'test_song'
        self.storage.store_fingerprint(
            song_id,
            {'title': 'Test'},
            [(12345, 0, song_id)]
        )
        
        # Delete
        self.storage.delete_song(song_id)
        
        # Verify deletion
        metadata = self.storage.get_song_metadata(song_id)
        self.assertIsNone(metadata)
    
    def test_get_stats(self):
        """Test getting statistics."""
        self.storage.store_fingerprint(
            'song1',
            {'title': 'Song 1'},
            [(12345, 0, 'song1'), (23456, 5, 'song1')]
        )
        
        stats = self.storage.get_stats()
        
        self.assertIn('total_songs', stats)
        self.assertIn('total_hashes', stats)
        self.assertEqual(stats['total_songs'], 1)
        self.assertEqual(stats['total_hashes'], 2)
    
    def test_clear(self):
        """Test clearing storage."""
        self.storage.store_fingerprint(
            'song1',
            {'title': 'Song 1'},
            [(12345, 0, 'song1')]
        )
        
        self.storage.clear()
        
        stats = self.storage.get_stats()
        self.assertEqual(stats['total_songs'], 0)
        self.assertEqual(stats['total_hashes'], 0)


class TestSQLiteStore(unittest.TestCase):
    """Test cases for SQLiteStore."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary database
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.storage = SQLiteStore(self.temp_db.name)
    
    def tearDown(self):
        """Clean up test fixtures."""
        # Delete temporary database
        if os.path.exists(self.temp_db.name):
            os.unlink(self.temp_db.name)
    
    def test_store_and_retrieve(self):
        """Test storing and retrieving fingerprints."""
        song_id = 'test_song_1'
        metadata = {'title': 'Test Song', 'artist': 'Test Artist'}
        hashes = [
            (12345, 0, song_id),
            (23456, 5, song_id),
        ]
        
        # Store
        self.storage.store_fingerprint(song_id, metadata, hashes)
        
        # Retrieve metadata
        retrieved_metadata = self.storage.get_song_metadata(song_id)
        self.assertEqual(retrieved_metadata['title'], metadata['title'])
        
        # Query hash
        results = self.storage.query_hash(12345)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0][0], song_id)
    
    def test_persistence(self):
        """Test that data persists across instances."""
        song_id = 'persistent_song'
        
        # Store in first instance
        self.storage.store_fingerprint(
            song_id,
            {'title': 'Persistent Song'},
            [(12345, 0, song_id)]
        )
        
        # Create new instance with same database
        storage2 = SQLiteStore(self.temp_db.name)
        
        # Retrieve from second instance
        metadata = storage2.get_song_metadata(song_id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata['title'], 'Persistent Song')


if __name__ == '__main__':
    unittest.main()

