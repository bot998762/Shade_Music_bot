"""
app.shared.exceptions
~~~~~~~~~~~~~~~~~~~~~
Custom exception hierarchy for ShadeMusicBot.

Every exception that crosses a module boundary should be typed here so
callers can catch specific errors rather than bare ``Exception``.

Hierarchy
---------
ShadeBotError
├── SearchError
│   └── NoResultsError
├── StreamResolveError
│   └── StreamResolveTimeoutError   ← Phase-1 OOM fix: timeout ≠ failure
├── VoiceChatError
│   ├── NoActiveVoiceChatError
│   └── PrivateGroupError
├── QueueFullError
└── ValidationError
"""

from __future__ import annotations


class ShadeBotError(Exception):
    """Base class for all ShadeMusicBot errors."""


# ── Search ────────────────────────────────────────────────────────────────────

class SearchError(ShadeBotError):
    """Raised when YouTube search fails unexpectedly."""


class NoResultsError(SearchError):
    """Raised when a search returns zero results."""


# ── Stream resolution ─────────────────────────────────────────────────────────

class StreamResolveError(ShadeBotError):
    """Raised when a direct audio CDN URL cannot be resolved."""


class StreamResolveTimeoutError(StreamResolveError):
    """
    Raised when resolver.resolve() times out via asyncio.TimeoutError.

    CRITICAL DISTINCTION (Phase-1 OOM fix):
    This is NOT the same as a DownloadError / extraction failure.

    - DownloadError  → fallback to FFmpegStreamBuilder.build_from_youtube() is ALLOWED.
    - TimeoutError   → fallback is FORBIDDEN.

    Why: When asyncio.wait_for() cancels the Future, the underlying
    ThreadPoolExecutor thread continues running yt-dlp + Deno.  If the
    controller immediately falls back to build_from_youtube(), ntgcalls
    spawns a second yt-dlp + second Deno + FFmpeg while the ghost thread's
    yt-dlp + Deno are still alive.  On Render's 512 MB limit this causes
    OOM (SIGKILL / exit 137).

    Catching this exception and NOT falling back eliminates the duplicate
    process overlap.  The ghost thread's memory (~90–180 MB) is no longer
    compounded by a second extraction peak (~120–240 MB).
    """


# ── Voice chat ────────────────────────────────────────────────────────────────

class VoiceChatError(ShadeBotError):
    """Raised for general voice-chat failures."""


class NoActiveVoiceChatError(VoiceChatError):
    """Raised when the target group has no active voice chat."""


class PrivateGroupError(VoiceChatError):
    """
    Raised when the assistant cannot auto-join a private group.

    Private groups have no public username, so the assistant cannot be
    invited automatically.  An admin must add @Shade_music_assistant
    to the group manually before /play can be used.
    """


# ── Playback ──────────────────────────────────────────────────────────────────

class QueueFullError(ShadeBotError):
    """Raised when a chat's queue has reached its size limit."""


# ── Input validation ──────────────────────────────────────────────────────────

class ValidationError(ShadeBotError):
    """Raised when user input fails validation checks."""
