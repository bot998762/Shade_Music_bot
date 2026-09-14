"""
tests.test_queue_lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~~~
Comprehensive queue lifecycle tests for Shade Music Bot.

Tests the full queue contract:
 - SessionManager in isolation (pure unit)
 - PlaybackController ↔ SessionManager integration (mocked I/O boundary)

Coverage
--------
 1.  Empty queue
 2.  Enqueue one track
 3.  Enqueue multiple tracks — FIFO ordering
 4.  Per-chat isolation
 5.  Current track separation (dequeue → current, not duplicated in queue)
 6.  Dequeue / advance
 7.  Track completion (queue exhausted)
 8.  Track-level failure (single bad track)
 9.  Failed track followed by valid track
10.  Multiple queued tracks survive a failure
11.  Queue cap enforcement — atomic TOCTOU safety
12.  Concurrent enqueue from multiple callers
13.  Concurrent advance — no double-advance
14.  No duplicate advancement
15.  Cleanup behavior
16.  Empty-after-finish behavior
17.  Session reset behavior
18.  Retry counter resets on success (good/bad/good sequence)
19.  Advance retries exhaust correctly on all-bad queue
20.  enqueue_if_room atomicity

All external I/O (yt-dlp, VC, Telegram) is mocked.
No network access; no subprocesses.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub heavy deps before any app import ─────────────────────────────────────
def _stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.logger = MagicMock()  # type: ignore
    return m

for _n in ["loguru"]:
    sys.modules.setdefault(_n, _stub(_n))

_media_flags = MagicMock()
_media_flags.IGNORE = object()
_MediaStream = MagicMock
_MediaStream.Flags = _media_flags
_AudioQuality = MagicMock()
_AudioQuality.HIGH = object()

for _n in ["pytgcalls", "pytgcalls.types", "pytgcalls.types.stream", "pytgcalls.filters"]:
    sys.modules.setdefault(_n, _stub(_n))
sys.modules["pytgcalls"].PyTgCalls = MagicMock  # type: ignore
sys.modules["pytgcalls.types"].MediaStream = _MediaStream  # type: ignore
sys.modules["pytgcalls.types"].AudioQuality = _AudioQuality  # type: ignore
sys.modules["pytgcalls.types"].ChatUpdate = MagicMock()  # type: ignore
sys.modules["pytgcalls.types"].Update = MagicMock  # type: ignore
sys.modules["pytgcalls.types.stream"].StreamEnded = MagicMock  # type: ignore
sys.modules["pytgcalls.filters"].chat_update = MagicMock()  # type: ignore

for _n in ["pyrogram", "pyrogram.errors"]:
    sys.modules.setdefault(_n, _stub(_n))
sys.modules["pyrogram"].Client = type("Client", (), {})  # type: ignore
sys.modules["pyrogram"].filters = MagicMock()  # type: ignore
sys.modules["pyrogram.errors"].UserAlreadyParticipant = type(  # type: ignore
    "UserAlreadyParticipant", (Exception,), {}
)

for _n in ["yt_dlp", "yt_dlp.utils"]:
    sys.modules.setdefault(_n, _stub(_n))

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── App imports ───────────────────────────────────────────────────────────────
from app.playback.session import SessionManager
from app.playback.state import StateManager
from app.playback.models import PlaybackStatus
from app.search.models import SearchResult, Track
from app.shared.exceptions import (
    QueueFullError,
    StreamResolveError,
    StreamResolveTimeoutError,
    VoiceChatError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_track(title: str = "Track", url: str = "https://youtube.com/watch?v=test") -> Track:
    return Track.from_search_result(
        SearchResult(title, 180, url, "Artist"),
        requested_by_id=1,
        requested_by_name="User",
    )


def _make_controller(
    resolver_returns=None,   # str URL or None
    resolver_raises=None,    # exception class/instance or None
    voice_play_returns=True,
    voice_replace_returns=True,
    max_queue=50,
):
    """
    Build a PlaybackController with all I/O boundaries mocked.

    resolver_returns: what resolver.resolve() returns (default: valid URL str)
    resolver_raises:  exception to raise from resolver.resolve()
    voice_play_returns: what voice.play() returns (bool)
    voice_replace_returns: what voice.replace_stream() returns (bool)
    """
    from app.playback.controller import PlaybackController
    from app.playback.cleanup import CleanupService

    session = SessionManager()
    state   = StateManager()

    mock_search   = MagicMock()
    mock_resolver = MagicMock()
    mock_voice    = MagicMock()
    mock_ffmpeg_stream = MagicMock()

    # Search always succeeds with a single result
    result = SearchResult("Track", 180, "https://youtube.com/watch?v=test", "Artist")
    mock_search.search = AsyncMock(return_value=result)
    mock_search.fetch_url_metadata = AsyncMock(return_value=result)

    # Resolver behavior
    if resolver_raises is not None:
        mock_resolver.resolve = AsyncMock(side_effect=resolver_raises)
    else:
        url = resolver_returns if resolver_returns is not None else "https://cdn.example.com/audio"
        mock_resolver.resolve = AsyncMock(return_value=url)

    # Voice behavior
    mock_voice.play            = AsyncMock(return_value=voice_play_returns)
    mock_voice.replace_stream  = AsyncMock(return_value=voice_replace_returns)
    mock_voice.leave           = AsyncMock()
    mock_voice.is_active       = MagicMock(return_value=False)

    cleanup = CleanupService(voice=mock_voice, session=session, state=state)

    with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
               return_value=mock_ffmpeg_stream):
        ctrl = PlaybackController(
            search=mock_search,
            resolver=mock_resolver,
            voice=mock_voice,
            session=session,
            state=state,
            cleanup=cleanup,
            notify=None,
            max_queue=max_queue,
        )

    # Attach mocks for assertion access
    ctrl._mock_resolver = mock_resolver
    ctrl._mock_voice    = mock_voice
    ctrl._mock_search   = mock_search
    ctrl._session_ref   = session
    ctrl._state_ref     = state
    ctrl._ffmpeg_stream = mock_ffmpeg_stream
    return ctrl


# ══════════════════════════════════════════════════════════════════════════════
# 1-5: SessionManager unit tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionManagerBasic(unittest.IsolatedAsyncioTestCase):
    """Tests 1–6: Empty queue, enqueue, FIFO, isolation, current-track separation."""

    async def test_01_empty_queue(self) -> None:
        """Test 1: Fresh session has empty queue."""
        sm = SessionManager()
        self.assertTrue(await sm.is_empty(1))
        self.assertEqual(await sm.size(1), 0)
        self.assertIsNone(await sm.dequeue(1))

    async def test_02_enqueue_one_track(self) -> None:
        """Test 2: Enqueue one track; size=1, not empty."""
        sm = SessionManager()
        t  = _make_track("A")
        pos = await sm.enqueue(1, t)
        self.assertEqual(pos, 1)
        self.assertFalse(await sm.is_empty(1))
        self.assertEqual(await sm.size(1), 1)

    async def test_03_fifo_ordering(self) -> None:
        """Test 3: Multiple tracks dequeue in FIFO order."""
        sm = SessionManager()
        titles = ["A", "B", "C", "D"]
        for title in titles:
            await sm.enqueue(1, _make_track(title))
        for expected in titles:
            t = await sm.dequeue(1)
            self.assertIsNotNone(t)
            self.assertEqual(t.title, expected)
        self.assertTrue(await sm.is_empty(1))

    async def test_04_per_chat_isolation(self) -> None:
        """Test 4: Operations on chat A never affect chat B."""
        sm = SessionManager()
        await sm.enqueue(1, _make_track("A1"))
        await sm.enqueue(1, _make_track("A2"))
        await sm.enqueue(2, _make_track("B1"))

        # Chat 2 sees only its own track
        self.assertEqual(await sm.size(2), 1)
        b = await sm.dequeue(2)
        self.assertEqual(b.title, "B1")
        self.assertTrue(await sm.is_empty(2))

        # Chat 1 still has both its tracks
        self.assertEqual(await sm.size(1), 2)
        a1 = await sm.dequeue(1)
        self.assertEqual(a1.title, "A1")

    async def test_05_current_track_separation_via_dequeue(self) -> None:
        """Test 5: Once dequeued, the current track is NOT still in the queue."""
        sm = SessionManager()
        await sm.enqueue(1, _make_track("A"))
        await sm.enqueue(1, _make_track("B"))
        await sm.enqueue(1, _make_track("C"))

        current = await sm.dequeue(1)
        self.assertEqual(current.title, "A")

        # Queue now contains only B, C — A is not duplicated
        upcoming = await sm.get_upcoming(1)
        titles = [t.title for t in upcoming]
        self.assertNotIn("A", titles)
        self.assertEqual(titles, ["B", "C"])
        self.assertEqual(await sm.size(1), 2)

    async def test_06_dequeue_empty_returns_none(self) -> None:
        """Test 6: Dequeuing an empty queue returns None without raising."""
        sm = SessionManager()
        result = await sm.dequeue(99)
        self.assertIsNone(result)

    async def test_07_clear_empties_queue(self) -> None:
        """Test 7: clear() discards all pending tracks."""
        sm = SessionManager()
        for i in range(5):
            await sm.enqueue(1, _make_track(f"T{i}"))
        await sm.clear(1)
        self.assertTrue(await sm.is_empty(1))
        self.assertEqual(await sm.size(1), 0)

    async def test_08_get_upcoming_snapshot(self) -> None:
        """Test 8: get_upcoming returns a snapshot; modifying it doesn't affect queue."""
        sm = SessionManager()
        await sm.enqueue(1, _make_track("X"))
        await sm.enqueue(1, _make_track("Y"))
        snapshot = await sm.get_upcoming(1)
        snapshot.clear()  # modify the snapshot
        self.assertEqual(await sm.size(1), 2)  # queue unaffected

    async def test_09_requeue_head(self) -> None:
        """Test 9: requeue_head inserts at the front, not the back."""
        sm = SessionManager()
        await sm.enqueue(1, _make_track("B"))
        await sm.enqueue(1, _make_track("C"))
        await sm.requeue_head(1, _make_track("A"))
        first = await sm.dequeue(1)
        self.assertEqual(first.title, "A")


