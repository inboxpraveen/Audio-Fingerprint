# AudioFP production image
#
#   docker build -t audiofp -f docker/Dockerfile .
#   docker run -p 5000:5000 -v audiofp-data:/data -e AUDIOFP_API_KEY=secret audiofp
#
# ffmpeg is installed in the image, so video containers and M4A/AAC/WMA decode without any extra setup.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AUDIOFP_PROFILE=production \
    AUDIOFP_HOST=0.0.0.0 \
    AUDIOFP_PORT=5000 \
    AUDIOFP_DATA_DIR=/data \
    AUDIOFP_LOG_FILE=

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml requirements.txt README.md LICENSE ./
COPY fingerprint ./fingerprint
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 1000 audiofp \
    && mkdir -p /data && chown audiofp:audiofp /data

USER audiofp
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/api/v1/health || exit 1

CMD ["audiofp", "serve", "--server", "waitress"]
