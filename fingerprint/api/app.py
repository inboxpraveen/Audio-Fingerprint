"""Flask application factory."""

import os
from flask import Flask
from flask_cors import CORS

from ..storage import MemoryStore, SQLiteStore, PostgresStore
from ..utils.logger import setup_logger


def create_app(config_name='development'):
    """
    Create and configure Flask application.
    
    Args:
        config_name: Configuration name ('development', 'production')
    
    Returns:
        Flask app instance
    """
    app = Flask(__name__)
    
    # Load configuration
    config_map = {
        'development': 'config.development.DevelopmentConfig',
        'production': 'config.production.ProductionConfig',
    }
    
    config_class = config_map.get(config_name, 'config.default.Config')
    app.config.from_object(config_class)
    
    # Setup CORS
    cors_origins = app.config.get('CORS_ORIGINS', '*')
    CORS(app, origins=cors_origins)
    
    # Setup logging
    logger = setup_logger(
        log_level=app.config.get('LOG_LEVEL', 'INFO'),
        log_file=app.config.get('LOG_FILE')
    )
    app.logger = logger
    
    # Initialize storage backend
    storage_type = app.config.get('STORAGE_TYPE', 'memory')
    
    if storage_type == 'memory':
        storage = MemoryStore()
    elif storage_type == 'sqlite':
        db_path = app.config.get('SQLITE_DATABASE_PATH', 'fingerprint.db')
        storage = SQLiteStore(db_path)
    elif storage_type == 'postgres':
        storage = PostgresStore(
            host=app.config.get('POSTGRES_HOST', 'localhost'),
            port=app.config.get('POSTGRES_PORT', 5432),
            database=app.config.get('POSTGRES_DB', 'fingerprint'),
            user=app.config.get('POSTGRES_USER', 'fingerprint_user'),
            password=app.config.get('POSTGRES_PASSWORD', '')
        )
    else:
        raise ValueError(f"Unknown storage type: {storage_type}")
    
    app.storage = storage
    logger.info(f"Initialized {storage_type} storage backend")
    
    # Register blueprints
    from .routes import api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')
    
    # Error handlers
    @app.errorhandler(404)
    def not_found(error):
        return {'error': 'Not found'}, 404
    
    @app.errorhandler(500)
    def internal_error(error):
        return {'error': 'Internal server error'}, 500
    
    logger.info(f"Flask app created with config: {config_name}")
    
    return app


if __name__ == '__main__':
    # Development server
    app = create_app('development')
    app.run(host='0.0.0.0', port=5000, debug=True)

