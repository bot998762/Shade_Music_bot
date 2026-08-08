"""
app.shared.constants
~~~~~~~~~~~~~~~~~~~~
Single source of truth for every magic number and string in the project.

Future phases add constants here — never inline them in business logic.
"""

from __future__ import annotations

# ── Bot identity ───────────────────────────────────────────────────────────────
BOT_NAME    = "ShadeMusicBot"
BOT_VERSION = "1.0.0"
BOT_PHASE   = "Phase 1"

# ── Rate limiting ──────────────────────────────────────────────────────────────
PLAY_COOLDOWN_SECONDS: float = 3.0

# ── Queue ──────────────────────────────────────────────────────────────────────
DEFAULT_MAX_QUEUE_SIZE: int = 50
DEFAULT_VOLUME:         int = 100

# ── Timeouts (seconds) ────────────────────────────────────────────────────────
SEARCH_TIMEOUT_SEC:         int = 30
STREAM_RESOLVE_TIMEOUT_SEC: int = 45

# ── Thread executor ───────────────────────────────────────────────────────────
YT_EXECUTOR_WORKERS:    int = 3
YT_EXECUTOR_NAME:       str = "ytdlp"

# ── Stream-end retry ──────────────────────────────────────────────────────────
MAX_SKIP_RETRIES: int = 3

# ── Cookies ───────────────────────────────────────────────────────────────────
COOKIES_TMP_DIR:     str = "/tmp"
COOKIES_SECRETS_DIR: str = "/etc/secrets"

# ── FFmpeg input flags passed to ntgcalls via MediaStream.ffmpeg_parameters ───
#
# These are prepended before "-i <url>" in the ffmpeg invocation that ntgcalls
# runs internally.  They target two distinct stutter causes:
#
# reconnect / reconnect_streamed / reconnect_delay_max
#   googlevideo.com CDN connections drop at DASH segment boundaries (~5 s
#   intervals) and under transient network pressure on Render's shared
#   infrastructure.  Without reconnect flags, FFmpeg exits on the first
#   connection drop; ntgcalls fires StreamAudioEnded; PyTgCalls advances the
#   queue — the user hears a sudden cut or unexpected track skip.
#   reconnect=1 + reconnect_streamed=1 tell FFmpeg to retry the HTTP
#   connection in-place.  reconnect_delay_max=5 caps the back-off at 5 s
#   so a temporary CDN hiccup recovers within one DASH segment window.
#
# buffer_size
#   avformat's network read buffer.  Default is 32 KB, which at 160 kbps
#   (opus/webm, the highest-priority audio-only format from mweb) gives
#   roughly 1.6 s of headroom before the decode pipeline starves.  On
#   Render's shared network a momentary slowdown easily exceeds that window,
#   producing the characteristic brief stutter without a full disconnect.
#   8 MB gives ~40 s of headroom at 160 kbps.  The buffer fills
#   opportunistically when the CDN is fast and does NOT increase perceived
#   start latency — ntgcalls opens the pipe and starts consuming audio
#   frames as soon as FFmpeg produces them, which happens after the first
#   HTTP response regardless of buffer_size.
#
# Verified against ntgcalls 2.2.5 source: ffmpeg_parameters is forwarded
# verbatim as input flags in the ffmpeg invocation.  These are standard
# libavformat HTTP options; no ntgcalls-specific behaviour required.
FFMPEG_INPUT_FLAGS: str = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5 "
    "-buffer_size 8192k"
)
