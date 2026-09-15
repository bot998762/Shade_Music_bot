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

# ── Deno/yt-dlp-ejs V8 cache pre-warm ────────────────────────────────────────
# Context
# -------
# yt-dlp ≥ 2025.11.12 can invoke Deno to run the yt-dlp-ejs JavaScript bundle
# for YouTube n-signature deobfuscation.  On Render ephemeral containers the
# Deno module cache (~/.cache/deno) is empty on every cold start.  If Deno is
# ever invoked, the first run JIT-compiles yt-dlp-ejs from scratch, taking
# > 30 s on a throttled 512 MB instance.
#
# Current player client: tv_embedded
# ------------------------------------
# The resolver uses ONLY the tv_embedded (TVHTML5_SIMPLY_EMBEDDED_PLAYER)
# client, which does NOT require YouTube PO tokens.  As of yt-dlp ≥ 2026.07.04,
# Deno is therefore NOT invoked on the normal /play path.  The pre-warm is a
# defensive measure for the case that yt-dlp changes this behaviour in a future
# version, or that n-signature computation falls back to Deno.
#
# Previous failure
# ----------------
# The previous implementation searched for "main.js" under
# .../site-packages/yt_dlp/ — the yt-dlp package directory.  This was wrong:
# yt-dlp-ejs is a SEPARATE PyPI package that installs as yt_dlp_ejs/ at the
# top level of site-packages, NOT inside the yt_dlp/ directory.  Additionally,
# the JS file is not named "main.js" — the filename varies by version.
#
# Correct discovery mechanism
# ---------------------------
# Python importlib.metadata is used to locate the JS file, exactly mirroring
# how yt-dlp discovers it at runtime.  This is version-agnostic and resilient
# to any future rename of the JS asset.
#
# `deno cache` vs `deno run`
# --------------------------
# `deno cache <file>` compiles the module into V8 bytecode WITHOUT executing it.
# This is preferable to `deno run` because:
#   • No IPC/stdin contract needed (yt-dlp-ejs expects a specific stdin protocol
#     when run by yt-dlp; running it standalone fails or blocks).
#   • `deno cache` exits cleanly with code 0 on success.
#   • The V8 bytecode cache written by `deno cache` is reused by `deno run`
#     — same cache keys, same cache directory.
#
# Non-fatal
# ---------
# If yt-dlp-ejs is not installed or has no .js files, this step warns and
# continues.  The bot operates correctly without the pre-warm because
# tv_embedded does not use Deno for PO tokens on the current /play path.
# A fatal exit here would block deployment for a non-mandatory optimisation.
#
# Must run as botuser (same uid as runtime) so the cache writes to
# /home/botuser/.cache/deno — the DENO_DIR Deno uses at runtime.
# Must be after the COPY steps so yt-dlp-ejs is already on disk.
RUN export DENO_NO_UPDATE_CHECK=1 DENO_DIR=/home/botuser/.cache/deno \
    && echo "[deno-warmup] Locating yt-dlp-ejs JS file via importlib.metadata ..." \
    && EJS=$(python3 -c " \
import importlib.metadata as M, sys; \
try: \
    d = M.distribution('yt-dlp-ejs'); \
    js = [str(d.locate_file(f)) for f in (d.files or []) if str(f).endswith('.js')]; \
    print(js[0]) if js else sys.exit(1) \
except M.PackageNotFoundError: \
    sys.exit(2) \
" 2>/dev/null) \
    && if [ -z "$EJS" ]; then \
           echo "[deno-warmup] WARNING: yt-dlp-ejs JS file not found via importlib.metadata."; \
           echo "[deno-warmup] This is non-fatal: the tv_embedded player client does not"; \
           echo "[deno-warmup] invoke Deno for PO tokens on the current /play path."; \
           echo "[deno-warmup] Skipping Deno pre-warm."; \
       else \
           echo "[deno-warmup] Found yt-dlp-ejs JS file at: $EJS"; \
           echo "[deno-warmup] Pre-compiling via 'deno cache' (no execution, just V8 JIT) ..."; \
           deno cache "$EJS" 2>&1 && \
             echo "[deno-warmup] V8 cache written to $DENO_DIR — Deno start will be fast." || \
             echo "[deno-warmup] WARNING: deno cache failed (non-fatal — tv_embedded does not use Deno)."; \
       fi

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
