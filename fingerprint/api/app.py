"""Flask application factory."""

import os
from flask import Flask, send_from_directory
from flask_cors import CORS

from ..storage import MemoryStore, SQLiteStore, PostgresStore
from ..utils.logger import setup_logger


def create_app(config_name: str = "development") -> Flask:
    """
    Create and configure the Flask application.

    Args:
        config_name: 'development' or 'production'

    Returns:
        Configured Flask app instance
    """
    # Resolve the static folder relative to this file so it works regardless
    # of the working directory the server is launched from.
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")

    app = Flask(__name__, static_folder=static_dir, static_url_path="/static")

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    config_map = {
        "development": "config.development.DevelopmentConfig",
        "production": "config.production.ProductionConfig",
    }
    app.config.from_object(config_map.get(config_name, "config.default.Config"))

    # ------------------------------------------------------------------
    # CORS
    # ------------------------------------------------------------------
    CORS(app, origins=app.config.get("CORS_ORIGINS", "*"))

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    logger = setup_logger(
        log_level=app.config.get("LOG_LEVEL", "INFO"),
        log_file=app.config.get("LOG_FILE"),
    )
    app.logger = logger

    # ------------------------------------------------------------------
    # Ensure required directories exist
    # ------------------------------------------------------------------
    upload_folder = app.config.get("UPLOAD_FOLDER", "data/uploads")
    os.makedirs(upload_folder, exist_ok=True)

    db_path = app.config.get("SQLITE_DATABASE_PATH", "data/fingerprint.db")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Storage backend
    # ------------------------------------------------------------------
    storage_type = app.config.get("STORAGE_TYPE", "sqlite")

    if storage_type == "memory":
        storage = MemoryStore()
    elif storage_type == "sqlite":
        storage = SQLiteStore(db_path)
    elif storage_type == "postgres":
        storage = PostgresStore(
            host=app.config.get("POSTGRES_HOST", "localhost"),
            port=app.config.get("POSTGRES_PORT", 5432),
            database=app.config.get("POSTGRES_DB", "fingerprint"),
            user=app.config.get("POSTGRES_USER", "fingerprint_user"),
            password=app.config.get("POSTGRES_PASSWORD", ""),
        )
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")

    app.storage = storage

    # In-memory job registry: job_id → job_info dict
    # Threads update this dict directly; GIL protects individual dict ops.
    app.jobs = {}

    logger.info(f"Storage backend: {storage_type}")

    # ------------------------------------------------------------------
    # Blueprints
    # ------------------------------------------------------------------
    from .routes import api_bp  # noqa: E402  (local import to avoid circulars)

    app.register_blueprint(api_bp, url_prefix="/api/v1")

    # ------------------------------------------------------------------
    # Frontend route
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    # ------------------------------------------------------------------
    # Error handlers
    # ------------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(error):
        return {"error": "Not found"}, 404

    @app.errorhandler(413)
    def too_large(error):
        return {"error": "File too large"}, 413

    @app.errorhandler(500)
    def internal_error(error):
        return {"error": "Internal server error"}, 500

    logger.info(f"AudioFP app ready (config={config_name})")
    return app
