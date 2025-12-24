"""Abstract storage interface."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract base class for storage backends."""
    
    @abstractmethod
    def store_fingerprint(self, song_id, song_metadata, hashes):
        """
        Store fingerprint hashes for a song.
        
        Args:
            song_id: Unique song identifier
            song_metadata: Dictionary with song metadata (title, artist, etc.)
            hashes: List of (hash_value, time_offset, song_id) tuples
        """
        pass
    
    @abstractmethod
    def query_hash(self, hash_value):
        """
        Query database for a specific hash.
        
        Args:
            hash_value: Hash integer to query
        
        Returns:
            list: List of (song_id, time_offset) tuples
        """
        pass
    
    @abstractmethod
    def get_song_metadata(self, song_id):
        """
        Get metadata for a song.
        
        Args:
            song_id: Song identifier
        
        Returns:
            dict: Song metadata or None if not found
        """
        pass
    
    @abstractmethod
    def get_all_songs(self):
        """
        Get list of all indexed songs.
        
        Returns:
            list: List of song metadata dictionaries
        """
        pass
    
    @abstractmethod
    def delete_song(self, song_id):
        """
        Delete a song and its fingerprints.
        
        Args:
            song_id: Song identifier
        """
        pass
    
    @abstractmethod
    def get_stats(self):
        """
        Get database statistics.
        
        Returns:
            dict: Statistics (total_songs, total_hashes, etc.)
        """
        pass
    
    @abstractmethod
    def clear(self):
        """Clear all data from storage."""
        pass

