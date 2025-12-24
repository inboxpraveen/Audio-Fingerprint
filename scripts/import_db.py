"""Import/restore database script."""

import argparse
import sys
import os
import json
import pickle

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.storage import MemoryStore, SQLiteStore
from fingerprint.utils.logger import setup_logger


def import_from_json(storage, input_path):
    """
    Import database from JSON format.
    
    Args:
        storage: Storage backend
        input_path: Input file path
    """
    logger = setup_logger()
    
    # Read from file
    with open(input_path, 'r') as f:
        import_data = json.load(f)
    
    songs = import_data.get('songs', [])
    
    logger.info(f"Importing {len(songs)} songs...")
    
    # Note: JSON export doesn't include hashes, only metadata
    logger.warning("JSON import only restores song metadata, not fingerprint hashes")
    logger.warning("You need to re-index songs to generate fingerprints")
    
    # Store metadata
    for song in songs:
        song_id = song.get('song_id')
        if song_id:
            # Store with empty hashes
            storage.store_fingerprint(song_id, song, [])
    
    logger.info(f"Imported {len(songs)} song metadata entries")


def import_from_pickle(storage, input_path):
    """
    Import database from pickle format.
    
    Args:
        storage: Storage backend
        input_path: Input file path
    """
    logger = setup_logger()
    
    if not isinstance(storage, MemoryStore):
        logger.error("Pickle import only supported for MemoryStore")
        sys.exit(1)
    
    # Read from file
    with open(input_path, 'rb') as f:
        import_data = pickle.load(f)
    
    # Restore data
    storage.hash_table = import_data['hash_table']
    storage.song_metadata = import_data['song_metadata']
    storage.total_hashes = import_data['total_hashes']
    
    logger.info(f"Imported {len(storage.song_metadata)} songs")
    logger.info(f"Total hashes: {storage.total_hashes}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Import/restore fingerprint database')
    
    parser.add_argument(
        '--storage-type',
        type=str,
        default='sqlite',
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
        '--input',
        type=str,
        required=True,
        help='Input file path'
    )
    
    parser.add_argument(
        '--format',
        type=str,
        default='json',
        choices=['json', 'pickle'],
        help='Import format (default: json)'
    )
    
    args = parser.parse_args()
    
    logger = setup_logger()
    
    # Validate input file
    if not os.path.exists(args.input):
        logger.error(f"Input file not found: {args.input}")
        sys.exit(1)
    
    # Initialize storage
    if args.storage_type == 'memory':
        storage = MemoryStore()
    elif args.storage_type == 'sqlite':
        # Create database directory if needed
        db_dir = os.path.dirname(args.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        storage = SQLiteStore(args.db_path)
    
    # Import based on format
    if args.format == 'json':
        import_from_json(storage, args.input)
    elif args.format == 'pickle':
        import_from_pickle(storage, args.input)
    
    logger.info("Import complete!")


if __name__ == '__main__':
    main()

