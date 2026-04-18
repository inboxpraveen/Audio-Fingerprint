"""Development environment configuration."""

from .default import Config


class DevelopmentConfig(Config):
    """Development - SQLite persistence so the library survives restarts."""

    DEBUG = True
    TESTING = False

    # Persistent SQLite database
    STORAGE_TYPE = "sqlite"
    SQLITE_DATABASE_PATH = "data/fingerprint_dev.db"
    UPLOAD_FOLDER = "data/uploads"

    LOG_LEVEL = "DEBUG"
    LOG_FILE = "data/logs/development.log"

    FLASK_ENV = "development"
    CORS_ORIGINS = "*"
