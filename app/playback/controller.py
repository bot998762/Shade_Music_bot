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
4. [ENQUEUE]   SessionManager.enqueue_if_room() — atomic cap check + append
5. [IDLE?]     If chat was idle → start playback now (outside the play lock)
               Else → return "added to queue" result to handler
6. [RESOLVE]   StreamResolver.resolve() → direct CDN URL
7. [FFMPEG]    FFmpegStreamBuilder.build_from_url() → MediaStream
8. [JOIN VC]   VoiceChatManager.play() → join and stream
9. [STATE]     StateManager.transition_to_playing()
10.[RETURN]    Return (track, is_playing_now) to handler

Locking contract
----------------
_play_lock (per-chat)
    Guards the idle-check + enqueue decision.
    Held ONLY for the duration of:
        session.enqueue_if_room()  — fast, in-memory
        state.is_idle()            — fast, in-memory dict lookup
    RELEASED before any I/O: stream resolution, VC join, Telegram API.
    This ensures a second concurrent /play in the same chat can proceed
    immediately once the first track has been enqueued and the decision
    "idle → start now" has been made, without waiting for yt-dlp or
    the VC join to complete.

_advance_lock / transition lock (per-chat)
    Serialises every operation that installs or tears down a stream:
    idle-start (_start_now), advancement (natural end AND /skip, via
    _advance) and /stop.  Held across stream resolution and the voice
    replace/join on purpose, so only one stream transition per chat runs
    at a time.  It is NEVER held across Telegram message I/O (the Now
    Playing notification is sent after release).

Lock ordering (outer → inner, never reversed):
    _play_lock  →  (released)  →  transition lock  →  SessionManager lock
    The SessionManager lock is innermost and only guards in-memory queue
    operations.  stop() deliberately does NOT take the transition lock
    before invalidating: it bumps the generation and cancels the in-flight
    resolution first, then waits for the lock, so it never queues behind a
    90 s resolve.

Generation token (per chat)
    Bumped by every teardown (/stop, queue exhausted, failures).  Each
    operation remembers the generation it started under and re-checks it
    after every await that could install a stream (resolve, join, replace).
    A mismatch means the session was stopped or superseded: the stale
    stream is discarded and state/queue are left alone.

Advance epoch (per chat)
    Read BEFORE waiting for the transition lock; incremented when an
    advancement completes.  A call that finds a changed epoch after getting
    the lock is a duplicate (natural end racing /skip, or two simultaneous
    /skip presses) and does nothing, so a track can never be advanced twice.

advance() workflow  (called by StreamMonitor on stream-end)
-----------------------------------------------------------
1. [ADVANCE]   Acquire per-chat transition lock; drop if duplicate/stale
2. [ADVANCE]   Queue empty? → cleanup → IDLE
3. [ADVANCE]   Dequeue next track
4. [RESOLVE]   Resolve stream URL
5. [FFMPEG]    Build MediaStream
6. [ADVANCE]   voice.replace_stream() → update state → notify
7. [ADVANCE]   Track resolve/replace failure → skip this track, retry next
8. [ADVANCE]   MAX_SKIP_RETRIES consecutive failures → cleanup → IDLE

