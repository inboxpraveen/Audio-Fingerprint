"""Dataset loading utilities."""

import os


class DatasetLoader:
    """Utility for loading audio files from directories."""
    
    AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.ogg', '.aac'}
    
    def find_audio_files(self, directory_path, recursive=True):
        """
        Find all audio files in a directory.
        
        Args:
            directory_path: Root directory path
            recursive: Search recursively in subdirectories
        
        Returns:
            list: List of audio file paths
        """
        audio_files = []
        
        if recursive:
            for root, dirs, files in os.walk(directory_path):
                for filename in files:
                    if self._is_audio_file(filename):
                        filepath = os.path.join(root, filename)
                        audio_files.append(filepath)
        else:
            for filename in os.listdir(directory_path):
                filepath = os.path.join(directory_path, filename)
                if os.path.isfile(filepath) and self._is_audio_file(filename):
                    audio_files.append(filepath)
        
        return sorted(audio_files)
    
    def _is_audio_file(self, filename):
        """
        Check if file is an audio file based on extension.
        
        Args:
            filename: File name
        
        Returns:
            bool: True if audio file
        """
        extension = os.path.splitext(filename)[1].lower()
        return extension in self.AUDIO_EXTENSIONS
    
    def load_metadata_from_filename(self, filepath):
        """
        Extract metadata from filename (basic implementation).
        
        Args:
            filepath: File path
        
        Returns:
            dict: Metadata dictionary
        """
        filename = os.path.basename(filepath)
        name_without_ext = os.path.splitext(filename)[0]
        
        # Try to parse "Artist - Title" format
        if ' - ' in name_without_ext:
            parts = name_without_ext.split(' - ', 1)
            return {
                'artist': parts[0].strip(),
                'title': parts[1].strip()
            }
        
        # Default: use filename as title
        return {
            'title': name_without_ext,
            'artist': 'Unknown'
        }

