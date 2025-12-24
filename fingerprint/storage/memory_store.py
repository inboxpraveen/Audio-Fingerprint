"""In-memory storage backend using Python dictionaries."""

from collections import defaultdict
from .base import StorageBackend


class MemoryStore(StorageBackend):
    """Fast in-memory storage using Python dict."""
    
    def __init__(self):
        """Initialize memory store."""
        # Hash table: hash_int -> [(song_id, time_offset), ...]
        self.hash_table = defaultdict(list)
        
        # Song metadata: song_id -> {title, artist, filepath, ...}
        self.song_metadata = {}
        
        # Track total hashes
        self.total_hashes = 0
    
    def store_fingerprint(self, song_id, song_metadata, hashes):
        """
        Store fingerprint hashes for a song.
        
        Args:
            song_id: Unique song identifier
            song_metadata: Dictionary with song metadata
            hashes: List of (hash_value, time_offset, song_id) tuples
        """
        # Store metadata
        self.song_metadata[song_id] = song_metadata
        
        # Store hashes
        for hash_value, time_offset, _ in hashes:
            self.hash_table[hash_value].append((song_id, time_offset))
            self.total_hashes += 1
    
    def query_hash(self, hash_value):
        """
        Query database for a specific hash.
        
        Args:
            hash_value: Hash integer to query
        
        Returns:
            list: List of (song_id, time_offset) tuples
        """
        return self.hash_table.get(hash_value, [])
    
    def get_song_metadata(self, song_id):
        """
        Get metadata for a song.
        
        Args:
            song_id: Song identifier
        
        Returns:
            dict: Song metadata or None if not found
        """
        return self.song_metadata.get(song_id)
    
    def get_all_songs(self):
        """
        Get list of all indexed songs.
        
        Returns:
            list: List of song metadata dictionaries
        """
        return list(self.song_metadata.values())
    
    def delete_song(self, song_id):
        """
        Delete a song and its fingerprints.
        
        Args:
            song_id: Song identifier
        """
        # Remove from metadata
        if song_id in self.song_metadata:
            del self.song_metadata[song_id]
        
        # Remove hashes
        for hash_value, entries in list(self.hash_table.items()):
            self.hash_table[hash_value] = [
                (sid, offset) for sid, offset in entries if sid != song_id
            ]
            # Clean up empty entries
            if not self.hash_table[hash_value]:
                del self.hash_table[hash_value]
    
    def get_stats(self):
        """
        Get database statistics.
        
        Returns:
            dict: Statistics
        """
        return {
            'total_songs': len(self.song_metadata),
            'total_hashes': self.total_hashes,
            'unique_hashes': len(self.hash_table),
            'storage_type': 'memory'
        }
    
    def clear(self):
        """Clear all data from storage."""
        self.hash_table.clear()
        self.song_metadata.clear()
        self.total_hashes = 0

