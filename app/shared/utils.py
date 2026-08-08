"""
app.shared.utils
~~~~~~~~~~~~~~~~
Reusable utility functions shared across all modules.

Rules
-----
* No imports from any other app module (this is the base layer).
* Pure functions only — no side effects, no state.
* If a helper is only relevant to one domain, put it there instead.
"""

from __future__ import annotations

import re
import time
from datetime import timedelta
from typing import Optional


def format_duration(seconds: int) -> str:
    """
    Convert a duration in seconds to a human-readable string.

    Examples
    --------
    >>> format_duration(90)
    '1:30'
    >>> format_duration(3661)
    '1:01:01'
    >>> format_duration(0)
    '0:00'
    """
    td = timedelta(seconds=max(0, seconds))
    total = int(td.total_seconds())
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def truncate(text: str, max_length: int = 50, suffix: str = "…") -> str:
    """
    Truncate *text* to *max_length* characters, appending *suffix* if cut.

    Examples
    --------
    >>> truncate("Hello World", 8)
    'Hello W…'
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def sanitise_filename(name: str) -> str:
    """
    Remove characters that are unsafe in filesystem paths.

    Replaces any character that is not alphanumeric, space, dash, or dot
    with an underscore.
    """
    return re.sub(r"[^\w\s\-.]", "_", name).strip()


def parse_time_to_seconds(time_str: str) -> Optional[int]:
    """
    Parse a time string like ``1:30`` or ``90`` into total seconds.

    Returns None if the format is not recognised.
    """
    time_str = time_str.strip()

    if time_str.isdigit():
        return int(time_str)

    parts = time_str.split(":")
    try:
        parts_int = [int(p) for p in parts]
    except ValueError:
        return None

    if len(parts_int) == 2:
        return parts_int[0] * 60 + parts_int[1]
    if len(parts_int) == 3:
        return parts_int[0] * 3600 + parts_int[1] * 60 + parts_int[2]
    return None


def mention_html(user_id: int, first_name: str) -> str:
    """Return a Telegram HTML mention link for a user."""
    return f'<a href="tg://user?id={user_id}">{first_name}</a>'


def format_uptime(start_time: float) -> str:
    """
    Format a monotonic start time as a human-readable uptime string.

    Parameters
    ----------
    start_time:
        Value from ``time.monotonic()`` recorded at process start.
    """
    seconds = int(time.monotonic() - start_time)
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)
