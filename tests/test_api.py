"""Unit tests for API endpoints."""

import unittest
import sys
import os
import io

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.api import create_app


class TestAPI(unittest.TestCase):
    """Test cases for Flask API."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.app = create_app('development')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
    
    def test_health_check(self):
        """Test health check endpoint."""
        response = self.client.get('/api/v1/health')
        
        self.assertEqual(response.status_code, 200)
        
        data = response.get_json()
        self.assertIn('status', data)
        self.assertEqual(data['status'], 'healthy')
    
    def test_get_stats(self):
        """Test stats endpoint."""
        response = self.client.get('/api/v1/stats')
        
        self.assertEqual(response.status_code, 200)
        
        data = response.get_json()
        self.assertIn('total_songs', data)
        self.assertIn('total_hashes', data)
    
    def test_get_songs(self):
        """Test get songs endpoint."""
        response = self.client.get('/api/v1/songs')
        
        self.assertEqual(response.status_code, 200)
        
        data = response.get_json()
        self.assertIn('songs', data)
        self.assertIn('count', data)
        self.assertIsInstance(data['songs'], list)
    
    def test_search_no_file(self):
        """Test search endpoint without file."""
        response = self.client.post('/api/v1/search')
        
        self.assertEqual(response.status_code, 400)
        
        data = response.get_json()
        self.assertIn('error', data)
    
    def test_search_invalid_file_type(self):
        """Test search with invalid file type."""
        data = {
            'audio': (io.BytesIO(b"test data"), 'test.txt')
        }
        
        response = self.client.post(
            '/api/v1/search',
            data=data,
            content_type='multipart/form-data'
        )
        
        self.assertEqual(response.status_code, 400)
    
    def test_get_song_not_found(self):
        """Test get song with non-existent ID."""
        response = self.client.get('/api/v1/songs/nonexistent')
        
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()

