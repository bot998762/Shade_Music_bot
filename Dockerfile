# ══════════════════════════════════════════════════════════════════════════════
#  ShadeMusicBot — Dockerfile
#  Phase 2: Stable Music Engine Migration
#  Target: Render Web Service (Linux x86_64, Python 3.12)
#  Strategy: multi-stage build — builder installs packages; production is lean.
# ══════════════════════════════════════════════════════════════════════════════

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Build-time dependencies for C extensions:
#   gcc, libc6-dev, libffi-dev, libssl-dev — required by TgCrypto-pyrofork
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Upgrade pip first so it correctly resolves modern wheel metadata (PEP 658).
# This is especially important for ntgcalls which uses complex wheel selectors.
RUN pip install --upgrade pip --no-cache-dir

# Copy requirements first — Docker only rebuilds this layer on file change.
COPY requirements.txt .
# cache-bust: 2026-08-06-v5

# Install into /install prefix so we can copy only what's needed to production.
# --prefer-binary: prefer pre-built wheels over source builds (critical for
#   ntgcalls which has no source distribution — wheels only).
RUN pip install \
        --no-cache-dir \
        --prefer-binary \
        --prefix=/install \
        -r requirements.txt


# ── Stage 2: production image ─────────────────────────────────────────────────
FROM python:3.12-slim AS production

# Runtime dependencies + Deno (JS runtime required by yt-dlp for YouTube):
#
#   ffmpeg     — audio decoding / transcoding for pytgcalls / ntgcalls
#   libssl3    — TgCrypto-pyrofork runtime crypto
#   ca-certs   — HTTPS for MongoDB Atlas, Telegram API, YouTube CDN
#   curl, unzip  — download and extract the Deno installer; both purged after use
#
# Since yt-dlp 2025.11.12, an external JavaScript runtime is required for
# full YouTube support (n-signature and PO-token challenges).  Deno is the
# default and recommended runtime; yt-dlp auto-detects it at
# /usr/local/bin/deno — no yt-dlp config changes are needed.
#
# DENO_INSTALL=/usr/local  → binary lands at /usr/local/bin/deno (on PATH).
# Minimum Deno for yt-dlp 2026.x: v2.3.0.  The installer fetches latest,
# which is always ≥ 2.3.0 and backwards-compatible with yt-dlp's usage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libssl3 \
        ca-certificates \
        curl \
        unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — least-privilege principle.
RUN groupadd --gid 1001 botuser \
 && useradd --uid 1001 --gid botuser --shell /bin/bash --create-home botuser

WORKDIR /app

# Copy installed Python packages from builder stage.
COPY --from=builder /install /usr/local

# Copy application source with correct ownership.
COPY --chown=botuser:botuser . .

# Persistent log directory for loguru rotation.
RUN mkdir -p /app/logs && chown botuser:botuser /app/logs

USER botuser

# ── Deno/yt-dlp-ejs JIT pre-warm ──────────────────────────────────────────────
# Context
# -------
# yt-dlp ≥ 2025.11.12 spawns Deno to compute YouTube PO tokens before returning
# stream URLs.  On Render's ephemeral free-plan containers the Deno module cache
# (~/.cache/deno) is empty on every cold start.  Deno JIT-compiles yt-dlp-ejs
# from scratch, which takes > 30 s on a throttled 512 MB instance and causes
# StreamResolveTimeoutError on every /play command.
#
# Fix
# ---
# Compile yt-dlp-ejs through Deno at IMAGE BUILD TIME so the V8 bytecode cache
# is baked into this image layer.  At container start the cache already exists;
# Deno reads compiled bytecode and starts in < 1 s.
#
# Why stdin=/dev/null is sufficient
# ----------------------------------
# Deno's V8 compilation (Phase 1) runs before any userspace JS executes.
# The cache key is (absolute-file-path, content-hash) — independent of stdin.
# Running with stdin=/dev/null triggers the identical Phase 1 write as a real
# yt-dlp invocation.  If yt-dlp-ejs then blocks waiting for IPC input (Phase 2),
# timeout(30) kills it — but Phase 1, and therefore the cache, is already done.
#
# Safety guarantees
# -----------------
#   stdout/stderr → /dev/null : eliminates SIGPIPE risk from any piping
#   stdin         → /dev/null : yt-dlp-ejs exits after reading EOF (or is
#                               killed after 30 s — Phase 1 already complete)
#   timeout 30                : prevents a broken EJS from hanging the build
#   DENO_NO_UPDATE_CHECK=1    : suppresses deno.land version-check network call
#   DENO_DIR set explicitly   : deterministic cache path matches runtime path
#   exit 1 on EJS not found   : build fails loudly instead of producing a
#                               silently broken image
#   exit 1 on unexpected deno error code (not 0 or 124): same loud failure
#   exit code 124 (timeout)   : treated as success — Phase 1 is provably done
#
# Must run as botuser (same uid as runtime) so cache writes to
# /home/botuser/.cache/deno — the default DENO_DIR for this user at runtime.
# Must be after COPY steps so yt-dlp-ejs is already present on disk.
RUN export DENO_NO_UPDATE_CHECK=1 DENO_DIR=/home/botuser/.cache/deno \
    && EJS=$(find /usr/local/lib/python3.12/site-packages/yt_dlp \
                  -name "main.js" -path "*ejs*" 2>/dev/null | head -1) \
    && { [ -n "$EJS" ] || { \
           echo "[deno-warmup] FATAL: yt-dlp-ejs main.js not found under" \
                "/usr/local/lib/python3.12/site-packages/yt_dlp/*ejs*." \
                "Verify that yt-dlp[default] is listed in requirements.txt" \
                "and that the builder COPY step transferred packages correctly."; \
           exit 1; }; } \
    && echo "[deno-warmup] Found yt-dlp-ejs at: $EJS" \
    && echo "[deno-warmup] Pre-compiling through Deno (timeout 30 s) ..." \
    && timeout 30 \
         deno run --allow-all "$EJS" \
         </dev/null >/dev/null 2>/dev/null \
    || { CODE=$?; \
         if [ "$CODE" -eq 124 ]; then \
           echo "[deno-warmup] Deno terminated by timeout; this is expected when" \
                "yt-dlp-ejs blocks after EOF — the V8 JIT cache is already written."; \
         else \
           echo "[deno-warmup] FATAL: deno exited with unexpected code $CODE."; \
           exit 1; \
         fi; } \
    && echo "[deno-warmup] Cache written to $DENO_DIR — runtime Deno start will be fast."

# Render injects $PORT at runtime; 8080 is the local development fallback.
EXPOSE 8080

# Health check — validates that the FastAPI health server is responding.
# start-period=60s gives pytgcalls time to complete its async startup.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" \
    || exit 1

# -u: unbuffered stdout/stderr so log lines appear in Render dashboard immediately.
CMD ["python", "-u", "main.py"]
