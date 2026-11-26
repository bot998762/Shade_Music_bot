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
# Maximum UPCOMING tracks per chat.  The currently playing track is held by
# StateManager and is NOT counted: 1 playing + 10 upcoming is allowed.
# MAX_UPCOMING_HARD_CAP is the production ceiling; no configuration value may
# raise the effective cap above it (see clamp_queue_cap()).
MAX_UPCOMING_HARD_CAP:  int = 10
DEFAULT_MAX_QUEUE_SIZE: int = 10


# A stream-end event carries no track/stream identity (see controller.advance).
# A "stream ended" event that arrives within this many seconds of a new stream
# being installed cannot be the natural end of that new track, so it is treated
# as a stale event from the previous stream.  Never applied to VC-closed/kicked.
STALE_END_WINDOW_SEC: float = 3.0


def clamp_queue_cap(value: int) -> int:
    """Return *value* limited to the inclusive range 1..MAX_UPCOMING_HARD_CAP."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_QUEUE_SIZE
    return max(1, min(MAX_UPCOMING_HARD_CAP, v))


DEFAULT_VOLUME:         int = 100

# ── Timeouts (seconds) ────────────────────────────────────────────────────────
SEARCH_TIMEOUT_SEC:         int = 20
STREAM_RESOLVE_TIMEOUT_SEC: int = 90  # Kept at 90 s for Render Standard validation; reduce to 30 s after first successful deployment confirms Deno resolution time

# ── Thread executor ───────────────────────────────────────────────────────────
YT_EXECUTOR_WORKERS:    int = 1
YT_EXECUTOR_NAME:       str = "ytdlp"

# ── Stream-end retry ──────────────────────────────────────────────────────────
MAX_SKIP_RETRIES: int = 3

# ── Cookies ───────────────────────────────────────────────────────────────────
COOKIES_TMP_DIR: str = "/tmp"
COOKIES_SECRETS_DIR: str = "/etc/secrets"
