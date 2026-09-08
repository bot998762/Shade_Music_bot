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
6. [ADVANCE]   voice.replace_stream() → update state → notify
7. [ADVANCE]   Retry up to MAX_SKIP_RETRIES on replace_stream failure
8. [ADVANCE]   Retries exhausted? → cleanup → IDLE

_build_stream() failure modes
------------------------------
resolver.resolve() raises StreamResolveTimeoutError on asyncio timeout.
resolver.resolve() returns None on genuine yt-dlp extraction failure.

  SUCCESS (str returned)              → primary CDN path
  FAILURE (None returned)             → raises StreamResolveError
  TIMEOUT (StreamResolveTimeoutError) → re-raised as-is

Why the ntgcalls fallback was removed
--------------------------------------
The previous implementation fell back to FFmpegStreamBuilder.build_from_youtube()
when resolver returned None.  build_from_youtube() passes the URL to ntgcalls
which runs yt-dlp + Deno + FFmpeg internally.  When ntgcalls raised YtDlpError
(its internal yt-dlp timeout), those child processes were NOT killed by
leave_call().  Across multiple tracks / concurrent chats, they accumulated:

  2 chats × (yt-dlp ~100 MB + Deno ~80 MB + FFmpeg ~30 MB) ≈ 420 MB
  + bot base ~150 MB = ~570 MB → OOM SIGKILL on Render 512 MB

The fallback also produced a misleading "Could not join voice chat" error
when the real failure was stream extraction.

