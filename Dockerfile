# ══════════════════════════════════════════════════════════════════════════════
#  ShadeMusicBot — Dockerfile
#  Target: Render Web Service (Linux x86_64)
#  Strategy: multi-stage build to keep the final image lean.
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Install build dependencies only in the builder stage
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy only the requirements file first to maximise layer cache hits.
# Docker rebuilds from this layer only when requirements.txt changes.
COPY requirements.txt .

RUN pip install --upgrade pip --no-cache-dir \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: production image ─────────────────────────────────────────────────
FROM python:3.12-slim AS production

# Runtime system dependencies (TgCrypto needs libssl at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security best practices
RUN groupadd --gid 1001 botuser \
 && useradd --uid 1001 --gid botuser --shell /bin/bash --create-home botuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=botuser:botuser . .

# Create logs directory with correct ownership
RUN mkdir -p /app/logs && chown botuser:botuser /app/logs

# Drop root privileges
USER botuser

# Render injects PORT; expose the default fallback for local Docker runs
EXPOSE 8080

# Health check — Render also configures this externally via render.yaml,
# but having it in the Dockerfile helps local docker-compose runs.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" \
    || exit 1

CMD ["python", "-u", "main.py"]
