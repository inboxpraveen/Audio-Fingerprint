"""Input validation utilities."""

import os


def validate_audio_file(file, allowed_extensions, max_size=None):
    """
    Validate uploaded audio file.
    
    Args:
        file: Werkzeug FileStorage object
        allowed_extensions: Set of allowed file extensions
        max_size: Maximum file size in bytes (optional)
    
    Returns:
        tuple: (is_valid, error_message)
    """
    # Check if file has a filename
    if not file or not file.filename:
        return False, 'No file selected'
    
    # Check file extension
    filename = file.filename.lower()
    extension = filename.rsplit('.', 1)[-1] if '.' in filename else ''
    
    if extension not in allowed_extensions:
        return False, f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'
    
    # Check file size (if specified)
    if max_size is not None:
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)
        
        if file_size > max_size:
            max_mb = max_size / (1024 * 1024)
            return False, f'File too large. Maximum size: {max_mb:.1f}MB'
    
    return True, None


def validate_directory_path(path):
    """
    Validate directory path.
    
    Args:
        path: Directory path string
    
    Returns:
        tuple: (is_valid, error_message)
    """
    if not path:
        return False, 'Path cannot be empty'
    
    if not os.path.exists(path):
        return False, 'Path does not exist'
    
    if not os.path.isdir(path):
        return False, 'Path is not a directory'
    
    return True, None

