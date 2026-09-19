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

# ── Deno wrapper: strip --no-code-cache ───────────────────────────────────────
# yt-dlp hardcodes --no-code-cache in its Deno invocation (yt_dlp/jsinterp/_deno.py).
# This flag disables the V8 code cache, forcing a full JIT compile on every run.
# On Render free tier (throttled CPU), that cold JIT takes ~60-80 s and peaks at
# ~264 MB RSS — exceeding the 512 MB container limit when combined with the rest
# of the bot.
#
# Fix: rename the real Deno binary to deno.real and install a thin Python wrapper
# at /usr/local/bin/deno that strips ONLY --no-code-cache before forwarding
# every other argument unchanged via os.execv.
#
# os.execv guarantees:
#   • The wrapper process is REPLACED by deno.real — same PID, same process group.
#     yt-dlp's killpg() on timeout correctly kills deno.real, not just the wrapper.
#   • File descriptors 0/1/2 (stdin/stdout/stderr) are inherited unchanged.
#     The JSON IPC protocol between yt-dlp and yt-dlp-ejs is unaffected.
#   • deno.real's exit code becomes the wrapper's exit code — yt-dlp sees the
#     correct success/failure status.
#   • All Deno security flags (--no-remote, --no-local-npm, --no-prompt) are
#     passed through unchanged. Only --no-code-cache is removed.
#
# With the wrapper in place, Deno can write and read the V8 code cache stored at
# /home/botuser/.cache/deno/v8_code_cache_v[N]/ — the same DENO_DIR used by the
# runtime process. After one warm run, subsequent runs skip JIT compilation and
# peak at ~80-130 MB instead of ~264 MB. The pre-warm below (using deno run
# instead of deno cache) populates this cache at image build time so that even
# the first /play after a deploy is warm.
#
# Reverting: remove the wrapper, rename deno.real back to deno. No other changes.
RUN mv /usr/local/bin/deno /usr/local/bin/deno.real \
    && printf '#!/usr/bin/env python3\nimport sys, os\nargs = [a for a in sys.argv[1:] if a != "--no-code-cache"]\nos.execv("/usr/local/bin/deno.real", ["/usr/local/bin/deno.real"] + args)\n' \
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

# ── Deno/yt-dlp-ejs V8 code-cache pre-warm ───────────────────────────────────
# Purpose
# -------
# yt-dlp ≥ 2025.11.12 spawns Deno to run yt-dlp-ejs for YouTube n-signature
# deobfuscation.  Without pre-warming, the first Deno invocation at runtime
# JIT-compiles yt-dlp-ejs.js from scratch.  On Render free tier (throttled CPU,
# 512 MB RAM) this cold JIT peaks at ~264 MB RSS — exceeding the memory limit
# when combined with the rest of the bot.
#
# What this pre-warm does
# -----------------------
# Runs `deno run <ejs_file>` through the wrapper (which strips --no-code-cache).
# deno.real writes the compiled bytecode to:
#   /home/botuser/.cache/deno/v8_code_cache_v[N]/
# At runtime every `deno run <same_ejs_file>` finds the cache, skips JIT
# compilation, and starts at ~80-130 MB instead of ~264 MB.
#
# Why `deno run`, not `deno cache`
# ----------------------------------
# `deno cache` populates gen/ (module bytecode) but NOT v8_code_cache_v[N]/.
# `deno run`   populates BOTH.  v8_code_cache_v[N]/ is what reduces RSS.
# Previous pre-warm used `deno cache` — that was insufficient.
#
# Why stdin=/dev/null + timeout is safe
# ---------------------------------------
# yt-dlp-ejs blocks on stdin waiting for a JSON challenge from yt-dlp.
# Running it alone makes it hang, but V8 compilation completes before any
# stdin read.  `timeout 30` kills deno after 30 s; the cache is already
# written.  Exit code 124 (timeout) is treated as success.
#
# Cache key guarantee
# --------------------
# Deno keys v8_code_cache_v[N]/ by (absolute path + content hash + V8 version).
# Pre-warm and runtime use identical yt-dlp-ejs installation → cache always hits.
# If yt-dlp-ejs updates, hash changes → cache misses → cold JIT once → new entry.
#
# Non-fatal
# ---------
# If yt-dlp-ejs is absent, this warns and continues.  First /play will be slow
# but the build does not fail.
#
# Must run as botuser so cache lands in /home/botuser/.cache/deno — same DENO_DIR
# the bot process uses at runtime.  Must be after COPY steps.
RUN export DENO_NO_UPDATE_CHECK=1 DENO_DIR=/home/botuser/.cache/deno \
    && echo "[deno-warmup] Locating yt-dlp-ejs JS file via importlib.metadata ..." \
    && if EJS=$(python3 -c " \
import importlib.metadata as M, sys; \
try: \
    d = M.distribution('yt-dlp-ejs'); \
    js = [str(d.locate_file(f)) for f in (d.files or []) if str(f).endswith('.js')]; \
    print(js[0]) if js else sys.exit(1) \
except M.PackageNotFoundError: \
    sys.exit(2) \
" 2>/dev/null); then \
           echo "[deno-warmup] Found yt-dlp-ejs JS file at: $EJS"; \
           echo "[deno-warmup] Pre-warming V8 code cache via deno run (stdin=/dev/null, timeout 30s) ..."; \
           timeout 30 deno run "$EJS" </dev/null >/dev/null 2>&1; \
           CODE=$?; \
           if [ "$CODE" -eq 0 ] || [ "$CODE" -eq 124 ]; then \
               echo "[deno-warmup] V8 code cache written to $DENO_DIR — runtime Deno start will be fast."; \
           else \
               echo "[deno-warmup] WARNING: deno run exited $CODE (non-fatal — first /play may be slow)."; \
           fi; \
       else \
           echo "[deno-warmup] WARNING: yt-dlp-ejs JS file not found via importlib.metadata."; \
           echo "[deno-warmup] Non-fatal: first /play will cold-start Deno."; \
           echo "[deno-warmup] Skipping Deno pre-warm."; \
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
