"""Production environment configuration."""

from .default import Config


class ProductionConfig(Config):
    """Production configuration."""
    
    DEBUG = False
    TESTING = False
    
    # Storage
    STORAGE_TYPE = 'sqlite'  # or 'postgres'
    SQLITE_DATABASE_PATH = './data/database/fingerprint.db'
    
    # PostgreSQL (optional)
    POSTGRES_HOST = 'localhost'
    POSTGRES_PORT = 5432
    POSTGRES_DB = 'fingerprint'
    POSTGRES_USER = 'fingerprint_user'
    POSTGRES_PASSWORD = 'change_this_password'
    
    # Logging
    LOG_LEVEL = 'INFO'
    LOG_FILE = './data/logs/production.log'
    
    # API
    FLASK_ENV = 'production'
    CORS_ORIGINS = []  # Specify allowed origins
    
    # Performance
    NUM_WORKERS = 4
    BATCH_SIZE = 100

