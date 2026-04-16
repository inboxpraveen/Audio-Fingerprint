"""Abstract storage interface."""

from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def store_fingerprint(self, song_id, song_metadata, hashes):
        """Store fingerprint hashes for a song.

        Args:
            song_id: Unique song identifier
            song_metadata: Dictionary with song metadata (title, artist, etc.)
            hashes: List of (hash_value, time_offset, song_id) tuples
        """
        pass

    @abstractmethod
    def query_hash(self, hash_value):
        """Query database for a single hash.

        Returns:
            list of (song_id, time_offset) tuples
        """
        pass

    def query_hashes_batch(self, hash_values: list) -> list:
        """Query multiple hashes in one call.

        Default implementation calls query_hash individually.
        Override in subclasses for better performance.

        Returns:
            list of (hash_value, song_id, time_offset) tuples
        """
        results = []
        for hv in hash_values:
            for song_id, time_offset in self.query_hash(hv):
                results.append((hv, song_id, time_offset))
        return results

    @abstractmethod
    def get_song_metadata(self, song_id):
        """Return metadata dict for song_id, or None if not found."""
        pass

    @abstractmethod
    def get_all_songs(self):
        """Return list of all song metadata dictionaries."""
        pass

    @abstractmethod
    def delete_song(self, song_id):
        """Delete a song and all its fingerprints."""
        pass

    @abstractmethod
    def get_stats(self):
        """Return statistics dict (total_songs, total_hashes, etc.)."""
        pass

    @abstractmethod
    def clear(self):
        """Remove all data from storage."""
        pass

