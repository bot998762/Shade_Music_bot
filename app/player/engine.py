"""
app.player.engine
~~~~~~~~~~~~~~~~~
MusicEngine — orchestrates search → queue → stream for every group chat.

Stream URL strategy (py-tgcalls==2.3.3)
----------------------------------------
py-tgcalls 2.3.x has yt-dlp built in. MediaStream accepts YouTube watch
URLs directly and handles extraction internally via its own yt-dlp call.

We pass the webpage_url (youtube.com/watch?v=...) directly to MediaStream
via FFmpegStreamBuilder.build_from_youtube(). This means:
  • No separate yt-dlp pre-resolution step needed for YouTube URLs.
  • No expiring pre-signed CDN URLs to manage.
  • Library's own format selection handles PO tokens, signatures, etc.

YouTubeService.search() still runs to get metadata (title, duration,
thumbnail, uploader) for the queue display. But get_stream_url() is no
longer called — we use the webpage_url stored on the Track directly.

Design rules
------------
* No Telegram API calls inside the engine.
* All errors are caught here; callers receive well-typed exceptions or None.
* on_stream_end is iterative (while loop) — no recursion.
* skip() sets VoiceChatManager.begin_skip() guard before change_stream().
* _start_next() re-queues the track on join failure so it is not lost.
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

NotifyFn = Callable[[int, str], Awaitable[None]]

# Max consecutive stream failures before giving up and leaving VC.
_MAX_SKIP_RETRIES = 3


class MusicEngine:
    """
    Orchestrates search → queue → stream for every group chat.

    Parameters
    ----------
    queue:   Shared QueueManager instance.
    vc:      VoiceChatManager (already started).
    yt:      YouTubeService for search (metadata only).
    notify_fn: Optional async callable — sends "Now Playing" messages.
    max_queue_size: Hard cap on upcoming tracks per chat.
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

        Returns (track, is_playing_now).
        Raises ValueError (no results) or RuntimeError (queue full / VC join failed).
        """
        track = await self._yt.search(query, requested_by_id, requested_by_name)
        if track is None:
            raise ValueError(f"No results found for: **{query}**")

        queue_size = await self._queue.size(chat_id)
        if queue_size >= self._max_queue:
            raise RuntimeError(
                f"Queue is full ({self._max_queue} tracks). "
                "Use /skip or /stop to make room."
            )

        is_idle = not self._queue.has_active_session(chat_id)
        await self._queue.add(chat_id, track)

        if is_idle:
            await self._start_next(chat_id)
            return track, True
        else:
            return track, False

    async def skip(self, chat_id: int) -> Optional[Track]:
        """
        Skip the current track and play the next one.

        Returns the new current track, or None when the queue is empty.
        """
        if await self._queue.is_empty(chat_id):
            await self._leave_and_cleanup(chat_id)
            return None

        next_track = await self._queue.pop_next(chat_id)
        if next_track is None:
            await self._leave_and_cleanup(chat_id)
            return None

        stream = FFmpegStreamBuilder.build_from_youtube(next_track.webpage_url)

        self._vc.begin_skip(chat_id)
        try:
            changed = await self._vc.change_stream(chat_id, stream)
        finally:
            # Delay clearing the skip guard — pytgcalls dispatches
            # StreamAudioEnded asynchronously for the replaced track.
            async def _delayed_end_skip() -> None:
                await asyncio.sleep(1.0)
                self._vc.end_skip(chat_id)
            asyncio.ensure_future(_delayed_end_skip())

        if not changed:
            await self._leave_and_cleanup(chat_id)
            return None

        self._queue.set_current(chat_id, next_track)
        await self._send_now_playing(chat_id, next_track)
        return next_track

    async def stop(self, chat_id: int) -> None:
        """Stop playback, clear the queue, and leave the voice chat."""
        await self._queue.clear(chat_id)
        await self._leave_and_cleanup(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause. Returns False when nothing is playing."""
        if not self._vc.is_active(chat_id):
            return False
        return await self._vc.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        """Resume. Returns False when not in a VC."""
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
        Invoked when a track finishes playing or VC is closed.
        Auto-advances the queue. Iterative — no recursion.
        Protected by a per-chat asyncio.Lock.
        """
        if chat_id not in self._stream_end_locks:
            self._stream_end_locks[chat_id] = asyncio.Lock()

        async with self._stream_end_locks[chat_id]:
            logger.info("on_stream_end — chat_id={}", chat_id)
            retries = 0

            while retries < _MAX_SKIP_RETRIES:
                if await self._queue.is_empty(chat_id):
                    logger.info("Queue exhausted — leaving VC  chat_id={}", chat_id)
                    await self._leave_and_cleanup(chat_id)
                    return

                next_track = await self._queue.pop_next(chat_id)
                if next_track is None:
                    await self._leave_and_cleanup(chat_id)
                    return

                # Build stream — pass YouTube URL directly to the library.
                stream = FFmpegStreamBuilder.build_from_youtube(next_track.webpage_url)
                changed = await self._vc.change_stream(chat_id, stream)

                if changed:
                    self._queue.set_current(chat_id, next_track)
                    logger.info(
                        "Auto-advanced to '{}' — chat_id={}",
                        next_track.title, chat_id,
                    )
                    await self._send_now_playing(chat_id, next_track)
                    return
                else:
                    retries += 1
                    logger.warning(
                        "change_stream failed for '{}' (attempt {}/{}) — chat_id={}",
                        next_track.title, retries, _MAX_SKIP_RETRIES, chat_id,
                    )

            logger.error(
                "{} consecutive failures — leaving VC  chat_id={}",
                _MAX_SKIP_RETRIES, chat_id,
            )
            await self._leave_and_cleanup(chat_id)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _start_next(self, chat_id: int) -> None:
        """
        Pop the queue head and start playback.

        Passes the YouTube watch URL directly to MediaStream —
        py-tgcalls 2.3.x handles yt-dlp extraction internally.

        Raises RuntimeError if the voice chat cannot be joined.
        """
        track = await self._queue.pop_next(chat_id)
        if track is None:
            return

        # Pass YouTube URL directly — library's built-in yt-dlp handles it.
        stream = FFmpegStreamBuilder.build_from_youtube(track.webpage_url)
        joined = await self._vc.play(chat_id, stream)

        if not joined:
            # Re-queue the track so it is not lost.
            await self._queue.prepend(chat_id, track)
            self._queue.set_current(chat_id, None)
            raise RuntimeError(
                "Could not join the voice chat. "
                "Make sure a voice chat is active and the assistant has "
                "permission to join."
            )

        self._queue.set_current(chat_id, track)

    async def _leave_and_cleanup(self, chat_id: int) -> None:
        await self._vc.leave(chat_id)
        await self._queue.clear(chat_id)
        logger.info("Playback ended, queue cleared — chat_id={}", chat_id)

    async def _send_now_playing(self, chat_id: int, track: Track) -> None:
        if self._notify is None:
            return
        text = format_now_playing(track)
        try:
            await self._notify(chat_id, text)
        except Exception as exc:
            logger.debug("Could not send now-playing notification: {}", exc)


def format_now_playing(track: Track) -> str:
    return (
        "🎵 **Now Playing**\n\n"
        f"**{track.title}**\n"
        f"⏱ {track.formatted_duration}  •  👤 {track.uploader}\n"
        f"Requested by: {track.requested_by_name}"
    )
