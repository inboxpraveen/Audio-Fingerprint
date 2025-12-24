# Deployment Guide

## Table of Contents

1. [Local Development Setup](#local-development-setup)
2. [Production Deployment](#production-deployment)
3. [Docker Deployment](#docker-deployment)
4. [Nginx Configuration](#nginx-configuration)
5. [SSL/TLS Setup](#ssltls-setup)
6. [Monitoring and Logging](#monitoring-and-logging)
7. [Scaling Strategies](#scaling-strategies)
8. [Backup and Recovery](#backup-and-recovery)

---

## Local Development Setup

### Prerequisites

- Python 3.8 or higher
- FFmpeg (for audio processing)
- Git

### Installation Steps

1. **Clone the repository:**

```bash
git clone <repository-url>
cd Audio-Fingerprint
```

2. **Create virtual environment:**

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/Mac
source venv/bin/activate
```

3. **Install dependencies:**

```bash
pip install -r requirements.txt
```

4. **Install FFmpeg:**

**Windows:**
- Download from https://ffmpeg.org/download.html
- Add to PATH

**Linux:**
```bash
sudo apt-get update
sudo apt-get install ffmpeg libsndfile1
```

**Mac:**
```bash
brew install ffmpeg
```

5. **Create data directories:**

```bash
mkdir -p data/songs
mkdir -p data/database
mkdir -p data/logs
```

6. **Configure environment:**

```bash
cp .env.example .env
# Edit .env with your settings
```

7. **Index some songs:**

```bash
python scripts/train_index.py --songs-dir ./data/songs --workers 4
```

8. **Start development server:**

```bash
python -m fingerprint.api.app
```

The API will be available at http://localhost:5000

---

## Production Deployment

### System Requirements

**Minimum:**
- 2 CPU cores
- 4 GB RAM
- 20 GB storage

**Recommended (for 100k songs):**
- 4+ CPU cores
- 16 GB RAM
- 100 GB SSD storage

### Production Setup

1. **Create production user:**

```bash
sudo useradd -m -s /bin/bash fingerprint
sudo su - fingerprint
```

2. **Install application:**

```bash
cd /opt
git clone <repository-url> fingerprint
cd fingerprint
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. **Configure production settings:**

```bash
cp .env.example .env
nano .env
```

Update the following:
```bash
FLASK_ENV=production
STORAGE_TYPE=sqlite
SQLITE_DATABASE_PATH=/opt/fingerprint/data/database/fingerprint.db
LOG_FILE=/opt/fingerprint/data/logs/production.log
LOG_LEVEL=INFO
```

4. **Create systemd service:**

```bash
sudo nano /etc/systemd/system/fingerprint.service
```

Add the following:

```ini
[Unit]
Description=Audio Fingerprint API
After=network.target

[Service]
Type=notify
User=fingerprint
Group=fingerprint
WorkingDirectory=/opt/fingerprint
Environment="PATH=/opt/fingerprint/venv/bin"
ExecStart=/opt/fingerprint/venv/bin/gunicorn \
    --workers 4 \
    --bind 127.0.0.1:5000 \
    --timeout 120 \
    --access-logfile /opt/fingerprint/data/logs/access.log \
    --error-logfile /opt/fingerprint/data/logs/error.log \
    "fingerprint.api.app:create_app('production')"
Restart=always

[Install]
WantedBy=multi-user.target
```

5. **Start service:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable fingerprint
sudo systemctl start fingerprint
sudo systemctl status fingerprint
```

---

## Docker Deployment

### Build Docker Image

1. **Create Dockerfile:**

Already included in the project structure (see docker/ directory).

2. **Build image:**

```bash
docker build -t audio-fingerprint:latest -f docker/Dockerfile .
```

3. **Run container:**

```bash
docker run -d \
  --name fingerprint \
  -p 5000:5000 \
  -v $(pwd)/data:/app/data \
  -e FLASK_ENV=production \
  -e STORAGE_TYPE=sqlite \
  audio-fingerprint:latest
```

### Docker Compose

1. **Use docker-compose.yml:**

```bash
docker-compose up -d
```

2. **View logs:**

```bash
docker-compose logs -f fingerprint
```

3. **Stop services:**

```bash
docker-compose down
```

---

## Nginx Configuration

### Install Nginx

```bash
sudo apt-get install nginx
```

### Configure Reverse Proxy

Create `/etc/nginx/sites-available/fingerprint`:

```nginx
upstream fingerprint_backend {
    server 127.0.0.1:5000;
}

server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 20M;

    location / {
        proxy_pass http://fingerprint_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Timeouts for long-running requests
        proxy_connect_timeout 120s;
        proxy_send_timeout 120s;
        proxy_read_timeout 120s;
    }

    # Static files (if needed)
    location /static {
        alias /opt/fingerprint/static;
        expires 30d;
    }

    # Health check endpoint
    location /health {
        proxy_pass http://fingerprint_backend/api/v1/health;
        access_log off;
    }
}
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/fingerprint /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## SSL/TLS Setup

### Using Let's Encrypt (Certbot)

1. **Install Certbot:**

```bash
sudo apt-get install certbot python3-certbot-nginx
```

2. **Obtain certificate:**

```bash
sudo certbot --nginx -d your-domain.com
```

3. **Auto-renewal:**

Certbot automatically sets up renewal. Test it:

```bash
sudo certbot renew --dry-run
```

### Manual SSL Configuration

Update Nginx configuration:

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /path/to/certificate.crt;
    ssl_certificate_key /path/to/private.key;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    # ... rest of configuration
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}
```

---

## Monitoring and Logging

### Application Logs

Logs are stored in `data/logs/`:
- `production.log` - Application logs
- `access.log` - HTTP access logs
- `error.log` - Error logs

### Log Rotation

Create `/etc/logrotate.d/fingerprint`:

```
/opt/fingerprint/data/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 fingerprint fingerprint
    sharedscripts
    postrotate
        systemctl reload fingerprint
    endscript
}
```

### Monitoring with Systemd

```bash
# View logs
journalctl -u fingerprint -f

# Check status
systemctl status fingerprint

# View resource usage
systemctl show fingerprint --property=MemoryCurrent
```

### Health Monitoring Script

Create `/opt/fingerprint/scripts/health_check.sh`:

```bash
#!/bin/bash

HEALTH_URL="http://localhost:5000/api/v1/health"
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" $HEALTH_URL)

if [ $RESPONSE -eq 200 ]; then
    echo "Service is healthy"
    exit 0
else
    echo "Service is unhealthy (HTTP $RESPONSE)"
    exit 1
fi
```

Add to crontab:
```bash
*/5 * * * * /opt/fingerprint/scripts/health_check.sh
```

---

## Scaling Strategies

### Vertical Scaling

1. **Increase Gunicorn workers:**

```bash
# Rule of thumb: (2 x CPU cores) + 1
gunicorn --workers 9 ...  # for 4 cores
```

2. **Increase system resources:**
- Add more RAM for larger databases
- Use SSD for faster database access

### Horizontal Scaling

1. **Load Balancer Setup:**

```nginx
upstream fingerprint_cluster {
    least_conn;
    server 10.0.0.1:5000;
    server 10.0.0.2:5000;
    server 10.0.0.3:5000;
}
```

2. **Shared Database:**

Use PostgreSQL for shared storage across nodes:

```python
# config/production.py
STORAGE_TYPE = 'postgres'
POSTGRES_HOST = 'db.example.com'
```

3. **Redis Caching:**

Add Redis for caching frequent queries (requires custom implementation).

### Database Optimization

1. **SQLite Optimization:**

```python
# Add to SQLiteStore.__init__
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('PRAGMA synchronous=NORMAL')
conn.execute('PRAGMA cache_size=-64000')  # 64MB cache
```

2. **PostgreSQL Optimization:**

```sql
-- Increase shared buffers
ALTER SYSTEM SET shared_buffers = '2GB';

-- Increase work memory
ALTER SYSTEM SET work_mem = '64MB';

-- Reload configuration
SELECT pg_reload_conf();
```

---

## Backup and Recovery

### Database Backup

**SQLite:**

```bash
# Backup
sqlite3 data/database/fingerprint.db ".backup data/backups/backup-$(date +%Y%m%d).db"

# Restore
cp data/backups/backup-20231225.db data/database/fingerprint.db
```

**PostgreSQL:**

```bash
# Backup
pg_dump -h localhost -U fingerprint_user fingerprint > backup.sql

# Restore
psql -h localhost -U fingerprint_user fingerprint < backup.sql
```

### Automated Backup Script

Create `/opt/fingerprint/scripts/backup.sh`:

```bash
#!/bin/bash

BACKUP_DIR="/opt/fingerprint/data/backups"
DATE=$(date +%Y%m%d_%H%M%S)
DB_PATH="/opt/fingerprint/data/database/fingerprint.db"

mkdir -p $BACKUP_DIR

# Create backup
sqlite3 $DB_PATH ".backup $BACKUP_DIR/fingerprint_$DATE.db"

# Compress
gzip $BACKUP_DIR/fingerprint_$DATE.db

# Keep only last 7 days
find $BACKUP_DIR -name "fingerprint_*.db.gz" -mtime +7 -delete

echo "Backup completed: fingerprint_$DATE.db.gz"
```

Add to crontab:
```bash
0 2 * * * /opt/fingerprint/scripts/backup.sh
```

### Disaster Recovery

1. **Stop service:**
```bash
sudo systemctl stop fingerprint
```

2. **Restore database:**
```bash
cp backup.db data/database/fingerprint.db
```

3. **Start service:**
```bash
sudo systemctl start fingerprint
```

---

## Performance Tuning

### System Limits

Edit `/etc/security/limits.conf`:

```
fingerprint soft nofile 65536
fingerprint hard nofile 65536
```

### Gunicorn Configuration

Create `gunicorn_config.py`:

```python
workers = 4
worker_class = 'sync'
worker_connections = 1000
timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
```

Use it:
```bash
gunicorn -c gunicorn_config.py "fingerprint.api.app:create_app('production')"
```

---

## Troubleshooting

### Service Won't Start

```bash
# Check logs
journalctl -u fingerprint -n 50

# Check permissions
ls -la /opt/fingerprint/data

# Test manually
cd /opt/fingerprint
source venv/bin/activate
python -m fingerprint.api.app
```

### High Memory Usage

- Reduce Gunicorn workers
- Use SQLite instead of memory storage
- Implement pagination for large result sets

### Slow Queries

- Check database indexes
- Monitor query hash counts
- Consider database optimization

### Database Locked (SQLite)

- Enable WAL mode
- Reduce concurrent writes
- Consider PostgreSQL for high concurrency

