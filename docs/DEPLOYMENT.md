# Deployment

## Local / Development

```bash
pip install -r requirements.txt
python run.py
# → http://localhost:5000
```

The development config (`config/development.py`) uses:
- SQLite at `data/fingerprint_dev.db`
- Uploads stored at `data/uploads/`
- `DEBUG=True` with detailed logging to `data/logs/development.log`
- CORS open to all origins (`*`)

---

## Production

### 1. Switch to the production config

```bash
python run.py --env production --port 8000
```

Or with gunicorn for multi-worker deployments:

```bash
gunicorn "fingerprint.api.app:create_app('production')" \
  --bind 0.0.0.0:8000 \
  --workers 4 \
  --threads 2 \
  --timeout 300 \
  --worker-class gthread
```

> **Note:** Use `--threads` rather than `--workers` alone when using SQLite, because thread-local connections are cheaper than forked processes competing for the same file.

### 2. Production config (`config/production.py`)

Key settings to review:

```python
STORAGE_TYPE = "sqlite"
SQLITE_DATABASE_PATH = "/var/data/audiofp/fingerprint.db"
UPLOAD_FOLDER = "/var/data/audiofp/uploads"
CORS_ORIGINS = ["https://yourdomain.com"]
LOG_LEVEL = "WARNING"
```

### 3. Reverse proxy (nginx example)

```nginx
server {
    listen 443 ssl;
    server_name audiofp.yourdomain.com;

    client_max_body_size 512m;   # match MAX_CONTENT_LENGTH

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 300s;   # long enough for large file uploads
    }
}
```

---

## Environment Variables

You can override config values at runtime by modifying `config/production.py` or subclassing `Config`. There is intentionally no dotenv loading to keep the setup explicit.

---

## Data Directories

AudioFP creates these automatically on startup:

| Path | Contents |
|------|----------|
| `data/fingerprint_dev.db` | SQLite database (dev) |
| `data/uploads/` | Audio files uploaded via the UI |
| `data/logs/` | Log files (when `LOG_FILE` is set) |

Back up the `.db` file to preserve your indexed library. Uploaded files in `data/uploads/` can be deleted after indexing if storage is a concern — the fingerprints remain in the database.

---

## Scaling

| Scenario | Recommendation |
|----------|----------------|
| < 10 K songs | Default SQLite config is fine |
| 10 K – 100 K songs | Increase `cache_size` pragma in `sqlite_store.py`; consider an SSD |
| > 100 K songs | Migrate to PostgreSQL (`STORAGE_TYPE = "postgres"`) |
| Many concurrent users | Add a gunicorn worker per CPU core; use PostgreSQL |

SQLite's WAL mode handles concurrent reads well. Writes (indexing) will briefly block reads only during the commit. For read-heavy production loads this is generally acceptable.
