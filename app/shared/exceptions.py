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