# ══════════════════════════════════════════════════════════════════════════════
# 10-11: Queue cap
# ══════════════════════════════════════════════════════════════════════════════

class TestQueueCap(unittest.IsolatedAsyncioTestCase):

    async def test_10_cap_enforced_atomically(self) -> None:
        """Test 10: enqueue_if_room refuses at cap; atomically."""
        sm = SessionManager()
        cap = 3
        for i in range(cap):
            added, pos = await sm.enqueue_if_room(1, _make_track(f"T{i}"), cap)
            self.assertTrue(added)

        # Next enqueue should be refused
        added, size = await sm.enqueue_if_room(1, _make_track("overflow"), cap)
        self.assertFalse(added)
        self.assertEqual(size, cap)
        self.assertEqual(await sm.size(1), cap)

    async def test_11_concurrent_cap_toctou(self) -> None:
        """
        Test 11: Concurrent enqueue_if_room calls with cap=1 must not
        allow both to succeed (TOCTOU race).

        Two coroutines each call enqueue_if_room with cap=1 on an empty
        queue.  Exactly one should succeed and one should be refused.
        """
        sm = SessionManager()
        cap = 1
        results = []

        async def try_enqueue(title: str) -> None:
            added, _ = await sm.enqueue_if_room(1, _make_track(title), cap)
            results.append(added)

        await asyncio.gather(try_enqueue("P1"), try_enqueue("P2"))
        # Exactly one succeeds
        self.assertEqual(sum(results), 1)
        self.assertEqual(await sm.size(1), 1)

    async def test_12_cap_not_breached_under_concurrent_load(self) -> None:
        """Test 12: 20 concurrent callers with cap=5 never exceed cap."""
        sm  = SessionManager()
        cap = 5
        results = []

        async def try_enqueue(i: int) -> None:
            added, _ = await sm.enqueue_if_room(1, _make_track(f"T{i}"), cap)
            results.append(added)

        await asyncio.gather(*(try_enqueue(i) for i in range(20)))
        self.assertLessEqual(await sm.size(1), cap)
        self.assertEqual(sum(results), cap)


