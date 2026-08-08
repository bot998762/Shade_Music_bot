"""
app.playback.models
~~~~~~~~~~~~~~~~~~~
Playback-layer data models.

PlaybackStatus  — enumeration of possible playback states.
PlaybackState   — snapshot of one chat's current playback situation.

These are distinct from search.models.Track (which describes the audio content)
and from session.py (which manages the queue).  Together they give a full
view of what is happening in a chat at any moment.

Future phases add PAUSED, SEEKING, BUFFERING to PlaybackStatus without
touching any other file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Track is owned by the search layer (Track creation is a search responsibility).
# Importing it here does NOT create a circular dependency because:
#   search/models.py → shared only
#   playback/models.py → search/models (one direction)
#   playback/controller.py → playback/models + search/models (no cycle)
from app.search.models import Track


class PlaybackStatus(Enum):
    """Possible playback states for a single chat session."""
    IDLE    = "idle"      # No VC, no current track
    PLAYING = "playing"   # Actively streaming audio
    PAUSED  = "paused"    # Phase 1+ (stream paused, VC still joined)


@dataclass
class PlaybackState:
    """
    Snapshot of one chat's current playback situation.

    Stored per chat_id in the session layer.
    Read by the controller and handlers to make routing decisions.
    """
    chat_id:       int
    status:        PlaybackStatus      = PlaybackStatus.IDLE
    current_track: Optional[Track]     = None

    @property
    def is_idle(self) -> bool:
        return self.status == PlaybackStatus.IDLE

    @property
    def is_playing(self) -> bool:
        return self.status == PlaybackStatus.PLAYING

    def set_playing(self, track: Track) -> None:
        self.status        = PlaybackStatus.PLAYING
        self.current_track = track

    def set_idle(self) -> None:
        self.status        = PlaybackStatus.IDLE
        self.current_track = None
