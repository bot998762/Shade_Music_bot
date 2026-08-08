"""
app.playback.state
~~~~~~~~~~~~~~~~~~
Per-chat state machine — the single source of truth for what is playing.

StateManager owns the PlaybackState for every chat that has ever been
active.  It is the ONLY place PlaybackStatus is mutated.  All other
modules read state via StateManager and request changes through it,
never writing directly to PlaybackState fields.

This single ownership rule means:
  * No hidden state scattered across modules.
  * Phase 1 (pause/resume) adds new transitions here without touching
    the controller, session, or cleanup modules.
  * Tests need only check StateManager to verify correctness.
"""

from __future__ import annotations

from typing import Dict, Optional

from app.infrastructure.logger import logger
from app.playback.models import PlaybackState, PlaybackStatus
from app.search.models import Track


class StateManager:
    """
    Owns and mutates the PlaybackState for every active chat.

    All methods are synchronous — state transitions are fast dict operations
    that do not need to be awaited.  Callers are always on the event loop.
    """

    def __init__(self) -> None:
        self._states: Dict[int, PlaybackState] = {}

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, chat_id: int) -> PlaybackState:
        """Return the PlaybackState for *chat_id*, creating IDLE if absent."""
        if chat_id not in self._states:
            self._states[chat_id] = PlaybackState(chat_id=chat_id)
        return self._states[chat_id]

    def is_idle(self, chat_id: int) -> bool:
        return self.get(chat_id).is_idle

    def is_playing(self, chat_id: int) -> bool:
        return self.get(chat_id).is_playing

    def current_track(self, chat_id: int) -> Optional[Track]:
        return self.get(chat_id).current_track

    # ── Write (transitions) ───────────────────────────────────────────────────

    def transition_to_playing(self, chat_id: int, track: Track) -> None:
        """
        Transition *chat_id* to PLAYING with *track* as the current track.
        Called by PlaybackController after a successful VC join.
        """
        state = self.get(chat_id)
        state.set_playing(track)
        logger.debug(
            "[STATE] {} → PLAYING  track='{}'",
            chat_id, track.title,
        )

    def transition_to_idle(self, chat_id: int) -> None:
        """
        Transition *chat_id* back to IDLE.
        Called by CleanupService after leaving the voice chat.
        """
        state = self.get(chat_id)
        prev_track = state.current_track
        state.set_idle()
        logger.debug(
            "[STATE] {} → IDLE  (was playing '{}')",
            chat_id,
            prev_track.title if prev_track else "nothing",
        )

    def update_current_track(self, chat_id: int, track: Optional[Track]) -> None:
        """
        Replace the current track without changing status.
        Used by the monitor when auto-advancing to the next track.
        """
        state = self.get(chat_id)
        state.current_track = track
        if track:
            logger.debug(
                "[STATE] {} current_track updated → '{}'",
                chat_id, track.title,
            )
