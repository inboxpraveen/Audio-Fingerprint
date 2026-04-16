"""Default configuration settings."""


class Config:
    """Base configuration."""

    # ---------------------------------------------------------------
    # Audio processing
    # ---------------------------------------------------------------
    SAMPLE_RATE = 11025      # Hz — low enough for speed, high enough for accuracy
    N_FFT = 2048             # FFT window size
    HOP_LENGTH = 512         # Frames between successive STFT windows

    # ---------------------------------------------------------------
    # Fingerprinting
    # ---------------------------------------------------------------
    PEAK_NEIGHBORHOOD_SIZE = 20  # Local-maxima window (time × freq bins)
    MIN_AMPLITUDE = 10           # Minimum spectral peak amplitude
    FAN_VALUE = 10               # Pairs per anchor peak (higher = more robust, more storage)

    # ---------------------------------------------------------------
    # Storage
    # ---------------------------------------------------------------
    STORAGE_TYPE = "sqlite"                         # 'memory' | 'sqlite' | 'postgres'
    SQLITE_DATABASE_PATH = "data/fingerprint.db"
    UPLOAD_FOLDER = "data/uploads"

    # ---------------------------------------------------------------
    # API / uploads
    # ---------------------------------------------------------------
    # 500 MB — generous enough for large uncompressed WAV files
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024
    ALLOWED_EXTENSIONS = {"mp3", "wav", "flac", "m4a", "ogg", "aac", "wma", "opus"}

    # ---------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------
    LOG_LEVEL = "INFO"
    LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
