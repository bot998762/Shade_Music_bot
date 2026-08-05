"""
app.player.queue
~~~~~~~~~~~~~~~~
In-memory, per-chat queue manager.

Design goals
------------
* FIFO ordering — first added, first played.
* One asyncio.Lock per chat — operations on different chats never block
  each other.
* ``current`` track stored separately from the upcoming queue so callers
  always know what is playing right now.
* Fully async-safe — safe to call from any coroutine on the same event loop.
* Stateless storage — designed so Phase 2+ can add MongoDB persistence by
  swapping only this module, without touching engine or handlers.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque, Dict, List, Optional

from app.player.models import Track


class QueueManager:
    """
    Manages per-chat music queues entirely in memory.

    All public methods are coroutines so callers can ``await`` them
    consistently, even though the current implementation is synchronous
    under the lock.  This keeps the interface stable if persistence is
    added later.
    """

    def __init__(self) -> None:
        # upcoming tracks (not yet played)
        self._queues: Dict[int, Deque[Track]] = {}
        # track that is currently streaming
        self._current: Dict[int, Optional[Track]] = {}
        # one lock per chat — operations on different chats are independent
        self._locks: Dict[int, asyncio.Lock] = {}

    # ── Private helpers ───────────────────────────────────────────────────────

    def _queue_for(self, chat_id: int) -> Deque[Track]:
        if chat_id not in self._queues:
            self._queues[chat_id] = deque()
        return self._queues[chat_id]

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    # ── Write operations ──────────────────────────────────────────────────────

    async def add(self, chat_id: int, track: Track) -> int:
        """
        Append *track* to the tail of the queue.

        Returns the queue length **after** insertion (i.e. the track's
        1-based position in the upcoming list).
        """
        async with self._lock_for(chat_id):
            q = self._queue_for(chat_id)
            q.append(track)
            return len(q)

    async def pop_next(self, chat_id: int) -> Optional[Track]:
        """
        Remove and return the head of the upcoming queue, setting it as
        the current track.  Returns ``None`` when the queue is empty.
        """
        async with self._lock_for(chat_id):
            q = self._queue_for(chat_id)
            if not q:
                self._current[chat_id] = None
                return None
            track = q.popleft()
            self._current[chat_id] = track
            return track

    def set_current(self, chat_id: int, track: Optional[Track]) -> None:
        """Overwrite the current-track slot (used when starting playback)."""
        self._current[chat_id] = track

    async def prepend(self, chat_id: int, track: Track) -> None:
        """
        Insert *track* at the HEAD of the upcoming queue.

        Used by MusicEngine._start_next() to restore a track when a VC join
        fails, so the track is not permanently lost.
        """
        async with self._lock_for(chat_id):
            q = self._queue_for(chat_id)
            q.appendleft(track)

    async def clear(self, chat_id: int) -> None:
        """Discard all upcoming tracks and clear the current-track slot."""
        async with self._lock_for(chat_id):
            self._queue_for(chat_id).clear()
            self._current[chat_id] = None

    # ── Read operations ───────────────────────────────────────────────────────

    def get_current(self, chat_id: int) -> Optional[Track]:
        """Return the track that is currently streaming (or ``None``)."""
        return self._current.get(chat_id)

    async def get_upcoming(self, chat_id: int) -> List[Track]:
        """Return a snapshot of the upcoming queue (does not modify it)."""
        async with self._lock_for(chat_id):
            return list(self._queue_for(chat_id))

    async def is_empty(self, chat_id: int) -> bool:
        """Return ``True`` when there are no upcoming tracks."""
        async with self._lock_for(chat_id):
            return len(self._queue_for(chat_id)) == 0

    async def size(self, chat_id: int) -> int:
        """Return the number of upcoming (not-yet-played) tracks."""
        async with self._lock_for(chat_id):
            return len(self._queue_for(chat_id))

    def has_active_session(self, chat_id: int) -> bool:
        """
        Return ``True`` when the chat has either a current track or upcoming
        tracks — i.e. the bot is considered active in this chat.
        """
        current = self._current.get(chat_id)
        upcoming = self._queues.get(chat_id)
        return current is not None or bool(upcoming)
