"""Development environment configuration."""

from .default import Config


class DevelopmentConfig(Config):
    """Development configuration."""
    
    DEBUG = True
    TESTING = False
    
    # Storage
    STORAGE_TYPE = 'memory'
    SQLITE_DATABASE_PATH = './data/database/fingerprint_dev.db'
    
    # Logging
    LOG_LEVEL = 'DEBUG'
    LOG_FILE = './data/logs/development.log'
    
    # API
    FLASK_ENV = 'development'
    CORS_ORIGINS = '*'