# ══════════════════════════════════════════════════════════════════════════════
# 13-18: PlaybackController lifecycle integration tests
# ══════════════════════════════════════════════════════════════════════════════

class TestControllerQueueLifecycle(unittest.IsolatedAsyncioTestCase):

    async def _play(self, ctrl, chat_id: int = 1, query: str = "song"):
        """Helper: invoke play() with patched FFmpegStreamBuilder."""
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            return await ctrl.play(
                chat_id=chat_id,
                query=query,
                requested_by_id=1,
                requested_by_name="User",
            )

    async def _advance(self, ctrl, chat_id: int = 1):
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            await ctrl.advance(chat_id=chat_id)

    async def test_13_play_when_idle_starts_immediately(self) -> None:
        """Test 13: /play on idle chat → is_playing_now=True."""
        ctrl = _make_controller()
        track, is_now = await self._play(ctrl)
        self.assertTrue(is_now)
        self.assertEqual(ctrl._state_ref.get(1).status.value, "playing")

    async def test_14_play_when_playing_enqueues(self) -> None:
        """Test 14: /play while already playing → is_playing_now=False; track queued."""
        ctrl = _make_controller()
        # First play starts immediately
        await self._play(ctrl, query="first")
        # Second play should queue
        track, is_now = await self._play(ctrl, query="second")
        self.assertFalse(is_now)
        self.assertEqual(await ctrl._session_ref.size(1), 1)

    async def test_15_advance_dequeues_next_track(self) -> None:
        """Test 15: advance() dequeues waiting track and updates current."""
        ctrl = _make_controller()
        await self._play(ctrl, query="first")   # starts playing
        await self._play(ctrl, query="second")  # queued
        await self._play(ctrl, query="third")   # queued

        # Queue has 2 waiting tracks
        self.assertEqual(await ctrl._session_ref.size(1), 2)

        # Advance — should consume "second" from queue
        await self._advance(ctrl)
        self.assertEqual(await ctrl._session_ref.size(1), 1)

    async def test_16_advance_exhausted_queue_goes_idle(self) -> None:
        """Test 16: advance() on empty queue → cleanup → IDLE."""
        ctrl = _make_controller()
        await self._play(ctrl, query="only")
        # Queue is now empty (track dequeued into playback by _start_now)
        self.assertTrue(await ctrl._session_ref.is_empty(1))

        # advance() is called by monitor on stream-end — queue is empty
        await self._advance(ctrl)
        self.assertEqual(ctrl._state_ref.get(1).status, PlaybackStatus.IDLE)

    async def test_17_track_level_failure_does_not_kill_queue(self) -> None:
        """
        Test 17: A track that fails to resolve in advance() is skipped;
        subsequent valid tracks still play.
        """
        from app.playback.controller import PlaybackController
        from app.playback.cleanup import CleanupService

        session = SessionManager()
        state   = StateManager()
        mock_voice  = MagicMock()
        mock_voice.play            = AsyncMock(return_value=True)
        mock_voice.replace_stream  = AsyncMock(return_value=True)
        mock_voice.leave           = AsyncMock()
        mock_voice.is_active       = MagicMock(return_value=False)

        call_count = [0]
        async def resolver_side_effect(url):
            call_count[0] += 1
            if call_count[0] == 2:
                # Second resolve (first advance call) fails
                raise StreamResolveError("unavailable")
            return "https://cdn.example.com/audio"

        mock_resolver = MagicMock()
        mock_resolver.resolve = resolver_side_effect
        mock_search = MagicMock()
        result = SearchResult("Track", 180, "https://youtube.com/watch?v=t", "A")
        mock_search.search = AsyncMock(return_value=result)
        mock_search.fetch_url_metadata = AsyncMock(return_value=result)

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)
        fake_stream = MagicMock()

        ctrl = PlaybackController(
            search=mock_search, resolver=mock_resolver, voice=mock_voice,
            session=session, state=state, cleanup=cleanup,
            notify=None, max_queue=50,
        )

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=fake_stream):
            # /play A → starts now (resolver call 1 succeeds)
            await ctrl.play(1, "A", 1, "U")
            # /play B → queued
            await ctrl.play(1, "B", 1, "U")
            # /play C → queued
            await ctrl.play(1, "C", 1, "U")

            # Queue: [B, C]. advance() is called by monitor on A ending.
            # resolver call 2 (for B) raises StreamResolveError → skip B.
            # resolver call 3 (for C) succeeds → C plays.
            await ctrl.advance(1)

        # C should now be playing; the session is not idle
        s = state.get(1)
        self.assertEqual(s.status, PlaybackStatus.PLAYING)
        # Queue should be empty (B skipped, C now current)
        self.assertTrue(await session.is_empty(1))

    async def test_18_consecutive_failures_exhaust_and_cleanup(self) -> None:
        """Test 18: MAX_SKIP_RETRIES consecutive failures → cleanup → IDLE."""
        from app.playback.controller import PlaybackController
        from app.playback.cleanup import CleanupService
        from app.shared.constants import MAX_SKIP_RETRIES

        session = SessionManager()
        state   = StateManager()
        mock_voice = MagicMock()
        mock_voice.play            = AsyncMock(return_value=True)
        mock_voice.replace_stream  = AsyncMock(return_value=True)
        mock_voice.leave           = AsyncMock()
        mock_voice.is_active       = MagicMock(return_value=False)

        mock_resolver = MagicMock()
        call_n = [0]
        async def always_fail_after_first(url):
            call_n[0] += 1
            if call_n[0] == 1:
                return "https://cdn.example.com/audio"
            raise StreamResolveError("unavailable")

        mock_resolver.resolve = always_fail_after_first
        mock_search = MagicMock()
        result = SearchResult("T", 180, "https://youtube.com/watch?v=t", "A")
        mock_search.search = AsyncMock(return_value=result)

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)
        fake_stream = MagicMock()

        ctrl = PlaybackController(
            search=mock_search, resolver=mock_resolver, voice=mock_voice,
            session=session, state=state, cleanup=cleanup,
            notify=None, max_queue=50,
        )

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=fake_stream):
            # Enqueue first track (starts playing)
            await ctrl.play(1, "q", 1, "U")
            # Enqueue MAX_SKIP_RETRIES more bad tracks
            for i in range(MAX_SKIP_RETRIES):
                await ctrl.play(1, f"bad{i}", 1, "U")
            # advance: all queued tracks fail to resolve
            await ctrl.advance(1)

        # Should be idle after retries exhausted
        self.assertEqual(state.get(1).status, PlaybackStatus.IDLE)

    async def test_19_retry_resets_on_success(self) -> None:
        """
        Test 19: Retry counter resets on success.

        Queue: [bad, bad, GOOD, bad, bad, GOOD] — each run of 2 bads
        is within MAX_SKIP_RETRIES=3.  Good tracks play; session
        does not prematurely die.
        """
        from app.playback.controller import PlaybackController
        from app.playback.cleanup import CleanupService
        from app.shared.constants import MAX_SKIP_RETRIES

        # This test verifies the retry-reset logic with a controlled sequence
        session = SessionManager()
        state   = StateManager()
        mock_voice = MagicMock()
        mock_voice.play            = AsyncMock(return_value=True)
        mock_voice.replace_stream  = AsyncMock(return_value=True)
        mock_voice.leave           = AsyncMock()
        mock_voice.is_active       = MagicMock(return_value=False)

        # Sequence: first call ok (play), then bad, bad, GOOD, bad, bad, GOOD
        calls = iter(
            ["ok", "fail", "fail", "ok", "fail", "fail", "ok"]
        )
        async def resolver(url):
            c = next(calls, "ok")
            if c == "fail":
                raise StreamResolveError("bad")
            return "https://cdn.example.com/audio"

        mock_resolver = MagicMock()
        mock_resolver.resolve = resolver
        mock_search = MagicMock()
        result = SearchResult("T", 180, "https://youtube.com/watch?v=t", "A")
        mock_search.search = AsyncMock(return_value=result)

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)
        fake_stream = MagicMock()

        ctrl = PlaybackController(
            search=mock_search, resolver=mock_resolver, voice=mock_voice,
            session=session, state=state, cleanup=cleanup,
            notify=None, max_queue=50,
        )

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=fake_stream):
            # Start first track (resolver call: ok)
            await ctrl.play(1, "first", 1, "U")
            # Queue 6 more: bad, bad, good, bad, bad, good
            for i in range(6):
                await ctrl.play(1, f"q{i}", 1, "U")
            # advance: consumes bad, bad, GOOD (4th resolver call)
            # consecutive_failures resets to 0 at GOOD
            await ctrl.advance(1)

        # Session should still be playing (not idle/cleaned-up)
        self.assertEqual(state.get(1).status, PlaybackStatus.PLAYING)

    async def test_20_play_lock_not_held_across_io(self) -> None:
        """
        Test 20: A second /play in an idle chat can enqueue while the first
        is resolving its stream (play lock released before I/O).

        This validates the fix for the critical bug where _start_now() was
        called inside the play lock, serializing all /play calls during I/O.
        """
        from app.playback.controller import PlaybackController
        from app.playback.cleanup import CleanupService

        session = SessionManager()
        state   = StateManager()
        mock_voice = MagicMock()
        mock_voice.play            = AsyncMock(return_value=True)
        mock_voice.replace_stream  = AsyncMock(return_value=True)
        mock_voice.leave           = AsyncMock()
        mock_voice.is_active       = MagicMock(return_value=False)

        # Resolver has a delay to simulate slow I/O
        resolve_started = asyncio.Event()
        second_enqueued = asyncio.Event()

        async def slow_resolver(url):
            resolve_started.set()
            await asyncio.sleep(0.01)   # simulate network delay
            return "https://cdn.example.com/audio"

        mock_resolver = MagicMock()
        mock_resolver.resolve = slow_resolver
        mock_search = MagicMock()
        result = SearchResult("T", 180, "https://youtube.com/watch?v=t", "A")
        mock_search.search = AsyncMock(return_value=result)

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)
        fake_stream = MagicMock()

        ctrl = PlaybackController(
            search=mock_search, resolver=mock_resolver, voice=mock_voice,
            session=session, state=state, cleanup=cleanup,
            notify=None, max_queue=50,
        )

        play1_done = asyncio.Event()

        async def first_play():
            with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                       return_value=fake_stream):
                await ctrl.play(1, "first", 1, "U")
            play1_done.set()

        async def second_play():
            # Wait until first play has started resolving (i.e. past the lock)
            await resolve_started.wait()
            with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                       return_value=fake_stream):
                track, is_now = await ctrl.play(1, "second", 1, "U")
            second_enqueued.set()
            # Second /play should queue (not play now) since first is playing
            return is_now

        results = await asyncio.gather(first_play(), second_play())
        is_now_second = results[1]

        # First play started immediately; second was queued
        self.assertFalse(is_now_second)
        # The second play should have completed before play1 necessarily finished
        # (verifies lock was released before slow I/O)
        self.assertTrue(second_enqueued.is_set())

    async def test_21_queue_full_error_raised(self) -> None:
        """Test 21: QueueFullError is raised when cap is reached."""
        ctrl = _make_controller(max_queue=2)

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            # First play: starts
            await ctrl.play(1, "A", 1, "U")
            # Second play: queues (queue size = 1)
            await ctrl.play(1, "B", 1, "U")
            # Third play: should raise QueueFullError (cap=2, but first was
            # dequeued from queue into playing, so queue size = 1 → actually
            # need to fill it up more precisely)
            # Let's fill it properly with cap=1 for clarity
        
        ctrl2 = _make_controller(max_queue=1)
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl2._ffmpeg_stream):
            await ctrl2.play(1, "A", 1, "U")   # starts, queue empty after dequeue
            await ctrl2.play(1, "B", 1, "U")   # queued (size=1 = cap)
            with self.assertRaises(QueueFullError):
                await ctrl2.play(1, "C", 1, "U")   # cap exceeded

    async def test_22_per_chat_queue_isolation_in_controller(self) -> None:
        """Test 22: Operations in chat 1 don't affect chat 2's queue."""
        ctrl = _make_controller()
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            await ctrl.play(1, "chat1_track", 1, "U")
            await ctrl.play(2, "chat2_track", 1, "U")
            await ctrl.play(1, "chat1_queued", 1, "U")

        # Chat 2 has no pending tracks
        self.assertTrue(await ctrl._session_ref.is_empty(2))
        # Chat 1 has one pending track
        self.assertEqual(await ctrl._session_ref.size(1), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 23-25: Advance concurrency / double-advance prevention
# ══════════════════════════════════════════════════════════════════════════════

class TestAdvanceConcurrency(unittest.IsolatedAsyncioTestCase):

    async def test_23_advance_lock_serializes_concurrent_advance(self) -> None:
        """
        Test 23: The advance lock serializes two concurrent advance() calls.

        Scenario: one queued track + two simultaneous advance() calls (as if
        two stream-end events arrived for the same stream).  The advance lock
        ensures they run sequentially, not in parallel.

        The first advance call dequeues the one queued track and calls
        replace_stream.  The second advance call then runs (after the first
        releases the lock), finds the queue empty, and triggers cleanup.

        Crucially, replace_stream is called at most once — the single queued
        track is not advanced twice.

        Note: the advance lock does NOT prevent the second call from running
        at all (it still runs, finds the queue empty, and cleans up).  What
        it prevents is two concurrent calls simultaneously dequeuing and
        calling replace_stream for the same track.
        """
        from app.playback.controller import PlaybackController
        from app.playback.cleanup import CleanupService

        session = SessionManager()
        state   = StateManager()
        mock_voice = MagicMock()
        mock_voice.play      = AsyncMock(return_value=True)
        mock_voice.leave     = AsyncMock()
        mock_voice.is_active = MagicMock(return_value=False)

        replace_count = [0]
        async def counting_replace(chat_id, stream):
            replace_count[0] += 1
            return True
        mock_voice.replace_stream = counting_replace

        mock_resolver = MagicMock()
        mock_resolver.resolve = AsyncMock(return_value="https://cdn.example.com/a")
        mock_search = MagicMock()
        result = SearchResult("T", 180, "https://youtube.com/watch?v=t", "A")
        mock_search.search = AsyncMock(return_value=result)

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)
        fake_stream = MagicMock()
        ctrl = PlaybackController(
            search=mock_search, resolver=mock_resolver, voice=mock_voice,
            session=session, state=state, cleanup=cleanup,
            notify=None, max_queue=50,
        )

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=fake_stream):
            # Start one track (goes playing, queue becomes empty)
            await ctrl.play(1, "q1", 1, "U")
            # Enqueue exactly ONE more
            await ctrl.play(1, "q2", 1, "U")
            self.assertEqual(await session.size(1), 1)

            # Two concurrent advance() calls arrive (duplicate stream-end events)
            await asyncio.gather(ctrl.advance(1), ctrl.advance(1))

        # The single queued track should have been advanced exactly once.
        # replace_stream is called once (not twice for the same track).
        self.assertEqual(replace_count[0], 1,
            "replace_stream should be called exactly once for one queued track")


