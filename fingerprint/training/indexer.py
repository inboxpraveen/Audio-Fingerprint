"""Batch song indexer."""

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core import load_audio, preprocess_audio, Fingerprinter, generate_hashes
from ..core.audio_processor import is_video_file
from .dataset_loader import DatasetLoader
from .progress_tracker import ProgressTracker


class Indexer:
    """Batch indexer - fingerprints one or many audio files and stores results."""

    def __init__(
        self,
        storage,
        sr: int = 11025,
        n_fft: int = 2048,
        hop_length: int = 512,
        peak_neighborhood_size: int = 20,
        min_amplitude: int = 10,
        fan_value: int = 10,
    ):
        self.storage = storage
        self.fingerprinter = Fingerprinter(
            sr=sr,
            n_fft=n_fft,
            hop_length=hop_length,
            peak_neighborhood_size=peak_neighborhood_size,
            min_amplitude=min_amplitude,
        )
        self.sr = sr
        self.fan_value = fan_value
        self._loader = DatasetLoader()

    # ------------------------------------------------------------------
    # Single file
    # ------------------------------------------------------------------

    def index_song(self, filepath: str, song_id: str = None, metadata: dict = None):
        """
        Index a single audio file.

        Returns:
            (song_id, success: bool, error_message: str | None)
        """
        try:
            if song_id is None:
                song_id = str(uuid.uuid4())

            audio, sr = load_audio(filepath, sr=self.sr)
            audio = preprocess_audio(audio)

            duration = len(audio) / sr
            peaks = self.fingerprinter.generate_fingerprint(audio)
            hashes = generate_hashes(peaks, song_id=song_id, fan_value=self.fan_value)

            # Start with filename-based metadata, then overlay caller-supplied values
            file_meta = self._loader.load_metadata_from_filename(filepath)

            base_meta = {
                "song_id": song_id,
                "filepath": filepath,
                "filename": os.path.basename(filepath),
                "duration": round(duration, 3),
                "num_peaks": len(peaks),
                "num_hashes": len(hashes),
                "indexed_at": time.time(),
                "source_type": "video" if is_video_file(filepath) else "audio",
                **file_meta,  # title, artist (parsed from filename)
            }

            if metadata:
                base_meta.update(metadata)

            self.storage.store_fingerprint(song_id, base_meta, hashes)
            return song_id, True, None

        except Exception as exc:
            return song_id, False, str(exc)

    # ------------------------------------------------------------------
    # Directory (parallel)
    # ------------------------------------------------------------------

    def index_directory(
        self,
        directory_path: str,
        num_workers: int = 4,
        progress_callback=None,
    ) -> dict:
        """
        Index all audio files in a directory (recursively).

        Args:
            directory_path:    Root directory to scan
            num_workers:       Parallel worker threads
            progress_callback: Optional callable(current, total, filename)

        Returns:
            dict with keys: total, success, failed, errors
        """
        audio_files = self._loader.find_audio_files(directory_path)

        if not audio_files:
            return {"total": 0, "success": 0, "failed": 0, "errors": []}

        tracker = ProgressTracker(total=len(audio_files))
        results = {"total": len(audio_files), "success": 0, "failed": 0, "errors": []}

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            future_to_path = {
                pool.submit(self.index_song, fp): fp for fp in audio_files
            }

            for future in as_completed(future_to_path):
                filepath = future_to_path[future]
                filename = os.path.basename(filepath)
                try:
                    _, success, error_msg = future.result()
                    if success:
                        results["success"] += 1
                    else:
                        results["failed"] += 1
                        results["errors"].append({"file": filepath, "error": error_msg})
                except Exception as exc:
                    results["failed"] += 1
                    results["errors"].append({"file": filepath, "error": str(exc)})

                tracker.update(filename)
                if progress_callback:
                    progress_callback(tracker.current, tracker.total, filename)

        return results
