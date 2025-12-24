"""SQLite storage backend."""

import sqlite3
import json
from .base import StorageBackend


class SQLiteStore(StorageBackend):
    """SQLite storage backend for persistent fingerprint database."""
    
    def __init__(self, db_path='fingerprint.db'):
        """
        Initialize SQLite store.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._init_database()
    
    def _init_database(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Songs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS songs (
                song_id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                filepath TEXT,
                duration REAL,
                metadata TEXT
            )
        ''')
        
        # Fingerprints table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fingerprints (
                hash_value INTEGER,
                song_id TEXT,
                time_offset INTEGER,
                FOREIGN KEY (song_id) REFERENCES songs(song_id)
            )
        ''')
        
        # Create index on hash_value for fast lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_hash_value 
            ON fingerprints(hash_value)
        ''')
        
        conn.commit()
        conn.close()
    
    def store_fingerprint(self, song_id, song_metadata, hashes):
        """
        Store fingerprint hashes for a song.
        
        Args:
            song_id: Unique song identifier
            song_metadata: Dictionary with song metadata
            hashes: List of (hash_value, time_offset, song_id) tuples
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Store song metadata
        cursor.execute('''
            INSERT OR REPLACE INTO songs 
            (song_id, title, artist, filepath, duration, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            song_id,
            song_metadata.get('title', ''),
            song_metadata.get('artist', ''),
            song_metadata.get('filepath', ''),
            song_metadata.get('duration', 0.0),
            json.dumps(song_metadata)
        ))
        
        # Store fingerprints
        fingerprint_data = [
            (hash_value, song_id, time_offset)
            for hash_value, time_offset, _ in hashes
        ]
        cursor.executemany('''
            INSERT INTO fingerprints (hash_value, song_id, time_offset)
            VALUES (?, ?, ?)
        ''', fingerprint_data)
        
        conn.commit()
        conn.close()
    
    def query_hash(self, hash_value):
        """
        Query database for a specific hash.
        
        Args:
            hash_value: Hash integer to query
        
        Returns:
            list: List of (song_id, time_offset) tuples
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT song_id, time_offset 
            FROM fingerprints 
            WHERE hash_value = ?
        ''', (hash_value,))
        
        results = cursor.fetchall()
        conn.close()
        
        return results
    
    def get_song_metadata(self, song_id):
        """
        Get metadata for a song.
        
        Args:
            song_id: Song identifier
        
        Returns:
            dict: Song metadata or None if not found
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT metadata FROM songs WHERE song_id = ?
        ''', (song_id,))
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return json.loads(result[0])
        return None
    
    def get_all_songs(self):
        """
        Get list of all indexed songs.
        
        Returns:
            list: List of song metadata dictionaries
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT metadata FROM songs')
        results = cursor.fetchall()
        conn.close()
        
        return [json.loads(row[0]) for row in results]
    
    def delete_song(self, song_id):
        """
        Delete a song and its fingerprints.
        
        Args:
            song_id: Song identifier
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM fingerprints WHERE song_id = ?', (song_id,))
        cursor.execute('DELETE FROM songs WHERE song_id = ?', (song_id,))
        
        conn.commit()
        conn.close()
    
    def get_stats(self):
        """
        Get database statistics.
        
        Returns:
            dict: Statistics
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM songs')
        total_songs = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM fingerprints')
        total_hashes = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(DISTINCT hash_value) FROM fingerprints')
        unique_hashes = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'total_songs': total_songs,
            'total_hashes': total_hashes,
            'unique_hashes': unique_hashes,
            'storage_type': 'sqlite',
            'db_path': self.db_path
        }
    
    def clear(self):
        """Clear all data from storage."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM fingerprints')
        cursor.execute('DELETE FROM songs')
        
        conn.commit()
        conn.close()

