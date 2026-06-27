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
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
