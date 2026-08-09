"""
app.shared.validators
~~~~~~~~~~~~~~~~~~~~~
Pure input validation functions.

All validators return (is_valid: bool, reason: str).
Callers decide what to do with an invalid result — validators never raise.

No imports from other app modules.
"""

from __future__ import annotations

from typing import Tuple


def validate_play_query(query: str) -> Tuple[bool, str]:
    """
    Validate a /play search query.

    Returns (True, "") on success.
    Returns (False, reason) on failure.
    """
    if not query or not query.strip():
        return False, "Query is empty."
    if len(query) > 500:
        return False, "Query is too long (max 500 characters)."
    return True, ""


def validate_chat_id(chat_id: int) -> Tuple[bool, str]:
    """Validate that a chat_id is a non-zero integer."""
    if not isinstance(chat_id, int) or chat_id == 0:
        return False, f"Invalid chat_id: {chat_id!r}"
    return True, ""


def validate_user_id(user_id: int) -> Tuple[bool, str]:
    """Validate that a user_id is a positive integer."""
    if not isinstance(user_id, int) or user_id <= 0:
        return False, f"Invalid user_id: {user_id!r}"
    return True, ""


def is_direct_url(query: str) -> bool:
    """
    Return True when the query is a direct URL rather than a search term.

    Detects http:// and https:// URLs.  Does not validate URL correctness —
    the yt-dlp resolver will report extraction errors on invalid URLs.
    """
    return query.startswith(("http://", "https://"))


def is_playlist_url(url: str) -> bool:
    """
    Return True when the URL is a pure playlist rather than a single video.

    Rejects YouTube /playlist? URLs and any URL that has a list= parameter
    without a v= parameter (or youtu.be/ single-video shortlink).

    Phase-1 guard only — playlist support is intentionally deferred to Phase 3.
    """
    lower = url.lower()
    # Explicit playlist endpoint
    if "youtube.com/playlist" in lower:
        return True
    # Playlist parameter without a specific video
    if "list=" in lower and "v=" not in lower and "youtu.be/" not in lower:
        return True
    return False


def normalise_query(query: str) -> str:
    """
    Normalise a search query for consistent processing.

    * Strips leading/trailing whitespace.
    * Collapses multiple internal spaces to one.
    """
    return " ".join(query.split())