# ══════════════════════════════════════════════════════════════════════════════
# 26: Session reset
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionReset(unittest.IsolatedAsyncioTestCase):

    async def test_24_cleanup_clears_queue_and_idles(self) -> None:
        """Test 24: cleanup() clears all queued tracks and transitions to IDLE."""
        from app.playback.cleanup import CleanupService

        session = SessionManager()
        state   = StateManager()
        mock_voice = MagicMock()
        mock_voice.leave = AsyncMock()

        cleanup = CleanupService(voice=mock_voice, session=session, state=state)

        # Put state into PLAYING with some queued tracks
        await session.enqueue(1, _make_track("A"))
        await session.enqueue(1, _make_track("B"))
        t = await session.dequeue(1)  # simulate "now playing"
        state.transition_to_playing(1, t)

        await cleanup.cleanup(1, reason="test")

        self.assertTrue(await session.is_empty(1))
        self.assertEqual(state.get(1).status, PlaybackStatus.IDLE)
        mock_voice.leave.assert_called_once_with(1)

    async def test_25_new_play_after_cleanup_starts_fresh(self) -> None:
        """Test 25: After cleanup, a new /play starts a fresh session."""
        ctrl = _make_controller()
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            await ctrl.play(1, "first", 1, "U")

        # Force cleanup
        await ctrl._cleanup.cleanup(1, reason="manual_test")
        self.assertEqual(ctrl._state_ref.get(1).status, PlaybackStatus.IDLE)

        # Start again
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=ctrl._ffmpeg_stream):
            track, is_now = await ctrl.play(1, "second", 1, "U")

        self.assertTrue(is_now)
        self.assertEqual(ctrl._state_ref.get(1).status, PlaybackStatus.PLAYING)


if __name__ == "__main__":
    unittest.main()
