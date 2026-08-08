"""
app.search.models
~~~~~~~~~~~~~~~~~
Data models owned by the search layer.

SearchResult — raw metadata returned by YouTubeSearch.
Track        — a fully-formed track ready for the playback queue.
               Track creation is a search responsibility per the architecture spec.

These models are intentionally pure dataclasses: no ORM, no DB coupling.
They flow freely between search, playback, and handlers without import cycles.
Database persistence (future phase) will serialise/deserialise them without
changing this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.shared.utils import format_duration


# ══════════════════════════════════════════════════════════════════════════════
# SearchResult
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SearchResult:
    """
    Raw metadata returned by YouTubeSearch.

    No request context — just what the search engine returned.
    PlaybackController promotes a SearchResult into a Track by adding
    ``requested_by_id`` and ``requested_by_name``.
    """

    title:       str
    duration:    int             # seconds; 0 for live streams
    webpage_url: str             # permanent YouTube watch URL
    uploader:    str
    thumbnail:   Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# Track
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Track:
    """
    A fully-formed track ready for the playback queue.

    ``webpage_url`` is the canonical, permanent YouTube URL — the only URL
    ever stored.  The direct-audio CDN URL (resolved by StreamResolver) is
    never stored here; it is fetched fresh before each play because CDN URLs
    expire in ~6 hours.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    title:       str
    duration:    int             # seconds; 0 for live streams
    webpage_url: str             # permanent page URL — safe to store

    # ── Metadata ──────────────────────────────────────────────────────────────
    uploader:  str
    thumbnail: Optional[str] = None

    # ── Request context ───────────────────────────────────────────────────────
    requested_by_id:   int = 0
    requested_by_name: str = "Unknown"
    added_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def formatted_duration(self) -> str:
        """Human-readable duration, e.g. ``3:45`` or ``1:02:30``."""
        return format_duration(self.duration)

    @property
    def is_live(self) -> bool:
        """True for live streams where duration is unknown."""
        return self.duration == 0

    def __str__(self) -> str:
        dur = "LIVE" if self.is_live else self.formatted_duration
        return f"{self.title} [{dur}]"

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_search_result(
        cls,
        result: SearchResult,
        requested_by_id:   int,
        requested_by_name: str,
    ) -> "Track":
        """
        Promote a SearchResult into a playable Track.

        Called by PlaybackController after search completes.
        """
        return cls(
            title=result.title,
            duration=result.duration,
            webpage_url=result.webpage_url,
            uploader=result.uploader,
            thumbnail=result.thumbnail,
            requested_by_id=requested_by_id,
            requested_by_name=requested_by_name,
        )
