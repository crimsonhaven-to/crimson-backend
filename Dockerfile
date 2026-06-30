# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Optional build-time source overlay. An operator build may inject extra source
# modules via two BuildKit secrets (a clone URL + a token); absent either, this is
# a no-op and the image is built as-is. Secrets are mounted only for this RUN and
# never land in any image layer. See the self-hosting docs.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS private-sources
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /injected
RUN mkdir -p resolvers scrapers
RUN --mount=type=secret,id=sources_pat --mount=type=secret,id=sources_repo \
    if [ -s /run/secrets/sources_pat ] && [ -s /run/secrets/sources_repo ]; then \
        git clone --depth 1 \
          "https://x-access-token:$(cat /run/secrets/sources_pat)@github.com/$(cat /run/secrets/sources_repo).git" /tmp/src && \
        cp /tmp/src/resolvers/*.py resolvers/ && \
        cp /tmp/src/scrapers/*.py scrapers/ && \
        rm -rf /tmp/src && \
        echo ">> overlay applied: $(ls resolvers | wc -l) resolver / $(ls scrapers | wc -l) scraper file(s)"; \
    else \
        echo ">> no overlay secrets supplied — building base image only"; \
    fi

# ---------------------------------------------------------------------------
FROM python:3.14-slim

# Don't write .pyc files; flush stdout/stderr so container logs are real-time.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies required by lxml and other native packages.
# ffmpeg powers the server-side video cache (remuxes played HLS/mp4 streams to a
# single .mp4 on the NAS — see cache_engine/).
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2 \
    libxslt1.1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code.
COPY api.py .
# Shared infrastructure: config, db pool, rate limiter, HTTP client, response
# cache, plus the small app-wide modules (lumi, player, source_health).
COPY core ./core
COPY scrapers ./scrapers
COPY resolvers ./resolvers
COPY metadata_engine ./metadata_engine
COPY account_engine ./account_engine
COPY supporters_engine ./supporters_engine
COPY discord_bot ./discord_bot
COPY local_engine ./local_engine
COPY cache_engine ./cache_engine
COPY changelog_engine ./changelog_engine
COPY recommend_engine ./recommend_engine
COPY apikey_engine ./apikey_engine
COPY subtitles_engine ./subtitles_engine
COPY skiptimes_engine ./skiptimes_engine
COPY telemetry_engine ./telemetry_engine

# Apply the optional build-time source overlay on top of the base packages. In a
# build without the overlay secrets these directories are empty, so this is a no-op.
COPY --from=private-sources /injected/resolvers/ ./resolvers/
COPY --from=private-sources /injected/scrapers/ ./scrapers/

# Run as a non-root user. State now lives in PostgreSQL (see db_pool.py), so the
# container is stateless and needs no writable data volume.
RUN useradd --create-home --uid 10001 appuser \
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
