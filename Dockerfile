FROM python:3.12-slim

WORKDIR /app

# System deps: build-essential for bcrypt, curl for healthcheck, sqlite3
# for the optional backup sidecar.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-create the data/ directory (mounted as a volume). The Dockerfile
# also creates uploads + sent_log under the app dir, which docker-compose
# bind-mounts to host volumes.
RUN mkdir -p /data uploads sent_log \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app

# Don't USER-switch here — the entrypoint needs root to chown the
# /data named volume, then drops to appuser via setpriv before exec.

EXPOSE 8123

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8123/api/health || exit 1

# Named volumes are created root-owned; the entrypoint chowns /data so
# appuser can write to it before uvicorn starts. Copy + chmod happen as
# root so appuser doesn't hit "Operation not permitted" on chmod later.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8123"]
