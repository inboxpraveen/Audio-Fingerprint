"""Batch song indexing."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core import load_audio, preprocess_audio, Fingerprinter, generate_hashes
from .dataset_loader import DatasetLoader
from .progress_tracker import ProgressTracker


class Indexer:
    """Batch indexer for songs."""
    
    def __init__(self, storage, sr=11025, n_fft=2048, hop_length=512,
                 peak_neighborhood_size=20, min_amplitude=10, fan_value=5):
        """
        Initialize indexer.
        
        Args:
            storage: Storage backend instance
            sr: Sample rate
            n_fft: FFT window size
            hop_length: Hop length
            peak_neighborhood_size: Peak detection neighborhood
            min_amplitude: Minimum peak amplitude
            fan_value: Number of peaks to pair with each anchor
        """
        self.storage = storage
        self.fingerprinter = Fingerprinter(
            sr=sr,
            n_fft=n_fft,
            hop_length=hop_length,
            peak_neighborhood_size=peak_neighborhood_size,
            min_amplitude=min_amplitude
        )
        self.sr = sr
        self.fan_value = fan_value
    
    def index_song(self, filepath, song_id=None, metadata=None):
        """
        Index a single song.
        
        Args:
            filepath: Path to audio file
            song_id: Optional song ID (auto-generated if None)
            metadata: Optional metadata dictionary
        
        Returns:
            tuple: (song_id, success, error_message)
        """
        try:
            # Generate song ID if not provided
            if song_id is None:
                song_id = str(uuid.uuid4())
            
            # Load audio
            audio, sr = load_audio(filepath, sr=self.sr)
            audio = preprocess_audio(audio)
            
            # Calculate duration
            duration = len(audio) / sr
            
            # Generate fingerprint
            peaks = self.fingerprinter.generate_fingerprint(audio)
            
            # Generate hashes
            hashes = generate_hashes(peaks, song_id=song_id, fan_value=self.fan_value)
            
            # Prepare metadata
            if metadata is None:
                metadata = {}
            
            metadata.update({
                'song_id': song_id,
                'filepath': filepath,
                'filename': os.path.basename(filepath),
                'duration': duration,
                'num_peaks': len(peaks),
                'num_hashes': len(hashes)
            })
            
            # Store in database
            self.storage.store_fingerprint(song_id, metadata, hashes)
            
            return song_id, True, None
        
        except Exception as e:
            return song_id, False, str(e)
    
    def index_directory(self, directory_path, num_workers=4, progress_callback=None):
        """
        Index all songs in a directory.
        
        Args:
            directory_path: Path to directory containing audio files
            num_workers: Number of parallel workers
            progress_callback: Optional callback function(current, total, filename)
        
        Returns:
            dict: Indexing results summary
        """
        # Load audio files
        loader = DatasetLoader()
        audio_files = loader.find_audio_files(directory_path)
        
        if not audio_files:
            return {
                'total': 0,
                'success': 0,
                'failed': 0,
                'errors': []
            }
        
        # Initialize progress tracker
        tracker = ProgressTracker(total=len(audio_files))
        
        results = {
            'total': len(audio_files),
            'success': 0,
            'failed': 0,
            'errors': []
        }
        
        # Index songs in parallel
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            # Submit all jobs
            future_to_file = {
                executor.submit(self.index_song, filepath): filepath
                for filepath in audio_files
            }
            
            # Process completed jobs
            for future in as_completed(future_to_file):
                filepath = future_to_file[future]
                filename = os.path.basename(filepath)
                
                try:
                    song_id, success, error_msg = future.result()
                    
                    if success:
                        results['success'] += 1
                    else:
                        results['failed'] += 1
                        results['errors'].append({
                            'file': filepath,
                            'error': error_msg
                        })
                    
                    # Update progress
                    tracker.update(filename)
                    
                    if progress_callback:
                        progress_callback(
                            tracker.current,
                            tracker.total,
                            filename
                        )
                
                except Exception as e:
                    results['failed'] += 1
                    results['errors'].append({
                        'file': filepath,
                        'error': str(e)
                    })
        
        return results

