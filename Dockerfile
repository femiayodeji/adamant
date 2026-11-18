# One process serves the REST API, both WebSockets, and the static frontend
# (plan Phase 6 — deliberately thin). Two stages so Node never ships at runtime.

# ─── stage 1: build the static frontend ───
FROM node:22-slim AS frontend
WORKDIR /build

# Copy the manifests alone first: this layer only rebuilds when dependencies
# change, not on every source edit.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
# The rig lives in statics/ (the original ADAM asset) and is copied into the
# export so the image is self-contained and needs no CDN.
COPY statics/models/xbot.glb ./public/models/xbot.glb
RUN npm run build


# ─── stage 2: runtime ───
FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ARENA_LOG_DIR=/data/logs \
    PORT=8000

# Dependencies before source, so an edit to server/ does not reinstall FastAPI.
COPY pyproject.toml README.md ./
COPY server/__init__.py ./server/
RUN pip install --no-cache-dir .

COPY server/ ./server/
COPY agents/ ./agents/
COPY docs/ ./docs/
COPY --from=frontend /build/out ./frontend/out
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Match logs are the evaluation dataset and the crash-recovery substrate, not
# scratch space. Mount a real volume here or a restart loses in-flight matches
# and every result ever recorded.
#
# The image starts as root only so the entrypoint can chown a host-mounted disk
# (a fresh Render disk is root-owned); it drops to `arena` before uvicorn starts.
RUN useradd --create-home --uid 10001 arena \
    && mkdir -p /data/logs \
    && chown -R arena:arena /data/logs /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
VOLUME ["/data/logs"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/api/health\").status==200 else 1)"

# `exec` so uvicorn becomes PID 1 and receives SIGTERM directly — without it the
# shell swallows the signal, websockets are cut rather than closed, and the
# lifespan shutdown never runs.
#
# One worker is required, not a default worth tuning: the match registry is an
# in-memory dict and tick loops are asyncio tasks inside this process. A second
# worker would serve matches it cannot see.
#
# $PORT is read at start so the same image runs on hosts that assign a port
# (Render, Fly, Cloud Run) with no rebuild.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-graceful-shutdown 20"]