Retry semantics
---------------
MAX_SKIP_RETRIES limits the number of consecutive track failures (resolve
error, replace_stream failure) before giving up the entire session.
Each successful advance resets the counter to 0.  This prevents one bad
track from destroying a healthy queue while still protecting against a
queue full of unplayable tracks.

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
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from app.infrastructure.logger import logger
from app.playback.cleanup import CleanupService
from app.playback.session import SessionManager
from app.playback.state import StateManager
from app.search.models import Track
from app.search.resolver import StreamResolver
from app.search.youtube import YouTubeSearch
from app.shared.constants import MAX_SKIP_RETRIES, STALE_END_WINDOW_SEC, clamp_queue_cap
from app.shared.errors import NOW_PLAYING
from app.shared.exceptions import (
    NoResultsError,
    PlaybackCancelledError,
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


class SkipOutcome(Enum):
    """Result of a shared advancement (/skip or natural end)."""
    SKIPPED         = "skipped"          # next track is now playing
    QUEUE_ENDED     = "queue_ended"      # queue was empty: session ended
    FAILED          = "failed"           # consecutive failures: session ended
    DUPLICATE       = "duplicate"        # another advancement already happened
    CANCELLED       = "cancelled"        # /stop superseded this operation
    NOTHING_PLAYING = "nothing_playing"  # chat was idle
    STALE           = "stale"            # stream-end event too early to be genuine


@dataclass(frozen=True)
class SkipResult:
    outcome: SkipOutcome
    track:   Optional[Track] = None


@dataclass(frozen=True)
class PlaybackSnapshot:
    """Read-only view of one chat for /queue and /nowplaying."""
    current:  Optional[Track]
    upcoming: List[Track] = field(default_factory=list)
    is_idle:  bool        = True
    cap:      int         = 10


class _Superseded(Exception):
    """Internal: the session generation changed while a stream was resolving."""


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
    max_queue:    Cap on UPCOMING tracks per chat; clamped to 1..10
                  (the current track is separate and not counted).
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
        max_queue:    int = 10,
        cookies_path: Optional[str] = None,
    ) -> None:
        self._search   = search
        self._resolver = resolver
        self._voice    = voice
        self._session  = session
        self._state    = state
        self._cleanup  = cleanup
        self._notify   = notify
        self._max_q    = clamp_queue_cap(max_queue)
        if self._max_q != max_queue:
            logger.warning(
                "[CONTROLLER] max_queue={} outside 1..{} — using {}",
                max_queue, clamp_queue_cap(10**9), self._max_q,
            )
        self._cookies  = cookies_path
        # One advance lock per chat — prevents concurrent StreamAudioEnded events
        # from double-advancing the queue.
        self._advance_locks: Dict[int, asyncio.Lock] = {}
        # One play lock per chat — guards the idle-check + enqueue decision.
        # IMPORTANT: this lock is released BEFORE slow I/O (stream resolution,
        # VC join).  It protects only the brief in-memory decision window.
        self._play_locks: Dict[int, asyncio.Lock] = {}
        # Per-chat session generation: bumped by every teardown (/stop, queue
        # exhausted, failures).  In-flight operations remember the generation
        # they started under and abandon their work when it changes.
        self._gens: Dict[int, int] = {}
        # Per-chat advance epoch: incremented when an advancement completes;
        # lets a queued duplicate (natural end + /skip) detect it is redundant.
        self._epochs: Dict[int, int] = {}
        # Per-chat in-flight stream-resolution task (cancelled by /stop).
        self._inflight: Dict[int, "asyncio.Future"] = {}
        # Per-chat monotonic time at which the CURRENT stream was installed;
        # used by the stale stream-end guard.  Injectable clock for tests.
        self._clock = time.monotonic
        self._started_at: Dict[int, float] = {}

    # ── Generation tokens / lock ownership ────────────────────────────────────

    def generation(self, chat_id: int) -> int:
        """Current session generation of *chat_id* (starts at 0)."""
        return self._gens.get(chat_id, 0)

    def _bump_generation(self, chat_id: int) -> int:
        """Invalidate every in-flight operation of the chat's current session."""
        self._gens[chat_id] = self._gens.get(chat_id, 0) + 1
        return self._gens[chat_id]

    def _transition_lock(self, chat_id: int) -> asyncio.Lock:
        # The per-chat transition lock is the _advance_locks entry (kept under
        # that name for compatibility).  It serialises every operation that
        # installs or tears down a stream: idle-start, advance/skip, stop.
        if chat_id not in self._advance_locks:
            self._advance_locks[chat_id] = asyncio.Lock()
        return self._advance_locks[chat_id]

    async def _teardown(self, chat_id: int, reason: str) -> None:
        """Invalidate the session, then run the shared CleanupService teardown."""
        self._bump_generation(chat_id)
        self._started_at.pop(chat_id, None)
        await self._cleanup.cleanup(chat_id, reason=reason)

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
            When the chat already has the maximum number of UPCOMING tracks.
            The currently playing track does not count towards the cap.
        StreamResolveError
            When yt-dlp cannot extract a playable stream URL.
            Show "stream unavailable" — not "no results found".
        StreamResolveTimeoutError
            When the resolver times out.  Fallback suppressed.
            The caller should show a "try again" message.
        VoiceChatError
            When the VC join fails.
        PlaybackCancelledError
            When /stop superseded the start before a stream was installed.
        """
        # ── Stage: SEARCH or DIRECT-URL FETCH ────────────────────────
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

        # ── Stage: ENQUEUE + IDLE-CHECK (brief lock) ──────────────────────
        # The play lock guards only the in-memory decision:
        #   1. Atomic cap check + enqueue  (enqueue_if_room)
        #   2. Read is_idle to decide whether to start playback now
        # The lock is released BEFORE any I/O so that a second concurrent
        # /play in the same chat can proceed without waiting for yt-dlp or
        # the VC join of the first track.
        #
        # Capacity: the queue holds UPCOMING tracks only.  While the chat is
        # idle the track is handed straight to _start_now() and NEVER enters
        # the queue, so the first track cannot consume upcoming capacity even
        # while concurrent /play requests are being accepted.
        if chat_id not in self._play_locks:
            self._play_locks[chat_id] = asyncio.Lock()

        async with self._play_locks[chat_id]:
            was_idle = self._state.is_idle(chat_id)
            gen = self.generation(chat_id)
            if was_idle:
                # Mark PLAYING before releasing the play lock so a concurrent
                # /play cannot also see is_idle=True and race into _start_now().
                # If _start_now() fails, _teardown() reverts the state to IDLE.
                self._state.transition_to_playing(chat_id, track)
                logger.info(
                    "[CONTROLLER] [START] Idle chat — starting directly  "
                    "title='{}'  chat_id={}",
                    track.title, chat_id,
                )
            else:
                added, pos_or_size = await self._session.enqueue_if_room(
                    chat_id, track, self._max_q
                )
                if not added:
                    raise QueueFullError(
                        f"Queue is full ({self._max_q} upcoming tracks). "
                        "Wait for a track to finish or use /skip."
                    )
                logger.info(
                    "[CONTROLLER] [ENQUEUE] title='{}'  chat_id={}  position={}",
                    track.title, chat_id, pos_or_size,
                )

        if was_idle:
            started = await self._start_now(chat_id, gen, track)
            if not started:
                raise PlaybackCancelledError("Playback was stopped before it started.")
            return track, True
        return track, False

    async def advance(self, chat_id: int, reason: str = "stream_ended") -> None:
        """
        Stream-end entry point, called by StreamMonitor.

        reason: "stream_ended" (track finished), "vc_closed" or "kicked".
        Shares ``_advance()`` with /skip, so a natural end and a manual skip
        arriving together result in exactly one advancement.

        The py-tgcalls update carries no track/stream identity (the handlers
        use only ``update.chat_id``), so a delayed event from an older stream
        cannot be matched to its track.  Instead a "stream_ended" event that
        arrives within STALE_END_WINDOW_SEC of the current stream being
        installed is ignored as stale (see ``_end_is_stale``).

        Stage log: [CONTROLLER] [ADVANCE]
        """
        await self._advance(chat_id, reason=reason)

    async def skip(self, chat_id: int) -> SkipResult:
        """
        Manual /skip: advance exactly one track through the shared mechanism.

        Outcomes are described by SkipOutcome; a skip that raced with another
        advancement is reported as DUPLICATE and does NOT skip a second track.
        """
        if self._state.is_idle(chat_id):
            return SkipResult(SkipOutcome.NOTHING_PLAYING)
        return await self._advance(chat_id, reason="skip")

    async def stop(self, chat_id: int) -> bool:
        """
        Stop playback: invalidate the session, cancel any in-flight stream
        resolution, clear the upcoming queue and leave the voice chat.

        Returns True when there was something to stop, False when the chat
        was already idle.  The generation is bumped BEFORE waiting for the
        transition lock, so an in-flight start/advance can never install a
        stream after this call; it is cancelled (resolution) or discarded.
        """
        had_session = (
            not self._state.is_idle(chat_id)
            or self._voice.is_active(chat_id)
            or not await self._session.is_empty(chat_id)
        )
        self._bump_generation(chat_id)
        task = self._inflight.get(chat_id)
        if task is not None and not task.done():
            task.cancel()          # kills the yt-dlp process group (resolver)
        async with self._transition_lock(chat_id):
            # By now any in-flight operation has finished or been discarded.
            await self._teardown(chat_id, reason="stop")
        logger.info("[CONTROLLER] [STOP] Stopped  chat_id={}  had_session={}",
                    chat_id, had_session)
        return had_session

    async def snapshot(self, chat_id: int) -> PlaybackSnapshot:
        """Read-only view for /queue and /nowplaying (current track + upcoming)."""
        upcoming = await self._session.get_upcoming(chat_id)
        return PlaybackSnapshot(
            current=self._state.current_track(chat_id),
            upcoming=upcoming,
            is_idle=self._state.is_idle(chat_id),
            cap=self._max_q,
        )

    @property
    def max_queue(self) -> int:
        """Effective cap on upcoming tracks per chat (never above 10)."""
        return self._max_q

    def is_active(self, chat_id: int) -> bool:
        """Return True when the bot is streaming in this chat."""
        return self._voice.is_active(chat_id)

    # ── Private pipeline ──────────────────────────────────────────────────────

    async def _build_guarded(self, chat_id: int, gen: int, track: Track):
        """
        _build_stream() run as a cancellable task registered for the chat.

        stop() cancels the task, which makes the resolver kill the yt-dlp
        process group.  A cancellation caused by a generation change is
        reported as _Superseded; any other cancellation propagates.
        """
        task = asyncio.ensure_future(self._build_stream(track))
        self._inflight[chat_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            if self.generation(chat_id) != gen:
                raise _Superseded() from None
            raise
        finally:
            if self._inflight.get(chat_id) is task:
                del self._inflight[chat_id]

    def _end_is_stale(self, chat_id: int) -> bool:
        """
        True when a natural "stream ended" event cannot belong to the current
        stream because the current stream was installed moments ago.

        Heuristic, not identity: it catches an old stream's end event that is
        delivered just after a new stream was installed.  It cannot catch one
        delivered later, and it also ignores a genuine end of a stream that
        dies within the window (the user can /skip to recover).
        """
        started = self._started_at.get(chat_id)
        track = self._state.current_track(chat_id)
        if started is None or track is None:
            return False
        window = STALE_END_WINDOW_SEC
        if track.duration > 0:
            window = min(window, track.duration / 2)
        return (self._clock() - started) < window

    async def _advance(
        self, chat_id: int, reason: str, gen: Optional[int] = None,
    ) -> SkipResult:
        """
        THE single advancement path (natural end and /skip).

        Duplicate suppression: the advance epoch is read BEFORE waiting for
        the transition lock.  If another advancement completed while this
        call was waiting, this call is a duplicate and does nothing — a
        natural end racing with /skip, or two simultaneous /skip presses,
        advance exactly one track.  A call that arrives after the other
        advancement finished sees the new epoch and is a real request.

        Stale protection: the session generation is re-checked after every
        await that could install a stream; /stop bumps it.
        """
        entry_epoch = self._epochs.get(chat_id, 0)
        if gen is None:
            gen = self.generation(chat_id)
        new_track: Optional[Track] = None
        outcome = SkipOutcome.SKIPPED

        async with self._transition_lock(chat_id):
            if self.generation(chat_id) != gen:
                return SkipResult(SkipOutcome.CANCELLED)
            if self._epochs.get(chat_id, 0) != entry_epoch:
                logger.info(
                    "[CONTROLLER] [ADVANCE] Duplicate {} ignored  chat_id={}",
                    reason, chat_id,
                )
                return SkipResult(SkipOutcome.DUPLICATE)
            if self._state.is_idle(chat_id):
                return SkipResult(SkipOutcome.NOTHING_PLAYING)
            if reason == "stream_ended" and self._end_is_stale(chat_id):
                logger.warning(
                    "[CONTROLLER] [ADVANCE] Stale stream-end ignored "
                    "(new stream installed moments ago)  chat_id={}",
                    chat_id,
                )
                return SkipResult(SkipOutcome.STALE)

            logger.info(
                "[CONTROLLER] [ADVANCE] Advancing  chat_id={}  reason={}",
                chat_id, reason,
            )
            consecutive_failures = 0
            outcome = SkipOutcome.FAILED

            while consecutive_failures < MAX_SKIP_RETRIES:
                # ── Queue empty: all tracks played ─────────────────────────
                if await self._session.is_empty(chat_id):
                    logger.info(
                        "[CONTROLLER] [ADVANCE] Queue exhausted — "
                        "leaving VC  chat_id={}",
                        chat_id,
                    )
                    await self._teardown(chat_id, reason="queue_exhausted")
                    outcome = SkipOutcome.QUEUE_ENDED
                    break

                # ── Dequeue next track ─────────────────────────────────────
                next_track = await self._session.dequeue(chat_id)
                if next_track is None:
                    logger.error(
                        "[CONTROLLER] [ADVANCE] dequeue() returned None "
                        "despite non-empty queue  chat_id={}",
                        chat_id,
                    )
                    await self._teardown(chat_id, reason="dequeue_returned_none")
                    outcome = SkipOutcome.QUEUE_ENDED
                    break

                # ── Resolve and switch stream ──────────────────────────────
                logger.info(
                    "[CONTROLLER] [ADVANCE] Next track: '{}'  chat_id={}",
                    next_track.title, chat_id,
                )
                try:
                    stream = await self._build_guarded(chat_id, gen, next_track)
                except _Superseded:
                    return SkipResult(SkipOutcome.CANCELLED)
                except StreamResolveTimeoutError:
                    logger.error(
                        "[CONTROLLER] [ADVANCE] Resolver timeout for '{}' — "
                        "skipping (consecutive_failures={})  chat_id={}",
                        next_track.title, consecutive_failures + 1, chat_id,
                    )
                    consecutive_failures += 1
                    continue
                except StreamResolveError:
                    logger.error(
                        "[CONTROLLER] [ADVANCE] Stream unavailable for '{}' — "
                        "skipping (consecutive_failures={})  chat_id={}",
                        next_track.title, consecutive_failures + 1, chat_id,
                    )
                    consecutive_failures += 1
                    continue

                # Stopped while the build finished: never install a stale stream.
                if self.generation(chat_id) != gen:
                    return SkipResult(SkipOutcome.CANCELLED)

                changed = await self._voice.replace_stream(chat_id, stream)

                if self.generation(chat_id) != gen:
                    # /stop arrived while the stream was being replaced.  It is
                    # waiting for this lock and will leave the voice chat next;
                    # touch neither state nor queue.
                    return SkipResult(SkipOutcome.CANCELLED)

                if changed:
                    self._state.update_current_track(chat_id, next_track)
                    self._started_at[chat_id] = self._clock()
                    self._epochs[chat_id] = self._epochs.get(chat_id, 0) + 1
                    logger.info(
                        "[CONTROLLER] [ADVANCE] Playback advanced to '{}'  "
                        "chat_id={}",
                        next_track.title, chat_id,
                    )
                    new_track = next_track
                    outcome = SkipOutcome.SKIPPED
                    break

                # ── replace_stream failed — retry with next track ──────────
                consecutive_failures += 1
                logger.warning(
                    "[CONTROLLER] [ADVANCE] replace_stream failed for '{}' "
                    "(consecutive_failures={})  chat_id={}",
                    next_track.title, consecutive_failures, chat_id,
                )
            else:
                # ── Consecutive failures exhausted ─────────────────────────
                logger.error(
                    "[CONTROLLER] [ADVANCE] {} consecutive failures — "
                    "triggering cleanup  chat_id={}",
                    consecutive_failures, chat_id,
                )
                await self._teardown(chat_id, reason="advance_retries_exhausted")
                outcome = SkipOutcome.FAILED

            if outcome != SkipOutcome.SKIPPED:
                # Teardown ends the session; count it as a completed advance so
                # a duplicate request waiting behind us is dropped.
                self._epochs[chat_id] = self._epochs.get(chat_id, 0) + 1

        # Telegram I/O happens OUTSIDE the transition lock.
        if new_track is not None and self.generation(chat_id) == gen:
            await self._send_now_playing(chat_id, new_track)
        return SkipResult(outcome, new_track)

    async def _start_now(self, chat_id: int, gen: int, track: Track) -> bool:
        """
        Resolve *track*'s stream and join the voice chat (idle-start path).

        Called by play() AFTER the play lock has been released.  Runs under the
        per-chat transition lock so /stop, /skip and natural-end events cannot
        interleave with the stream installation.

        Returns True when playback started, False when the session was
        superseded by /stop (nothing installed, state untouched — it belongs
        to whoever stopped/restarted the chat).

        Failure handling (tracks other users queued meanwhile are never
        dropped silently):
          * Resolver timeout / failure — a problem with THIS track only.  If
            tracks are queued behind it, the next one is started through the
            normal advancement path; the error is still raised so the
            requester learns their track failed.  With nothing queued the
            session is torn down.
          * Voice join failure / private group — the voice chat itself is
            unusable, so queued tracks cannot play either.  The session is torn
            down and the chat is TOLD how many queued tracks were dropped.

        Stage log: [RESOLVE] [FFMPEG] [JOIN VC] [PLAY]
        """
        pending_error: Optional[Exception] = None
        dropped_notice: Optional[str] = None
        try:
            async with self._transition_lock(chat_id):
                if self.generation(chat_id) != gen:
                    return False

                # ── Stage: RESOLVE ────────────────────────────────────────
                logger.info(
                    "[CONTROLLER] [RESOLVE] Resolving stream  title='{}'",
                    track.title,
                )
                try:
                    stream = await self._build_guarded(chat_id, gen, track)
                except _Superseded:
                    return False
                except (StreamResolveTimeoutError, StreamResolveError) as exc:
                    logger.error(
                        "[CONTROLLER] Track unplayable ({})  title='{}'  "
                        "chat_id={}",
                        type(exc).__name__, track.title, chat_id,
                    )
                    if await self._session.is_empty(chat_id):
                        await self._teardown(chat_id, reason="resolver_failed")
                        raise
                    pending_error = exc     # advance to the queued tracks below
                else:
                    if self.generation(chat_id) != gen:
                        return False

                    # ── Stage: JOIN VC ────────────────────────────────────
                    logger.info(
                        "[CONTROLLER] [JOIN VC] Joining voice chat  chat_id={}",
                        chat_id,
                    )
                    try:
                        joined = await self._voice.play(chat_id, stream)
                    except PrivateGroupError:
                        dropped_notice = await self._voice_failure_teardown(
                            chat_id, "private_group")
                        raise

                    if self.generation(chat_id) != gen:
                        # /stop arrived during the join; it is waiting for this
                        # lock and will leave the voice chat right after we
                        # return.
                        return False

                    if not joined:
                        dropped_notice = await self._voice_failure_teardown(
                            chat_id, "vc_join_failed")
                        raise VoiceChatError(
                            "Could not join the voice chat. "
                            "Make sure a voice chat is active and the "
                            "assistant has permission to join."
                        )

                    # ── Stage: PLAY ───────────────────────────────────────
                    self._state.transition_to_playing(chat_id, track)
                    self._started_at[chat_id] = self._clock()
                    logger.info(
                        "[CONTROLLER] [PLAY] Playback started  title='{}'  "
                        "chat_id={}",
                        track.title, chat_id,
                    )
                    return True
        finally:
            # Telegram I/O happens after the transition lock is released.
            if dropped_notice is not None and self._notify is not None:
                try:
                    await self._notify(chat_id, dropped_notice)
                except Exception as exc:
                    logger.debug("[CONTROLLER] drop notice failed: {}", exc)

        # Resolver failure with tracks queued behind: start the next one.
        # The captured generation makes this a no-op if /stop (or a newer
        # session) intervened since the lock was released.
        if pending_error is None:       # defensive: every other path returned/raised
            return False
        logger.info(
            "[CONTROLLER] First track failed — advancing to queued tracks  "
            "chat_id={}", chat_id,
        )
        try:
            await self._advance(chat_id, reason="start_failed", gen=gen)
        except Exception as exc:
            # The requester must still learn why THEIR track failed.
            logger.error(
                "[CONTROLLER] Advance after failed start raised  chat_id={}  "
                "error={}", chat_id, exc,
            )
        raise pending_error

    async def _voice_failure_teardown(self, chat_id: int, reason: str) -> Optional[str]:
        """Tear down after a voice failure; return a notice if tracks were dropped."""
        dropped = len(await self._session.get_upcoming(chat_id))
        await self._teardown(chat_id, reason=reason)
        if dropped:
            return (
                f"⚠️ The voice chat could not be joined, so {dropped} queued "
                f"track{'s' if dropped != 1 else ''} could not be played and "
                "the queue was cleared."
            )
        return None

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
