"""Default configuration settings."""

class Config:
    """Base configuration class."""
    
    # Audio processing
    SAMPLE_RATE = 11025
    N_FFT = 2048
    HOP_LENGTH = 512
    
    # Fingerprinting
    PEAK_NEIGHBORHOOD_SIZE = 20
    MIN_AMPLITUDE = 10
    FAN_VALUE = 5
    
    # Storage
    STORAGE_TYPE = 'memory'  # 'memory', 'sqlite', 'postgres'
    
    # API
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    ALLOWED_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg'}
    
    # Logging
    LOG_LEVEL = 'INFO'
    LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

