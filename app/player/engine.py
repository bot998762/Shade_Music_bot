"""
app.player.engine
~~~~~~~~~~~~~~~~~
MusicEngine — orchestrates search → queue → stream for every group chat.

Fixed stream flow (py-tgcalls==2.3.3 + ntgcalls==2.2.5)
----------------------------------------------------------
OLD (broken):
  1. search() → Track(webpage_url)
  2. FFmpegStreamBuilder.build_from_youtube(webpage_url, cookies)
       → MediaStream(webpage_url, ytdlp_parameters="--cookies ...")
  3. ntgcalls internal yt-dlp runs WITHOUT cookies → bot-detected

NEW (fixed):
  1. search() → Track(webpage_url)
  2. _resolve_stream(track):
       a. YouTubeService.get_stream_url(webpage_url)
            → Python yt-dlp, cookiefile + android client in dict opts
            → returns direct CDN URL (e.g. googlevideo.com)
       b. FFmpegStreamBuilder.build_from_url(direct_cdn_url)
            → MediaStream(direct_cdn_url)  ← no yt-dlp inside ntgcalls
  3. ntgcalls passes direct URL straight to FFmpeg → streams ✅

  On get_stream_url() failure (timeout, network, etc.):
       a. Falls back to FFmpegStreamBuilder.build_from_youtube(webpage_url, cookies)
          (original behaviour — may fail on flagged IPs, but won't crash)

The key insight: cookies work reliably when applied as Python dict options
(cookiefile="...") in the Python yt-dlp layer.  They do NOT work when
passed as ytdlp_parameters="--cookies ..." to MediaStream because
ntgcalls 2.2.5 silently ignores them in its internal yt-dlp call.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, List, Optional, Tuple

from pytgcalls.types import MediaStream

from app.core.logger import logger
from app.player.models import Track
from app.player.queue import QueueManager
from app.services.youtube import YouTubeService
from app.streaming.ffmpeg import FFmpegStreamBuilder
from app.streaming.voice_chat import VoiceChatManager

NotifyFn = Callable[[int, str], Awaitable[None]]

_MAX_SKIP_RETRIES = 3


class MusicEngine:
    """
    Orchestrates search → queue → stream for every group chat.

    Parameters
    ----------
    queue:          Shared QueueManager instance.
    vc:             VoiceChatManager (already started).
    yt:             YouTubeService — search + stream URL resolution.
    notify_fn:      Optional async callable — sends "Now Playing" messages.
    max_queue_size: Hard cap on upcoming tracks per chat.
    cookies_path:   Path to cookies.txt — used ONLY in the fallback path
                    (FFmpegStreamBuilder.build_from_youtube). Primary path
                    (YouTubeService.get_stream_url) uses the copy already
                    stored in YouTubeService._cookies_path.
    """

    def __init__(
        self,
        queue: QueueManager,
        vc: VoiceChatManager,
        yt: YouTubeService,
        notify_fn: Optional[NotifyFn] = None,
        max_queue_size: int = 50,
        cookies_path: Optional[str] = None,
    ) -> None:
        self._queue = queue
        self._vc = vc
        self._yt = yt
        self._notify = notify_fn
        self._max_queue = max_queue_size
        self._cookies = cookies_path
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
        Search YouTube and either start playback or enqueue.
        Returns (track, is_playing_now).
        Raises ValueError (no results) or RuntimeError (queue full / join failed).
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
        """Skip current track. Returns new current track or None if queue empty."""
        if await self._queue.is_empty(chat_id):
            await self._leave_and_cleanup(chat_id)
            return None

        next_track = await self._queue.pop_next(chat_id)
        if next_track is None:
            await self._leave_and_cleanup(chat_id)
            return None

        # Resolve direct CDN URL before changing stream
        stream = await self._resolve_stream(next_track)

        self._vc.begin_skip(chat_id)
        try:
            changed = await self._vc.change_stream(chat_id, stream)
        finally:
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
        """Stop playback, clear queue, leave VC."""
        await self._queue.clear(chat_id)
        await self._leave_and_cleanup(chat_id)

    async def pause(self, chat_id: int) -> bool:
        if not self._vc.is_active(chat_id):
            return False
        return await self._vc.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
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

    # ── Stream-end callback ───────────────────────────────────────────────────

    async def on_stream_end(self, chat_id: int) -> None:
        """Auto-advance queue when a track ends. Iterative, lock-protected."""
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

                # Resolve direct CDN URL before auto-advancing
                stream = await self._resolve_stream(next_track)
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
                        "change_stream failed for '{}' (attempt {}/{})  chat_id={}",
                        next_track.title, retries, _MAX_SKIP_RETRIES, chat_id,
                    )

            logger.error(
                "{} consecutive failures — leaving VC  chat_id={}",
                _MAX_SKIP_RETRIES, chat_id,
            )
            await self._leave_and_cleanup(chat_id)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _resolve_stream(self, track: Track) -> MediaStream:
        """
        THE CORE FIX — resolve a MediaStream for the given track.

        Step 1: Call YouTubeService.get_stream_url(webpage_url) which uses
                Python yt-dlp with cookiefile + android player_client.
                Returns a direct CDN audio URL on success.

        Step 2a: SUCCESS → FFmpegStreamBuilder.build_from_url(direct_url)
                 MediaStream receives a direct https://... URL.
                 ntgcalls passes it to FFmpeg with zero yt-dlp invocations.
                 No bot-detection risk.

        Step 2b: FAILURE (timeout / rate-limit / error) →
                 FFmpegStreamBuilder.build_from_youtube(webpage_url, cookies)
                 Fallback to the old MediaStream(webpage_url) approach.
                 May fail on Render's flagged IP but won't crash the bot.
        """
        direct_url = await self._yt.get_stream_url(track.webpage_url)

        if direct_url:
            logger.info(
                "Stream resolved via Python yt-dlp  title='{}' cdnUrl={}...",
                track.title, direct_url[:60],
            )
            return FFmpegStreamBuilder.build_from_url(direct_url)

        logger.warning(
            "get_stream_url returned None for '{}' — using fallback MediaStream(webpage_url)",
            track.title,
        )
        return FFmpegStreamBuilder.build_from_youtube(track.webpage_url, self._cookies)

    async def _start_next(self, chat_id: int) -> None:
        """Pop queue head, resolve CDN URL, and start playback."""
        track = await self._queue.pop_next(chat_id)
        if track is None:
            return

        stream = await self._resolve_stream(track)
        joined = await self._vc.play(chat_id, stream)

        if not joined:
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


def format_now_playing(track: "Track") -> str:
    """
    Shared helper — produces the Now Playing message text.
    Imported by music.py handlers for consistent formatting.
    """
    return (
        "🎵 **Now Playing**\n\n"
        f"**{track.title}**\n"
        f"⏱ {track.formatted_duration}  •  👤 {track.uploader}\n"
        f"Requested by: {track.requested_by_name}"
    )
