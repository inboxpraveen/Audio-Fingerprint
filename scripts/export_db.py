"""Export/backup database script."""

import argparse
import sys
import os
import json
import pickle

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fingerprint.storage import MemoryStore, SQLiteStore
from fingerprint.utils.logger import setup_logger


def export_to_json(storage, output_path):
    """
    Export database to JSON format.
    
    Args:
        storage: Storage backend
        output_path: Output file path
    """
    logger = setup_logger()
    
    # Get all songs and metadata
    songs = storage.get_all_songs()
    stats = storage.get_stats()
    
    export_data = {
        'metadata': {
            'storage_type': stats.get('storage_type'),
            'total_songs': stats.get('total_songs'),
            'total_hashes': stats.get('total_hashes'),
            'export_timestamp': None  # Could add timestamp
        },
        'songs': songs
    }
    
    # Write to file
    with open(output_path, 'w') as f:
        json.dump(export_data, f, indent=2)
    
    logger.info(f"Exported {len(songs)} songs to {output_path}")


def export_to_pickle(storage, output_path):
    """
    Export database to pickle format (includes hashes).
    
    Args:
        storage: Storage backend
        output_path: Output file path
    """
    logger = setup_logger()
    
    if isinstance(storage, MemoryStore):
        # For memory store, we can pickle the entire data structure
        export_data = {
            'hash_table': dict(storage.hash_table),
            'song_metadata': storage.song_metadata,
            'total_hashes': storage.total_hashes
        }
        
        with open(output_path, 'wb') as f:
            pickle.dump(export_data, f)
        
        logger.info(f"Exported database to {output_path}")
    else:
        logger.error("Pickle export only supported for MemoryStore")
        sys.exit(1)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Export/backup fingerprint database')
    
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
        '--output',
        type=str,
        required=True,
        help='Output file path'
    )
    
    parser.add_argument(
        '--format',
        type=str,
        default='json',
        choices=['json', 'pickle'],
        help='Export format (default: json)'
    )
    
    args = parser.parse_args()
    
    logger = setup_logger()
    
    # Initialize storage
    if args.storage_type == 'memory':
        logger.error("Cannot export from memory store (use pickle format with active session)")
        sys.exit(1)
    elif args.storage_type == 'sqlite':
        if not os.path.exists(args.db_path):
            logger.error(f"Database not found: {args.db_path}")
            sys.exit(1)
        storage = SQLiteStore(args.db_path)
    
    # Create output directory if needed
    output_dir = os.path.dirname(args.output)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
    
    # Export based on format
    if args.format == 'json':
        export_to_json(storage, args.output)
    elif args.format == 'pickle':
        export_to_pickle(storage, args.output)
    
    logger.info("Export complete!")


if __name__ == '__main__':
    main()

