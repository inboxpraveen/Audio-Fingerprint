"""WSGI entry point for production servers.

    gunicorn "fingerprint.api.wsgi:app" --workers 1 --threads 8 --timeout 600
    waitress-serve --port 5000 fingerprint.api.wsgi:app

Configuration comes from ``AUDIOFP_*`` environment variables / ``.env``.
"""

from .app import create_app

app = create_app()
