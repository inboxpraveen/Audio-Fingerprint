"""CLI script to index songs from a directory."""

import argparse
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.storage import MemoryStore, SQLiteStore
from fingerprint.training import Indexer
from fingerprint.utils.logger import setup_logger


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Index audio files for fingerprinting')
    
    parser.add_argument(
        '--songs-dir',
        type=str,
        required=True,
        help='Directory containing audio files to index'
    )
    
    parser.add_argument(
        '--storage-type',
        type=str,
        default='memory',
        choices=['memory', 'sqlite'],
        help='Storage backend type (default: memory)'
    )
    
    parser.add_argument(
        '--db-path',
        type=str,
        default='./data/database/fingerprint.db',
        help='Database file path for SQLite (default: ./data/database/fingerprint.db)'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        default=4,
        help='Number of parallel workers (default: 4)'
    )
    
    parser.add_argument(
        '--sample-rate',
        type=int,
        default=11025,
        help='Audio sample rate (default: 11025)'
    )
    
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)'
    )
    
    args = parser.parse_args()
    
    # Setup logger
    logger = setup_logger(log_level=args.log_level)
    
    # Validate directory
    if not os.path.isdir(args.songs_dir):
        logger.error(f"Directory not found: {args.songs_dir}")
        sys.exit(1)
    
    # Initialize storage
    logger.info(f"Initializing {args.storage_type} storage...")
    
    if args.storage_type == 'memory':
        storage = MemoryStore()
    elif args.storage_type == 'sqlite':
        # Create database directory if it doesn't exist
        db_dir = os.path.dirname(args.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        storage = SQLiteStore(args.db_path)
    else:
        logger.error(f"Unknown storage type: {args.storage_type}")
        sys.exit(1)
    
    # Initialize indexer
    logger.info("Initializing indexer...")
    indexer = Indexer(storage, sr=args.sample_rate)
    
    # Progress callback
    def progress_callback(current, total, filename):
        percent = (current / total) * 100 if total > 0 else 0
        logger.info(f"Progress: {current}/{total} ({percent:.1f}%) - {filename}")
    
    # Index songs
    logger.info(f"Starting indexing from: {args.songs_dir}")
    logger.info(f"Using {args.workers} workers")
    
    results = indexer.index_directory(
        args.songs_dir,
        num_workers=args.workers,
        progress_callback=progress_callback
    )
    
    # Print results
    logger.info("=" * 60)
    logger.info("Indexing Complete!")
    logger.info("=" * 60)
    logger.info(f"Total files processed: {results['total']}")
    logger.info(f"Successfully indexed: {results['success']}")
    logger.info(f"Failed: {results['failed']}")
    
    if results['errors']:
        logger.warning(f"\nErrors encountered ({len(results['errors'])}):")
        for error in results['errors'][:10]:  # Show first 10 errors
            logger.warning(f"  - {error['file']}: {error['error']}")
        
        if len(results['errors']) > 10:
            logger.warning(f"  ... and {len(results['errors']) - 10} more errors")
    
    # Print storage stats
    stats = storage.get_stats()
    logger.info("\nDatabase Statistics:")
    logger.info(f"  Total songs: {stats['total_songs']}")
    logger.info(f"  Total hashes: {stats['total_hashes']}")
    logger.info(f"  Unique hashes: {stats.get('unique_hashes', 'N/A')}")
    
    logger.info("\nIndexing completed successfully!")


if __name__ == '__main__':
    main()

