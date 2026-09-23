# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Optional build-time source overlay. An operator build may inject extra source
# modules from a private repo, passed as two BuildKit secrets: the clone target
# ("host/group/project") and a token with read access to it. Absent either, this
# stage copies nothing and the image is built as-is, so a plain `docker build .`
# needs neither. Secrets are mounted only for this RUN and never land in a layer.
# See the self-hosting docs.
#
# On GitLab the token is the pipeline's own CI_JOB_TOKEN, so there is no PAT to
# mint or rotate: the overlay project just lists this one under
# Settings > CI/CD > Job token permissions. See .gitlab-ci.yml.
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS private-sources
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /injected
RUN mkdir -p resolvers scrapers manga
# Cache buster, and it is load-bearing: BuildKit does NOT hash secret CONTENT into
# the cache key, and the clone below is byte-identical every build, so without a
# value that changes the layer is reused and new overlay commits are never baked
# in. CI passes the pipeline id. It is referenced in the RUN so only the clone is
# invalidated; the apt layer above stays cached.
ARG OVERLAY_REV=none
RUN --mount=type=secret,id=sources_token --mount=type=secret,id=sources_repo \
    if [ -s /run/secrets/sources_token ] && [ -s /run/secrets/sources_repo ]; then \
        echo ">> overlay build ${OVERLAY_REV}" && \
        git clone --depth 1 --branch main \
          "https://gitlab-ci-token:$(cat /run/secrets/sources_token)@$(cat /run/secrets/sources_repo).git" /tmp/src && \
        cp /tmp/src/resolvers/*.py resolvers/ && \
        cp /tmp/src/scrapers/*.py scrapers/ && \
        # Older overlays have no manga directory.
        if [ -d /tmp/src/manga ]; then cp /tmp/src/manga/*.py manga/ 2>/dev/null || true; fi && \
        rm -rf /tmp/src && \
        echo ">> overlay applied: $(ls resolvers | wc -l) resolver / $(ls scrapers | wc -l) scraper / $(ls manga | wc -l) manga file(s)"; \
    else \
        echo ">> no overlay secrets supplied, building base image only"; \
    fi

# ---------------------------------------------------------------------------
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg remuxes streams for the server-side video cache and local transcoding.
# No compiler: every Python dependency ships a wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api.py startup.py ./
COPY core ./core
COPY scrapers ./scrapers
COPY resolvers ./resolvers
COPY account_engine ./account_engine
COPY apikey_engine ./apikey_engine
COPY cache_engine ./cache_engine
COPY changelog_engine ./changelog_engine
COPY chat_engine ./chat_engine
COPY discord_bot ./discord_bot
COPY download_engine ./download_engine
COPY iptv_engine ./iptv_engine
COPY local_engine ./local_engine
COPY manga_engine ./manga_engine
COPY metadata_engine ./metadata_engine
COPY notify_engine ./notify_engine
COPY playback_engine ./playback_engine
COPY recommend_engine ./recommend_engine
COPY skiptimes_engine ./skiptimes_engine
COPY subtitles_engine ./subtitles_engine
COPY supporters_engine ./supporters_engine
COPY system_engine ./system_engine
COPY telemetry_engine ./telemetry_engine
# Without the .sql files the container finds no migrations and reports itself
# up to date while unmigrated. tests/core/test_migrations.py guards this line,
# since tests/test_dockerfile_copies.py follows only Python imports.
COPY migrations ./migrations

# Empty without the overlay secrets. manga_engine.provider discovers an overlay
# MangaProvider at runtime.
COPY --from=private-sources /injected/resolvers/ ./resolvers/
COPY --from=private-sources /injected/scrapers/ ./scrapers/
COPY --from=private-sources /injected/manga/ ./manga_engine/

# State lives in PostgreSQL, so the container needs no writable data volume.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# The slim image has no curl. An unhealthy task is rescheduled by Swarm.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

# Trust X-Forwarded-Proto/Host from the TLS-terminating proxy, or proxied iframe
# URLs come out as http and the https frontend blocks them as mixed content.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
