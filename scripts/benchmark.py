"""Performance benchmarking script."""

import argparse
import sys
import os
import time
import random

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.core import load_audio, preprocess_audio, Fingerprinter, generate_hashes, match_fingerprint
from fingerprint.storage import MemoryStore, SQLiteStore
from fingerprint.utils.logger import setup_logger


def benchmark_fingerprinting(audio_files, num_samples=10):
    """
    Benchmark fingerprinting performance.
    
    Args:
        audio_files: List of audio file paths
        num_samples: Number of samples to test
    
    Returns:
        dict: Benchmark results
    """
    fingerprinter = Fingerprinter()
    times = []
    
    sample_files = random.sample(audio_files, min(num_samples, len(audio_files)))
    
    for filepath in sample_files:
        try:
            start_time = time.time()
            
            # Load and process
            audio, sr = load_audio(filepath)
            audio = preprocess_audio(audio)
            
            # Generate fingerprint
            peaks = fingerprinter.generate_fingerprint(audio)
            hashes = generate_hashes(peaks)
            
            elapsed = time.time() - start_time
            times.append(elapsed)
            
        except Exception as e:
            print(f"Error processing {filepath}: {e}")
    
    return {
        'mean_time': sum(times) / len(times) if times else 0,
        'min_time': min(times) if times else 0,
        'max_time': max(times) if times else 0,
        'samples': len(times)
    }


def benchmark_matching(storage, query_files, num_queries=10):
    """
    Benchmark matching performance.
    
    Args:
        storage: Storage backend
        query_files: List of query audio files
        num_queries: Number of queries to test
    
    Returns:
        dict: Benchmark results
    """
    fingerprinter = Fingerprinter()
    times = []
    
    sample_files = random.sample(query_files, min(num_queries, len(query_files)))
    
    for filepath in sample_files:
        try:
            # Generate query fingerprint
            audio, sr = load_audio(filepath)
            audio = preprocess_audio(audio)
            peaks = fingerprinter.generate_fingerprint(audio)
            query_hashes = generate_hashes(peaks)
            
            # Benchmark matching
            start_time = time.time()
            matches = match_fingerprint(query_hashes, storage)
            elapsed = time.time() - start_time
            
            times.append(elapsed)
            
        except Exception as e:
            print(f"Error querying {filepath}: {e}")
    
    return {
        'mean_time': sum(times) / len(times) if times else 0,
        'min_time': min(times) if times else 0,
        'max_time': max(times) if times else 0,
        'samples': len(times)
    }


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Benchmark audio fingerprinting performance')
    
    parser.add_argument(
        '--audio-dir',
        type=str,
        required=True,
        help='Directory containing audio files'
    )
    
    parser.add_argument(
        '--storage-type',
        type=str,
        default='memory',
        choices=['memory', 'sqlite'],
        help='Storage backend type'
    )
    
    parser.add_argument(
        '--db-path',
        type=str,
        default='./data/database/fingerprint.db',
        help='Database path for SQLite'
    )
    
    parser.add_argument(
        '--num-samples',
        type=int,
        default=10,
        help='Number of samples for fingerprinting benchmark'
    )
    
    parser.add_argument(
        '--num-queries',
        type=int,
        default=10,
        help='Number of queries for matching benchmark'
    )
    
    args = parser.parse_args()
    
    logger = setup_logger()
    
    # Find audio files
    from fingerprint.training import DatasetLoader
    loader = DatasetLoader()
    audio_files = loader.find_audio_files(args.audio_dir)
    
    if not audio_files:
        logger.error(f"No audio files found in {args.audio_dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(audio_files)} audio files")
    
    # Benchmark fingerprinting
    logger.info("\n" + "=" * 60)
    logger.info("Benchmarking Fingerprinting Performance")
    logger.info("=" * 60)
    
    fp_results = benchmark_fingerprinting(audio_files, args.num_samples)
    
    logger.info(f"Samples tested: {fp_results['samples']}")
    logger.info(f"Mean time: {fp_results['mean_time']:.3f}s")
    logger.info(f"Min time: {fp_results['min_time']:.3f}s")
    logger.info(f"Max time: {fp_results['max_time']:.3f}s")
    
    # Benchmark matching (if database exists)
    if args.storage_type == 'sqlite' and os.path.exists(args.db_path):
        logger.info("\n" + "=" * 60)
        logger.info("Benchmarking Matching Performance")
        logger.info("=" * 60)
        
        storage = SQLiteStore(args.db_path)
        match_results = benchmark_matching(storage, audio_files, args.num_queries)
        
        logger.info(f"Queries tested: {match_results['samples']}")
        logger.info(f"Mean time: {match_results['mean_time']:.3f}s")
        logger.info(f"Min time: {match_results['min_time']:.3f}s")
        logger.info(f"Max time: {match_results['max_time']:.3f}s")
        
        # Get storage stats
        stats = storage.get_stats()
        logger.info(f"\nDatabase size: {stats['total_songs']} songs, {stats['total_hashes']} hashes")
    
    logger.info("\nBenchmark complete!")


if __name__ == '__main__':
    main()

