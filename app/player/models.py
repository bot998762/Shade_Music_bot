"""
app.player.models
~~~~~~~~~~~~~~~~~
Core data models for the music player.

Track is intentionally a plain dataclass (no ORM, no DB coupling) so it
can flow freely between the queue, engine, and handlers without import
cycles.  Database persistence (Phase 2+) will serialise/deserialise Tracks
without changing this model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.utils.helpers import format_duration


@dataclass
class Track:
    """
    Represents a single queued or playing audio track.

    ``webpage_url`` is the canonical, permanent YouTube URL and is the only
    URL we ever store.  The direct-audio URL (fetched fresh before each play)
    is never stored here; it is resolved on demand in YouTubeService.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    title: str
    duration: int                     # seconds; 0 if unknown (live streams)
    webpage_url: str                  # permanent page URL — safe to store

    # ── Metadata ──────────────────────────────────────────────────────────────
    uploader: str
    thumbnail: Optional[str] = None

    # ── Request context ───────────────────────────────────────────────────────
    requested_by_id: int = 0
    requested_by_name: str = "Unknown"
    added_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def formatted_duration(self) -> str:
        """Human-readable duration string, e.g. ``3:45`` or ``1:02:30``."""
        return format_duration(self.duration)

    @property
    def is_live(self) -> bool:
        """True for live streams where duration is unknown."""
        return self.duration == 0

    def __str__(self) -> str:
        dur = "LIVE" if self.is_live else self.formatted_duration
        return f"{self.title} [{dur}]"
