# ══════════════════════════════════════════════════════════════════════════════
#  ShadeMusicBot — Dockerfile
#  Target: Render Web Service (Linux x86_64)
#  Strategy: multi-stage build to keep the final image lean.
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# libc6-dev  — provides stdint.h required by TgCrypto's C extension
# gcc        — C compiler for any extension that needs compilation
# libffi-dev — required by some Python C extensions
# libssl-dev — required by TgCrypto / cryptographic extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first — Docker only rebuilds this layer when the file changes
COPY requirements.txt .

RUN pip install --upgrade pip --no-cache-dir \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: production image ─────────────────────────────────────────────────
FROM python:3.12-slim AS production

# Runtime dependencies:
#   ffmpeg     — audio decoding / transcoding used by pytgcalls / ntgcalls
#   libssl3    — TgCrypto runtime crypto
#   ca-certs   — HTTPS for MongoDB Atlas and Telegram API
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libssl3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --gid 1001 botuser \
 && useradd --uid 1001 --gid botuser --shell /bin/bash --create-home botuser

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application source with correct ownership
COPY --chown=botuser:botuser . .

# Persistent log directory
RUN mkdir -p /app/logs && chown botuser:botuser /app/logs

USER botuser

# Render injects $PORT at runtime; 8080 is the local fallback
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" \
    || exit 1

CMD ["python", "-u", "main.py"]