The fix: when resolver returns None, raise StreamResolveError immediately.
No new processes are spawned.  The user receives a specific "stream unavailable"
message.  Memory stays bounded.

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
from app.shared.exceptions import (
    NoResultsError,
    PrivateGroupError,
    QueueFullError,
    StreamResolveError,
    StreamResolveTimeoutError,
    VoiceChatError,
)
from app.shared.validators import is_direct_url
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
    cookies_path: Kept for interface compatibility; not used by resolver.
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
        # One advance lock per chat — prevents concurrent StreamAudioEnded events
        # from double-advancing the queue.
        self._advance_locks: Dict[int, asyncio.Lock] = {}
        # One play lock per chat — prevents concurrent /play commands in the
        # same idle chat from each calling _start_now() simultaneously.
        self._play_locks: Dict[int, asyncio.Lock] = {}

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
            When the search/metadata fetch returns no results.
        QueueFullError
            When the chat's queue is at its limit.
        StreamResolveError
            When yt-dlp cannot extract a playable stream URL.
            Show "stream unavailable" — not "no results found".
        StreamResolveTimeoutError
            When the resolver times out.  Fallback suppressed.
            The caller should show a "try again" message.
        VoiceChatError
            When the VC join fails.
        """
        # ── Stage: SEARCH or DIRECT-URL FETCH ────────────────────────
        # For a direct URL: skip the ytsearch1: round-trip and fetch metadata
        # directly.  The CDN stream URL is resolved by _build_stream() at play
        # time — same path as for search results.
        if is_direct_url(query):
            logger.info(
                "[CONTROLLER] [FETCH_URL] Direct URL  url='{}'  chat_id={}",
                query, chat_id,
            )
            result = await self._search.fetch_url_metadata(query)
        else:
            logger.info(
                "[CONTROLLER] [SEARCH] query='{}'  chat_id={}", query, chat_id,
            )
            result = await self._search.search(query)

        if result is None:
            logger.warning(
                "[CONTROLLER] No result  query='{}'  chat_id={}", query, chat_id,
            )
            raise NoResultsError(query)

        # ── Stage: TRACK ──────────────────────────────────────────────────
        track = Track.from_search_result(result, requested_by_id, requested_by_name)
        logger.info(
            "[CONTROLLER] [TRACK] Created  title='{}'  url='{}'",
            track.title, track.webpage_url,
        )

        # ── Stage: ENQUEUE + START (per-chat lock) ────────────────────────
        if chat_id not in self._play_locks:
            self._play_locks[chat_id] = asyncio.Lock()

        async with self._play_locks[chat_id]:
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

            if was_idle:
                await self._start_now(chat_id)
                return track, True
            else:
                return track, False

    async def advance(self, chat_id: int) -> None:
        """
        Auto-advance to the next queued track after the current one ends.

        Called exclusively by StreamMonitor when a stream-end event fires.

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
                try:
                    stream = await self._build_stream(next_track)
                except StreamResolveTimeoutError:
                    # Timeout: yt-dlp subprocess was killed; no ghost processes.
                    # Skip this track and try the next.
                    logger.error(
                        "[CONTROLLER] [ADVANCE] Resolver timeout for '{}' — "
                        "skipping (attempt {}/{})  chat_id={}",
                        next_track.title, retries + 1, MAX_SKIP_RETRIES, chat_id,
                    )
                    retries += 1
                    continue
                except StreamResolveError:
                    # Genuine extraction failure — track is unplayable.
                    # Skip to the next track.
                    logger.error(
                        "[CONTROLLER] [ADVANCE] Stream unavailable for '{}' — "
                        "skipping (attempt {}/{})  chat_id={}",
                        next_track.title, retries + 1, MAX_SKIP_RETRIES, chat_id,
                    )
                    retries += 1
                    continue

                changed = await self._voice.replace_stream(chat_id, stream)

                if changed:
                    self._state.update_current_track(chat_id, next_track)
                    logger.info(
                        "[CONTROLLER] [ADVANCE] Playback advanced to '{}'  "
                        "chat_id={}",
                        next_track.title, chat_id,
                    )
                    await self._send_now_playing(chat_id, next_track)
                    return

                # ── replace_stream failed — retry with next track ──────────
                retries += 1
                logger.warning(
                    "[CONTROLLER] [ADVANCE] replace_stream failed for '{}' "
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

        On resolver timeout:  runs cleanup() and re-raises StreamResolveTimeoutError.
        On resolver failure:  runs cleanup() and re-raises StreamResolveError.
        On VC join failure:   runs cleanup() and raises VoiceChatError.

        State remains IDLE in all failure cases. The next /play starts clean.

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
        try:
            stream = await self._build_stream(track)
        except StreamResolveTimeoutError:
            logger.error(
                "[CONTROLLER] Resolver timeout — track unplayable  "
                "title='{}'  chat_id={}",
                track.title, chat_id,
            )
            await self._cleanup.cleanup(chat_id, reason="resolver_timeout")
            raise
        except StreamResolveError:
            logger.error(
                "[CONTROLLER] Stream unavailable — track unplayable  "
                "title='{}'  chat_id={}",
                track.title, chat_id,
            )
            await self._cleanup.cleanup(chat_id, reason="resolver_failed")
            raise

        # ── Stage: JOIN VC ────────────────────────────────────────────────
        logger.info(
            "[CONTROLLER] [JOIN VC] Joining voice chat  chat_id={}",
            chat_id,
        )
        try:
            joined = await self._voice.play(chat_id, stream)
        except PrivateGroupError:
            await self._cleanup.cleanup(chat_id, reason="private_group")
            raise

        if not joined:
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
        This is the ONLY place stream resolution and MediaStream creation occur.

        Primary path only: StreamResolver → direct CDN URL →
        FFmpegStreamBuilder.build_from_url().

        The ntgcalls yt-dlp fallback (build_from_youtube) has been removed.
        It spawned unmanaged yt-dlp+Deno+FFmpeg subprocesses that were not
        killed by leave_call(), causing cumulative OOM on Render 512 MB.
        When the resolver cannot produce a URL, StreamResolveError is raised
        and the user sees a specific "stream unavailable" message.

        Raises
        ------
        StreamResolveTimeoutError
            Propagated from resolver.resolve().
        StreamResolveError
            When resolver returns None (yt-dlp extraction failed).

        Stage log: [RESOLVE] [FFMPEG]
        """
        # StreamResolveTimeoutError propagates unmodified — do NOT catch it here.
        direct_url = await self._resolver.resolve(track.webpage_url)

        if direct_url:
            logger.info(
                "[CONTROLLER] [RESOLVE] Direct CDN URL obtained  "
                "title='{}'  url={}...",
                track.title, direct_url[:60],
            )
            logger.info("[CONTROLLER] [FFMPEG] Building MediaStream from direct URL")
            return FFmpegStreamBuilder.build_from_url(direct_url)

        # direct_url is None → yt-dlp exited non-zero (video unavailable,
        # age-restricted, geo-blocked, or API change).
        #
        # IMPORTANT: we do NOT fall back to FFmpegStreamBuilder.build_from_youtube()
        # here.  That fallback spawned ntgcalls-internal yt-dlp+Deno+FFmpeg that
        # were never properly cleaned up, causing OOM after ~2-4 tracks.
        # Instead: raise StreamResolveError so the caller can show a clear message.
        logger.error(
            "[CONTROLLER] [RESOLVE] yt-dlp returned no URL for '{}' — "
            "video may be unavailable/restricted  title='{}'",
            track.webpage_url, track.title,
        )
        raise StreamResolveError(
            f"Could not extract a stream URL for: {track.webpage_url!r}. "
            "The video may be unavailable, age-restricted, or geo-blocked."
        )

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
