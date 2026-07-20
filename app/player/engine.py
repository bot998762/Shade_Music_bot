"""
app.player.engine
~~~~~~~~~~~~~~~~~
MusicEngine — the heart of Phase 1.

This class is the single authority over playback state.  Handlers call it;
it delegates to QueueManager, YouTubeService, and VoiceChatManager.

Design rules
------------
* No Telegram API calls inside the engine — the engine does not know about
  messages or chats beyond their integer IDs.
* All errors are caught here; callers receive well-typed exceptions or
  ``None`` rather than raw library exceptions.
* ``on_stream_end`` is called by ``VoiceChatManager`` when a track finishes
  naturally.  The engine decides what to do next (play next track or leave).
* ``notify_fn`` is an optional async callable injected by the lifecycle so
  the engine can send "Now Playing" messages without importing pyrogram.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, List, Optional, Tuple

from app.core.logger import logger
from app.player.models import Track
from app.player.queue import QueueManager
from app.services.youtube import YouTubeService
from app.streaming.ffmpeg import FFmpegStreamBuilder
from app.streaming.voice_chat import VoiceChatManager

# Signature: notify_fn(chat_id, text)
NotifyFn = Callable[[int, str], Awaitable[None]]


class MusicEngine:
    """
    Orchestrates search → queue → stream for every group chat.

    Parameters
    ----------
    queue:
        Shared QueueManager instance.
    vc:
        VoiceChatManager (already started).
    yt:
        YouTubeService for search and stream-URL resolution.
    notify_fn:
        Async callable that sends a text message to a chat.
        Signature: ``async def notify(chat_id: int, text: str) -> None``.
        Optional — "Now Playing" notifications are skipped when absent.
    max_queue_size:
        Hard cap on upcoming tracks per chat.
    """

    def __init__(
        self,
        queue: QueueManager,
        vc: VoiceChatManager,
        yt: YouTubeService,
        notify_fn: Optional[NotifyFn] = None,
        max_queue_size: int = 50,
    ) -> None:
        self._queue = queue
        self._vc = vc
        self._yt = yt
        self._notify = notify_fn
        self._max_queue = max_queue_size
        # Guards against concurrent on_stream_end calls for the same chat
        self._stream_end_locks: dict[int, asyncio.Lock] = {}

    # ── Public commands ───────────────────────────────────────────────────────

    async def play(
        self,
        chat_id: int,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Tuple[Track, bool]:
        """
        Search YouTube and either start playback or enqueue the result.

        Returns
        -------
        (track, is_playing_now)
            ``is_playing_now`` is ``True`` when the track started immediately,
            ``False`` when it was added to the queue behind existing tracks.

        Raises
        ------
        ValueError
            No search results found.
        RuntimeError
            Queue is full, or the voice chat could not be joined.
        """
        # Search
        track = await self._yt.search(query, requested_by_id, requested_by_name)
        if track is None:
            raise ValueError(f"No results found for: **{query}**")

        # Queue-full guard
        queue_size = await self._queue.size(chat_id)
        if queue_size >= self._max_queue:
            raise RuntimeError(
                f"Queue is full ({self._max_queue} tracks). "
                "Use /skip or /stop to make room."
            )

        is_idle = not self._queue.has_active_session(chat_id)

        await self._queue.add(chat_id, track)

        if is_idle:
            # Nothing is playing — start immediately
            await self._start_next(chat_id)
            return track, True
        else:
            # Something is already playing — track is queued
            return track, False

    async def skip(self, chat_id: int) -> Optional[Track]:
        """
        Skip the current track.

        Starts the next queued track if one exists, otherwise stops and
        leaves the voice chat.

        Returns the new current track, or ``None`` when the queue was empty.
        """
        upcoming_empty = await self._queue.is_empty(chat_id)

        if upcoming_empty:
            await self._leave_and_cleanup(chat_id)
            return None

        next_track = await self._queue.pop_next(chat_id)
        if next_track is None:
            await self._leave_and_cleanup(chat_id)
            return None

        stream_url = await self._yt.get_stream_url(next_track.webpage_url)
        if stream_url is None:
            logger.error("skip: could not resolve stream URL for '{}'", next_track.title)
            # Try the one after this
            return await self.skip(chat_id)

        stream = FFmpegStreamBuilder.build(stream_url)
        changed = await self._vc.change_stream(chat_id, stream)
        if not changed:
            await self._leave_and_cleanup(chat_id)
            return None

        return next_track

    async def stop(self, chat_id: int) -> None:
        """Stop playback, clear the queue, and leave the voice chat."""
        await self._queue.clear(chat_id)
        await self._leave_and_cleanup(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause the current stream. Returns False when nothing is playing."""
        if not self._vc.is_active(chat_id):
            return False
        return await self._vc.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns False when not in a VC."""
        if not self._vc.is_active(chat_id):
            return False
        return await self._vc.resume(chat_id)

    # ── State queries ─────────────────────────────────────────────────────────

    def get_current_track(self, chat_id: int) -> Optional[Track]:
        return self._queue.get_current(chat_id)

    async def get_upcoming(self, chat_id: int) -> List[Track]:
        return await self._queue.get_upcoming(chat_id)

    def is_active(self, chat_id: int) -> bool:
        return self._vc.is_active(chat_id)

    # ── Stream-end callback (called by VoiceChatManager) ─────────────────────

    async def on_stream_end(self, chat_id: int) -> None:
        """
        Invoked automatically when a track finishes playing.

        Plays the next queued track or leaves the VC if the queue is empty.
        Protected by a per-chat lock to prevent race conditions when multiple
        end events arrive close together (kicked + stream_end, etc.).
        """
        if chat_id not in self._stream_end_locks:
            self._stream_end_locks[chat_id] = asyncio.Lock()

        async with self._stream_end_locks[chat_id]:
            logger.info("on_stream_end triggered — chat_id={}", chat_id)

            if await self._queue.is_empty(chat_id):
                await self._leave_and_cleanup(chat_id)
                return

            next_track = await self._queue.pop_next(chat_id)
            if next_track is None:
                await self._leave_and_cleanup(chat_id)
                return

            stream_url = await self._yt.get_stream_url(next_track.webpage_url)
            if stream_url is None:
                logger.error(
                    "on_stream_end: could not resolve stream URL for '{}' — skipping",
                    next_track.title,
                )
                # Recurse once to try the next track
                await self.on_stream_end(chat_id)
                return

            stream = FFmpegStreamBuilder.build(stream_url)
            changed = await self._vc.change_stream(chat_id, stream)

            if changed:
                logger.info(
                    "Auto-advancing to '{}' in chat_id={}",
                    next_track.title,
                    chat_id,
                )
                await self._send_now_playing(chat_id, next_track)
            else:
                logger.warning(
                    "Auto-advance failed for chat_id={} — leaving VC",
                    chat_id,
                )
                await self._leave_and_cleanup(chat_id)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _start_next(self, chat_id: int) -> None:
        """
        Pop the head of the queue, resolve its stream URL, and begin playback.
        Raises ``RuntimeError`` if the voice chat cannot be joined.
        """
        track = await self._queue.pop_next(chat_id)
        if track is None:
            return

        stream_url = await self._yt.get_stream_url(track.webpage_url)
        if stream_url is None:
            raise RuntimeError(
                f"Could not resolve audio stream for **{track.title}**. "
                "The video may be unavailable or region-locked."
            )

        stream = FFmpegStreamBuilder.build(stream_url)
        joined = await self._vc.play(chat_id, stream)
        if not joined:
            # Roll the track back so it isn't lost
            self._queue.set_current(chat_id, None)
            raise RuntimeError(
                "Could not join the voice chat. "
                "Make sure a voice chat is active and the bot has permission to join."
            )

    async def _leave_and_cleanup(self, chat_id: int) -> None:
        await self._vc.leave(chat_id)
        await self._queue.clear(chat_id)
        logger.info("Playback ended, queue cleared — chat_id={}", chat_id)

    async def _send_now_playing(self, chat_id: int, track: Track) -> None:
        """Send a 'Now Playing' notification if a notify function is set."""
        if self._notify is None:
            return
        text = (
            "🎵 **Now Playing**\n\n"
            f"**{track.title}**\n"
            f"⏱ {track.formatted_duration}  •  👤 {track.uploader}\n"
            f"Requested by: {track.requested_by_name}"
        )
        try:
            await self._notify(chat_id, text)
        except Exception as exc:
            logger.debug("Could not send now-playing notification: {}", exc)
