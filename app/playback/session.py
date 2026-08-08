"""
app.playback.session
~~~~~~~~~~~~~~~~~~~~
Per-chat session and queue management.

SessionManager maintains the upcoming track queue for every chat.
It is intentionally separate from StateManager (which holds playback status)
so that Phase 2 can add MongoDB persistence to this module without touching
the state machine.

Design
------
* FIFO ordering — first added, first played.
* One asyncio.Lock per chat — operations on different chats never block each
  other.
* Stateless storage — swap the deque for a Motor cursor later to add
  persistence without changing the public interface.
* All public methods are coroutines for a stable interface, even though the
  current implementation is synchronous under the lock.

Phase 1+ extensions (no redesign required)
------------------------------------------
  shuffle()   — reorder the deque
  move()      — swap positions
  remove()    — remove a specific track by index
  peek_next() — inspect without consuming
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Deque, Dict, List, Optional

from app.search.models import Track


class SessionManager:
    """
    Manages per-chat music queues entirely in memory.

    One instance shared across the whole application (injected into
    PlaybackController by the bootstrap layer).
    """

    def __init__(self) -> None:
        self._queues: Dict[int, Deque[Track]] = {}
        self._locks:  Dict[int, asyncio.Lock] = {}

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

    async def enqueue(self, chat_id: int, track: Track) -> int:
        """
        Append *track* to the tail of the queue.

        Returns the queue length after insertion (1-based position).
        """
        async with self._lock_for(chat_id):
            q = self._queue_for(chat_id)
            q.append(track)
            return len(q)

    async def dequeue(self, chat_id: int) -> Optional[Track]:
        """
        Remove and return the head of the queue.
        Returns None when the queue is empty.
        """
        async with self._lock_for(chat_id):
            q = self._queue_for(chat_id)
            if not q:
                return None
            return q.popleft()

    async def requeue_head(self, chat_id: int, track: Track) -> None:
        """
        Insert *track* at the HEAD of the queue.

        Used by PlaybackController to restore a track when a VC join fails,
        so the track is not permanently lost.
        """
        async with self._lock_for(chat_id):
            self._queue_for(chat_id).appendleft(track)

    async def clear(self, chat_id: int) -> None:
        """Discard all upcoming tracks for *chat_id*."""
        async with self._lock_for(chat_id):
            self._queue_for(chat_id).clear()

    # ── Read operations ───────────────────────────────────────────────────────

    async def get_upcoming(self, chat_id: int) -> List[Track]:
        """Return a snapshot of the upcoming queue (does not modify it)."""
        async with self._lock_for(chat_id):
            return list(self._queue_for(chat_id))

    async def is_empty(self, chat_id: int) -> bool:
        """Return True when there are no upcoming tracks."""
        async with self._lock_for(chat_id):
            return len(self._queue_for(chat_id)) == 0

    async def size(self, chat_id: int) -> int:
        """Return the number of upcoming (not-yet-played) tracks."""
        async with self._lock_for(chat_id):
            return len(self._queue_for(chat_id))
