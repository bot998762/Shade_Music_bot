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
SEARCH_TIMEOUT_SEC:         int = 20
STREAM_RESOLVE_TIMEOUT_SEC: int = 25

# ── Thread executor ───────────────────────────────────────────────────────────
YT_EXECUTOR_WORKERS:    int = 3
YT_EXECUTOR_NAME:       str = "ytdlp"

# ── Stream-end retry ──────────────────────────────────────────────────────────
MAX_SKIP_RETRIES: int = 3

# ── Cookies ───────────────────────────────────────────────────────────────────
COOKIES_TMP_DIR: str = "/tmp"
COOKIES_SECRETS_DIR: str = "/etc/secrets"
