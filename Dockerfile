FROM python:3.14-slim

# Don't write .pyc files; flush stdout/stderr so container logs are real-time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies required by lxml and other native packages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY api.py .
COPY player.py .
COPY scrapers ./scrapers
COPY resolvers ./resolvers
COPY metadata_engine ./metadata_engine
COPY account_engine ./account_engine

# Run as a non-root user. /app (mapping DB + its -wal/-shm) and /app/data
# (accounts DB) must be writable; chowning them in the image means a freshly
# created named volume mounted there inherits this ownership.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# Expose FastAPI default port.
EXPOSE 8000

# Container-level health check (no curl in slim — use stdlib urllib). Marks the
# task unhealthy if /health stops returning 200, so Swarm can reschedule it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

# Run the application with uvicorn.
# --proxy-headers + --forwarded-allow-ips=* make uvicorn trust the
# X-Forwarded-Proto/Host set by our TLS-terminating reverse proxy, so the app
# sees the real https scheme (otherwise proxied iframe URLs are emitted as http
# and blocked as mixed content on the https frontend).
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
