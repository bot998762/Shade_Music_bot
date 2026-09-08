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
├── StreamResolveError          ← yt-dlp could not produce a stream URL
│   └── StreamResolveTimeoutError  ← resolver timed out; subprocess killed
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
    """Raised when a search returns zero results or metadata fetch fails."""


# ── Stream resolution ─────────────────────────────────────────────────────────

class StreamResolveError(ShadeBotError):
    """
    Raised when yt-dlp cannot extract a playable stream URL.

    Distinct from NoResultsError — the track was found (metadata exists)
    but the CDN audio URL could not be obtained.  The video may be
    unavailable, age-restricted, geo-blocked, or affected by a yt-dlp
    API change.

    Handlers should show "stream unavailable" — NOT "no results found".
    """


class StreamResolveTimeoutError(StreamResolveError):
    """
    Raised when the yt-dlp subprocess exceeds STREAM_RESOLVE_TIMEOUT_SEC.

    The subprocess (yt-dlp + its Deno child) is killed via SIGTERM to
    the process group before this exception is raised.  No ghost processes
    remain after the exception is caught.

    This is a subclass of StreamResolveError so callers that catch
    StreamResolveError also catch this.  Callers that need to distinguish
    the timeout case (e.g. to show "try again" vs "video unavailable")
    must catch StreamResolveTimeoutError FIRST (more specific → less specific).
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
