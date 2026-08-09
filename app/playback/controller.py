"""
app.playback.controller
~~~~~~~~~~~~~~~~~~~~~~~~
PlaybackController — the sole playback authority.

Responsibility
--------------
Receive a /play request.
Coordinate search → track creation → stream resolution → VC join.
Drive all auto-advance decisions when a track finishes.
Own every playback decision made in this application.

What it does NOT do
-------------------
* It does not search YouTube (search.youtube does that).
* It does not resolve stream URLs (search.resolver does that).
* It does not manage the voice chat (streaming.voice does that).
* It does not send Telegram messages (handlers do that).
* It does not perform cleanup steps (playback.cleanup does that).

What StreamMonitor does
-----------------------
StreamMonitor is a passive observer. When VoiceChatManager fires a
stream-end event, StreamMonitor logs it and calls controller.advance().
All decisions about what to do next are made here, not there.

Architecture
------------
PlaybackController receives all its dependencies via __init__
(dependency injection) so each dependency is tested independently and
swapped without touching this file.

/play workflow
--------------
1. [VALIDATE]  Input already validated by handler and validators.
2. [SEARCH]    YouTubeSearch.search(query) → SearchResult
3. [TRACK]     Track.from_search_result() — attaches request context
4. [ENQUEUE]   SessionManager.enqueue()
5. [IDLE?]     If chat was idle → start playback now
               Else → return "added to queue" result to handler
6. [RESOLVE]   StreamResolver.resolve() → direct CDN URL
7. [FFMPEG]    FFmpegStreamBuilder.build_from_url() → MediaStream
8. [JOIN VC]   VoiceChatManager.play() → join and stream
9. [STATE]     StateManager.transition_to_playing()
10.[RETURN]    Return (track, is_playing_now) to handler

advance() workflow  (called by StreamMonitor on stream-end)
-----------------------------------------------------------
1. [ADVANCE]   Acquire per-chat lock (prevents double-advance)
2. [ADVANCE]   Queue empty? → cleanup → IDLE
3. [ADVANCE]   Dequeue next track
4. [RESOLVE]   Resolve stream URL
5. [FFMPEG]    Build MediaStream
6. [ADVANCE]   voice.change_stream() → update state → notify
7. [ADVANCE]   Retry up to MAX_SKIP_RETRIES on change_stream failure
8. [ADVANCE]   Retries exhausted? → cleanup → IDLE

Stage log: [CONTROLLER]
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional, Tuple

from app.infrastructure.logger import logger
from app.playback.cleanup import CleanupService
from app.playback.session import SessionManager
from app.playback.state import StateManager
from app.search.models import Track
from app.search.resolver import StreamResolver
from app.search.youtube import YouTubeSearch
from app.shared.constants import MAX_SKIP_RETRIES
from app.shared.errors import NOW_PLAYING
from app.shared.exceptions import NoResultsError, QueueFullError, VoiceChatError
from app.streaming.ffmpeg import FFmpegStreamBuilder
from app.streaming.voice import VoiceChatManager

NotifyFn = Callable[[int, str], Awaitable[None]]


class PlaybackController:
    """
    The sole playback authority.

    Owns every decision about what plays, when it plays, and what happens
    when it stops. No other module makes playback decisions.

    Parameters
    ----------
    search:       YouTubeSearch instance.
    resolver:     StreamResolver instance.
    voice:        VoiceChatManager instance (already started).
    session:      SessionManager instance.
    state:        StateManager instance.
    cleanup:      CleanupService instance.
    notify:       Optional async callable — sends "Now Playing" messages.
    max_queue:    Hard cap on upcoming tracks per chat.
    cookies_path: Passed to the FFmpeg fallback path.
    """

    def __init__(
        self,
        search:       YouTubeSearch,
        resolver:     StreamResolver,
        voice:        VoiceChatManager,
        session:      SessionManager,
        state:        StateManager,
        cleanup:      CleanupService,
        notify:       Optional[NotifyFn] = None,
        max_queue:    int = 50,
        cookies_path: Optional[str] = None,
    ) -> None:
        self._search   = search
        self._resolver = resolver
        self._voice    = voice
        self._session  = session
        self._state    = state
        self._cleanup  = cleanup
        self._notify   = notify
        self._max_q    = max_queue
        self._cookies  = cookies_path
        # One lock per chat — prevents concurrent StreamAudioEnded events
        # from double-advancing the queue.
        self._advance_locks: Dict[int, asyncio.Lock] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def play(
        self,
        chat_id:           int,
        query:             str,
        requested_by_id:   int,
        requested_by_name: str,
    ) -> Tuple[Track, bool]:
        """
        Full /play pipeline: search → enqueue → start if idle.

        Returns
        -------
        (track, is_playing_now)
            is_playing_now is True when playback started immediately.
            is_playing_now is False when the track was added to the queue.

        Raises
        ------
        NoResultsError
            When the search returns no results.
        QueueFullError
            When the chat's queue is at its limit.
        VoiceChatError
            When the VC join fails.
        """
        # ── Stage: SEARCH ─────────────────────────────────────────────────
        logger.info("[CONTROLLER] [SEARCH] query='{}'  chat_id={}", query, chat_id)
        result = await self._search.search(query)

        if result is None:
            logger.warning(
                "[CONTROLLER] [SEARCH] No results  query='{}'  chat_id={}",
                query, chat_id,
            )
            raise NoResultsError(query)

        # ── Stage: TRACK ──────────────────────────────────────────────────
        track = Track.from_search_result(result, requested_by_id, requested_by_name)
        logger.info(
            "[CONTROLLER] [TRACK] Created  title='{}'  url='{}'",
            track.title, track.webpage_url,
        )

        # ── Stage: ENQUEUE ────────────────────────────────────────────────
        queue_size = await self._session.size(chat_id)
        if queue_size >= self._max_q:
            raise QueueFullError(
                f"Queue is full ({self._max_q} tracks). "
                "Wait for the current track to finish."
            )

        was_idle = self._state.is_idle(chat_id)
        position = await self._session.enqueue(chat_id, track)
        logger.info(
            "[CONTROLLER] [ENQUEUE] title='{}'  chat_id={}  "
            "position={}  was_idle={}",
            track.title, chat_id, position, was_idle,
        )

        # ── Start or queue ─────────────────────────────────────────────────
        if was_idle:
            await self._start_now(chat_id)
            return track, True
        else:
            return track, False

    async def advance(self, chat_id: int) -> None:
        """
        Auto-advance to the next queued track after the current one ends.

        Called exclusively by StreamMonitor when a stream-end event fires.
        StreamMonitor is a passive observer — it fires this method and returns.
        All decisions about what to play next are made here.

        Lock-protected per chat_id so concurrent StreamAudioEnded events
        (which PyTgCalls can fire for the same chat under some conditions)
        do not double-advance the queue.

        Stage log: [CONTROLLER] [ADVANCE]
        """
        if chat_id not in self._advance_locks:
            self._advance_locks[chat_id] = asyncio.Lock()

        async with self._advance_locks[chat_id]:
            logger.info("[CONTROLLER] [ADVANCE] Advancing  chat_id={}", chat_id)
            retries = 0

            while retries < MAX_SKIP_RETRIES:
                # ── Queue empty: all tracks played ─────────────────────────
                if await self._session.is_empty(chat_id):
                    logger.info(
                        "[CONTROLLER] [ADVANCE] Queue exhausted — "
                        "leaving VC  chat_id={}",
                        chat_id,
                    )
                    await self._cleanup.cleanup(chat_id, reason="queue_exhausted")
                    return

                # ── Dequeue next track ─────────────────────────────────────
                next_track = await self._session.dequeue(chat_id)
                if next_track is None:
                    logger.error(
                        "[CONTROLLER] [ADVANCE] dequeue() returned None "
                        "despite non-empty queue  chat_id={}",
                        chat_id,
                    )
                    await self._cleanup.cleanup(chat_id, reason="dequeue_returned_none")
                    return

                # ── Resolve and switch stream ──────────────────────────────
                logger.info(
                    "[CONTROLLER] [ADVANCE] Next track: '{}'  chat_id={}",
                    next_track.title, chat_id,
                )
                stream = await self._build_stream(next_track)
                changed = await self._voice.change_stream(chat_id, stream)

                if changed:
                    self._state.update_current_track(chat_id, next_track)
                    logger.info(
                        "[CONTROLLER] [ADVANCE] Playback advanced to '{}'  "
                        "chat_id={}",
                        next_track.title, chat_id,
                    )
                    await self._send_now_playing(chat_id, next_track)
                    return

                # ── change_stream failed — retry with next track ───────────
                retries += 1
                logger.warning(
                    "[CONTROLLER] [ADVANCE] change_stream failed for '{}' "
                    "(attempt {}/{})  chat_id={}",
                    next_track.title, retries, MAX_SKIP_RETRIES, chat_id,
                )

            # ── Retries exhausted ──────────────────────────────────────────
            logger.error(
                "[CONTROLLER] [ADVANCE] {} consecutive failures — "
                "triggering cleanup  chat_id={}",
                MAX_SKIP_RETRIES, chat_id,
            )
            await self._cleanup.cleanup(
                chat_id, reason="advance_retries_exhausted"
            )

    def is_active(self, chat_id: int) -> bool:
        """Return True when the bot is streaming in this chat."""
        return self._voice.is_active(chat_id)

    # ── Private pipeline ──────────────────────────────────────────────────────

    async def _start_now(self, chat_id: int) -> None:
        """
        Dequeue the head track, resolve its stream, and join the voice chat.

        Called by play() when the chat was idle — first track in a new session.

        On VC join failure: runs cleanup() to clear the queue and then raises
        VoiceChatError. State remains IDLE. The next /play starts clean.

        Stage log: [RESOLVE] [FFMPEG] [JOIN VC] [PLAY]
        """
        track = await self._session.dequeue(chat_id)
        if track is None:
            return

        # ── Stage: RESOLVE ────────────────────────────────────────────────
        logger.info(
            "[CONTROLLER] [RESOLVE] Resolving stream  title='{}'",
            track.title,
        )
        stream = await self._build_stream(track)

        # ── Stage: JOIN VC ────────────────────────────────────────────────
        logger.info(
            "[CONTROLLER] [JOIN VC] Joining voice chat  chat_id={}",
            chat_id,
        )
        joined = await self._voice.play(chat_id, stream)

        if not joined:
            # Wipe the session clean before raising.
            # State is still IDLE — transition_to_playing was not reached.
            # Cleanup clears the queue so the next /play starts from a
            # clean slate instead of replaying the failed track.
            await self._cleanup.cleanup(chat_id, reason="vc_join_failed")
            raise VoiceChatError(
                "Could not join the voice chat. "
                "Make sure a voice chat is active and the assistant has "
                "permission to join."
            )

        # ── Stage: PLAY ───────────────────────────────────────────────────
        self._state.transition_to_playing(chat_id, track)
        logger.info(
            "[CONTROLLER] [PLAY] Playback started  title='{}'  chat_id={}",
            track.title, chat_id,
        )

    async def _build_stream(self, track: Track):
        """
        Resolve a playable MediaStream for *track*.

        Single implementation — used by both _start_now() and advance().
        This is the only place stream resolution and MediaStream creation occur.

        Primary:  StreamResolver → direct CDN URL → FFmpegStreamBuilder.build_from_url()
        Fallback: FFmpegStreamBuilder.build_from_youtube() when resolver fails.

        Stage log: [RESOLVE] [FFMPEG]
        """
        direct_url = await self._resolver.resolve(track.webpage_url)

        if direct_url:
            logger.info(
                "[CONTROLLER] [RESOLVE] Direct CDN URL obtained  "
                "title='{}'  url={}...",
                track.title, direct_url[:60],
            )
            logger.info("[CONTROLLER] [FFMPEG] Building MediaStream from direct URL")
            return FFmpegStreamBuilder.build_from_url(direct_url)

        logger.warning(
            "[CONTROLLER] [RESOLVE] Resolver returned None for '{}' — "
            "using fallback",
            track.title,
        )
        logger.warning("[CONTROLLER] [FFMPEG] Building fallback MediaStream")
        return FFmpegStreamBuilder.build_from_youtube(track.webpage_url, self._cookies)

    async def _send_now_playing(self, chat_id: int, track: Track) -> None:
        """
        Send a Now Playing notification via the bot client.

        Errors are suppressed — a failed notification never affects playback.
        """
        if self._notify is None:
            return
        text = NOW_PLAYING.format(
            title=track.title,
            duration=track.formatted_duration,
            uploader=track.uploader,
            requested_by=track.requested_by_name,
        )
        try:
            await self._notify(chat_id, text)
        except Exception as exc:
            logger.debug(
                "[CONTROLLER] Now-playing notification failed  "
                "chat_id={}  error={}",
                chat_id, exc,
            )
