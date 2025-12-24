"""PostgreSQL storage backend (optional)."""

import json
from .base import StorageBackend


class PostgresStore(StorageBackend):
    """PostgreSQL storage backend for large-scale deployments."""
    
    def __init__(self, host='localhost', port=5432, database='fingerprint',
                 user='fingerprint_user', password=''):
        """
        Initialize PostgreSQL store.
        
        Args:
            host: Database host
            port: Database port
            database: Database name
            user: Database user
            password: Database password
        """
        self.connection_params = {
            'host': host,
            'port': port,
            'database': database,
            'user': user,
            'password': password
        }
        
        # Note: psycopg2 import is optional
        try:
            import psycopg2
            self.psycopg2 = psycopg2
            self._init_database()
        except ImportError:
            raise ImportError(
                "psycopg2 is required for PostgreSQL support. "
                "Install with: pip install psycopg2-binary"
            )
    
    def _get_connection(self):
        """Get database connection."""
        return self.psycopg2.connect(**self.connection_params)
    
    def _init_database(self):
        """Create database tables if they don't exist."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Songs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS songs (
                song_id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                filepath TEXT,
                duration REAL,
                metadata JSONB
            )
        ''')
        
        # Fingerprints table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS fingerprints (
                hash_value BIGINT,
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
        cursor.close()
        conn.close()
    
    def store_fingerprint(self, song_id, song_metadata, hashes):
        """
        Store fingerprint hashes for a song.
        
        Args:
            song_id: Unique song identifier
            song_metadata: Dictionary with song metadata
            hashes: List of (hash_value, time_offset, song_id) tuples
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Store song metadata
        cursor.execute('''
            INSERT INTO songs 
            (song_id, title, artist, filepath, duration, metadata)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (song_id) DO UPDATE 
            SET title = EXCLUDED.title,
                artist = EXCLUDED.artist,
                filepath = EXCLUDED.filepath,
                duration = EXCLUDED.duration,
                metadata = EXCLUDED.metadata
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
            VALUES (%s, %s, %s)
        ''', fingerprint_data)
        
        conn.commit()
        cursor.close()
        conn.close()
    
    def query_hash(self, hash_value):
        """
        Query database for a specific hash.
        
        Args:
            hash_value: Hash integer to query
        
        Returns:
            list: List of (song_id, time_offset) tuples
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT song_id, time_offset 
            FROM fingerprints 
            WHERE hash_value = %s
        ''', (hash_value,))
        
        results = cursor.fetchall()
        cursor.close()
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
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT metadata FROM songs WHERE song_id = %s
        ''', (song_id,))
        
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            return json.loads(result[0]) if isinstance(result[0], str) else result[0]
        return None
    
    def get_all_songs(self):
        """
        Get list of all indexed songs.
        
        Returns:
            list: List of song metadata dictionaries
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT metadata FROM songs')
        results = cursor.fetchall()
        cursor.close()
        conn.close()
        
        return [
            json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for row in results
        ]
    
    def delete_song(self, song_id):
        """
        Delete a song and its fingerprints.
        
        Args:
            song_id: Song identifier
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM fingerprints WHERE song_id = %s', (song_id,))
        cursor.execute('DELETE FROM songs WHERE song_id = %s', (song_id,))
        
        conn.commit()
        cursor.close()
        conn.close()
    
    def get_stats(self):
        """
        Get database statistics.
        
        Returns:
            dict: Statistics
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM songs')
        total_songs = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM fingerprints')
        total_hashes = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(DISTINCT hash_value) FROM fingerprints')
        unique_hashes = cursor.fetchone()[0]
        
        cursor.close()
        conn.close()
        
        return {
            'total_songs': total_songs,
            'total_hashes': total_hashes,
            'unique_hashes': unique_hashes,
            'storage_type': 'postgres'
        }
    
    def clear(self):
        """Clear all data from storage."""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM fingerprints')
        cursor.execute('DELETE FROM songs')
        
        conn.commit()
        cursor.close()
        conn.close()

