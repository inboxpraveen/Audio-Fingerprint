"""Dataset loading utilities."""

import os


class DatasetLoader:
    """Scan directories for supported audio and video files."""

    AUDIO_EXTENSIONS = frozenset({
        ".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".opus",
    })

    VIDEO_EXTENSIONS = frozenset({
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
        ".webm", ".m4v", ".mpeg", ".mpg", ".ts", ".mts", ".3gp", ".vob",
    })

    SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

    def find_audio_files(self, directory_path: str, recursive: bool = True) -> list:
        """
        Find all supported audio and video files in a directory.

        Args:
            directory_path: Root directory path
            recursive:      Search sub-directories (default: True)

        Returns:
            Sorted list of absolute file paths
        """
        found = []

        if recursive:
            for root, _dirs, files in os.walk(directory_path):
                for filename in files:
                    if self._is_supported(filename):
                        found.append(os.path.join(root, filename))
        else:
            for filename in os.listdir(directory_path):
                fp = os.path.join(directory_path, filename)
                if os.path.isfile(fp) and self._is_supported(filename):
                    found.append(fp)

        return sorted(found)

    def _is_supported(self, filename: str) -> bool:
        return os.path.splitext(filename)[1].lower() in self.SUPPORTED_EXTENSIONS

    def load_metadata_from_filename(self, filepath: str) -> dict:
        """
        Extract title / artist from the filename.

        Recognises the common "Artist - Title" convention.
        Falls back to using the bare filename stem as the title.
        """
        stem = os.path.splitext(os.path.basename(filepath))[0]

        if " - " in stem:
            artist, title = stem.split(" - ", 1)
            return {"artist": artist.strip(), "title": title.strip()}

        return {"title": stem, "artist": "Unknown"}
