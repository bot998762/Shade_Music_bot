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
# full YouTube support (n-signature and PO-token challenges).  yt-dlp supports
# multiple runtimes in priority order: Deno > Node.js > PhantomJS > Python jsinterp.
# Deno is the primary runtime used with yt-dlp-ejs for n-sig + PO-token solving.
#
# WHY Deno (and NOT Node.js):
#   Production measurement (2026-09-21) showed Deno PSS ≈ 244 MB during n-sig.
#   This was incompatible with the previous 512 MB Render Starter plan.
#   The plan has been upgraded to Render Standard (2 GB RAM) — Deno now fits
#   with ≈1.5 GB headroom even during advance() FFmpeg overlap.
#   The Node.js path (yt-dlp >= 2025.11.12 YouTube [jsc:] framework) was
#   investigated and DISPROVEN: YouTube n-sig uses the [jsc:] framework which
#   only has DenoJSI backend; no Node.js backend exists in this version.
#
# DENO_INSTALL=/usr/local → binary lands at /usr/local/bin/deno (on PATH).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libssl3 \
        ca-certificates \
        curl \
        unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

# ── Deno wrapper: strip --no-code-cache ────────────────────────────────────────
# yt-dlp invokes Deno with --no-code-cache which prevents V8 from writing
# compiled JavaScript to disk.  On a fresh container this forces Deno to
# JIT-compile yt-dlp-ejs from scratch on every invocation, adding 10-25 s
# to the first resolve and increasing peak memory during compilation.
#
# The wrapper intercepts the yt-dlp Deno invocation and removes that one flag
# before forwarding to the real Deno binary.  All other flags, stdin/stdout/
# stderr file descriptors, environment variables, and the process group are
# passed through unchanged.  exec() replaces the wrapper process — PID,
# exit code, signal handling, and process-group membership are all preserved.
# The real Deno binary is at /usr/local/bin/deno.real.
RUN mv /usr/local/bin/deno /usr/local/bin/deno.real \
    && printf '#!/bin/sh\nexec /usr/local/bin/deno.real $(echo "$@" | sed "s/--no-code-cache//g")\n' \
       > /usr/local/bin/deno \
    && chmod +x /usr/local/bin/deno

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

# ── Deno/yt-dlp-ejs V8 code-cache pre-warm ────────────────────────────────────
# On first invocation, Deno JIT-compiles yt-dlp-ejs and caches the result.
# Running this during the Docker build populates the V8 code cache so that
# the first /play on a cold container does not pay the full JIT cost.
#
# The wrapper passes yt-dlp-ejs to deno.real without --no-code-cache, so
# the cache written here will actually be used at runtime.
#
# timeout 60: if the pre-warm takes more than 60 s (e.g. very constrained
# build environment), fail gracefully rather than hanging the build.
# The bot still works without the cache — the first resolve is just slower.
RUN python3 -c "
import importlib.metadata, pathlib, sys
try:
    dist = importlib.metadata.distribution('yt-dlp-ejs')
    ejs_path = pathlib.Path(list(dist.files)[0].locate()).resolve()
    print(f'yt-dlp-ejs found: {ejs_path}', file=sys.stderr)
except Exception as e:
    print(f'yt-dlp-ejs not found: {e}', file=sys.stderr)
    sys.exit(0)
import subprocess, os
result = subprocess.run(
    ['timeout', '60', '/usr/local/bin/deno', 'run', '--ext=js',
     '--no-prompt', '--no-remote', '--no-local-npm', str(ejs_path)],
    input=b'',
    capture_output=True,
    env={**os.environ, 'DENO_NO_UPDATE_CHECK': '1'},
)
print(f'pre-warm exit={result.returncode}', file=sys.stderr)
print(result.stderr.decode(errors=\"replace\")[:500], file=sys.stderr)
" || true

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
