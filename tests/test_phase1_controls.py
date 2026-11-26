"""
tests.test_phase1_controls
~~~~~~~~~~~~~~~~~~~~~~~~~~
Phase 1: queue cap (10 upcoming), generation tokens, unified advancement,
/skip /stop /end /leave /queue /nowplaying.

Classification
  UNIT TESTED: everything here, with Telegram, yt-dlp, PyTgCalls and the
  resolver MOCKED.  Concurrency is driven by asyncio events, never by sleeps.
  NOT TESTED: real Telegram, real voice chat, real py-tgcalls behaviour.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import types

from tests import test_playback_integration as base  # installs dependency stubs

if "pyrogram.types" not in sys.modules:
    _pt = types.ModuleType("pyrogram.types")
    _pt.Message = MagicMock  # type: ignore[attr-defined]
    sys.modules["pyrogram.types"] = _pt

from app.handlers import controls
from app.playback.controller import PlaybackController, SkipOutcome
from app.search.models import SearchResult
from app.shared.constants import DEFAULT_MAX_QUEUE_SIZE, MAX_UPCOMING_HARD_CAP, clamp_queue_cap
from app.shared.exceptions import PlaybackCancelledError, QueueFullError

BUILD = "app.playback.controller.FFmpegStreamBuilder.build_from_url"


def _result(title: str) -> SearchResult:
    return SearchResult(title=title, duration=100, webpage_url=f"https://www.youtube.com/watch?v={title[:11]:_<11}",
                        uploader="U")


def make(max_queue: int = 10):
    ctrl, _ = base._make_controller_and_deps(max_queue=max_queue)
    ctrl._test_search.search = AsyncMock(side_effect=lambda q: _result(q))
    return ctrl


async def play(ctrl, q, chat=1):
    with patch(BUILD, return_value=MagicMock()):
        return await ctrl.play(chat, q, 1, "User")


async def until(pred):
    """Yield to the event loop until *pred()* holds (no timing assumptions)."""
    while not pred():
        await asyncio.sleep(0)


def run(coro):
    """Run a test coroutine; a deadlock fails after 10 s instead of hanging the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout=10))


class QueueCap(unittest.TestCase):
    def test_production_cap_is_exactly_10(self):
        self.assertEqual(DEFAULT_MAX_QUEUE_SIZE, 10)
        self.assertEqual(MAX_UPCOMING_HARD_CAP, 10)
        self.assertEqual(make().max_queue, 10)

    def test_no_override_raises_cap_above_10(self):
        for v, want in ((50, 10), (11, 10), (10, 10), (3, 3), (1, 1), (0, 1), (-5, 1), ("7", 7), ("x", 10), (None, 10)):
            self.assertEqual(clamp_queue_cap(v), want, v)
        self.assertEqual(make(max_queue=500).max_queue, 10)
        self.assertEqual(make(max_queue=3).max_queue, 3)   # lowering stays possible (tests)

    def test_settings_validator_clamps(self):
        try:
            from app.infrastructure.config import Settings
        except Exception as exc:  # pydantic-settings not installed in sandbox
            self.skipTest(f"Settings not importable here: {exc}")
        self.assertEqual(Settings.clamp_max_queue(50), 10)

    def test_idle_first_track_does_not_consume_capacity(self):
        async def go():
            c = make()
            _, now = await play(c, "first")
            self.assertTrue(now)
            self.assertEqual((await c.snapshot(1)).upcoming, [])
            for i in range(10):
                _, now = await play(c, f"q{i}")
                self.assertFalse(now)
            with self.assertRaises(QueueFullError):
                await play(c, "eleventh")
            snap = await c.snapshot(1)
            self.assertEqual(len(snap.upcoming), 10)
            self.assertEqual(snap.current.title, "first")       # 1 playing + 10 upcoming
        run(go())

    def test_full_queue_never_drops_existing_tracks(self):
        async def go():
            c = make()
            await play(c, "cur")
            for i in range(10):
                await play(c, f"q{i}")
            with self.assertRaises(QueueFullError):
                await play(c, "extra")
            self.assertEqual([t.title for t in (await c.snapshot(1)).upcoming], [f"q{i}" for i in range(10)])
        run(go())

    def test_concurrent_enqueue_at_idle_chat_never_exceeds_cap(self):
        async def go():
            c = make()
            with patch(BUILD, return_value=MagicMock()):
                res = await asyncio.gather(*[c.play(1, f"t{i}", 1, "U") for i in range(40)], return_exceptions=True)
            ok = [r for r in res if not isinstance(r, Exception)]
            full = [r for r in res if isinstance(r, QueueFullError)]
            self.assertEqual(len(ok), 11)                        # 1 playing + 10 upcoming
            self.assertEqual(len(full), 29)
            self.assertEqual(sum(1 for _, now in ok if now), 1)
            self.assertEqual(len((await c.snapshot(1)).upcoming), 10)
        run(go())

    def test_concurrent_enqueue_while_playing_never_exceeds_cap(self):
        async def go():
            c = make()
            await play(c, "cur")
            with patch(BUILD, return_value=MagicMock()):
                res = await asyncio.gather(*[c.play(1, f"t{i}", 1, "U") for i in range(40)], return_exceptions=True)
            self.assertEqual(sum(isinstance(r, QueueFullError) for r in res), 30)
            self.assertEqual(len((await c.snapshot(1)).upcoming), 10)
        run(go())

    def test_chats_are_isolated(self):
        async def go():
            c = make()
            await play(c, "a-cur", chat=1)
            for i in range(10):
                await play(c, f"a{i}", chat=1)
            _, now = await play(c, "b-cur", chat=2)
            self.assertTrue(now)
            await play(c, "b1", chat=2)
            self.assertEqual(len((await c.snapshot(1)).upcoming), 10)
            self.assertEqual([t.title for t in (await c.snapshot(2)).upcoming], ["b1"])
            await c.stop(1)
            self.assertFalse((await c.snapshot(2)).is_idle)
        run(go())

    def test_fifo_order(self):
        async def go():
            c = make()
            await play(c, "cur")
            for n in "abc":
                await play(c, n)
            order = []
            for _ in range(3):
                r = await c.skip(1)
                order.append(r.track.title)
            self.assertEqual(order, ["a", "b", "c"])
        run(go())


class Skip(unittest.TestCase):
    def test_skip_idle(self):
        async def go():
            r = await make().skip(1)
            self.assertIs(r.outcome, SkipOutcome.NOTHING_PLAYING)
        run(go())

    def test_skip_advances_exactly_one(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b"):
                await play(c, n)
            with patch(BUILD, return_value=MagicMock()):
                r = await c.skip(1)
            self.assertIs(r.outcome, SkipOutcome.SKIPPED)
            snap = await c.snapshot(1)
            self.assertEqual(snap.current.title, "a")
            self.assertEqual([t.title for t in snap.upcoming], ["b"])
            self.assertEqual(c._test_voice.replace_stream.await_count, 1)
        run(go())

    def test_skip_with_empty_queue_ends_session(self):
        async def go():
            c = make()
            await play(c, "only")
            r = await c.skip(1)
            self.assertIs(r.outcome, SkipOutcome.QUEUE_ENDED)
            self.assertTrue((await c.snapshot(1)).is_idle)
            c._test_voice.leave.assert_awaited()
        run(go())

    def test_skip_failures_end_session_without_hiding_it(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b", "c", "d"):
                await play(c, n)
            c._test_voice.replace_stream = AsyncMock(return_value=False)
            with patch(BUILD, return_value=MagicMock()):
                r = await c.skip(1)
            self.assertIs(r.outcome, SkipOutcome.FAILED)
            self.assertTrue((await c.snapshot(1)).is_idle)
        run(go())

    def test_two_simultaneous_skips_advance_once(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b", "c"):
                await play(c, n)
            with patch(BUILD, return_value=MagicMock()):
                r = await asyncio.gather(c.skip(1), c.skip(1))
            self.assertEqual(sorted(x.outcome.value for x in r), ["duplicate", "skipped"])
            snap = await c.snapshot(1)
            self.assertEqual(snap.current.title, "a")
            self.assertEqual(len(snap.upcoming), 2)
        run(go())

    def test_skip_and_natural_end_together_advance_once(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b", "c"):
                await play(c, n)
            with patch(BUILD, return_value=MagicMock()):
                await asyncio.gather(c.skip(1), c.advance(1))
                snap = await c.snapshot(1)
                self.assertEqual(snap.current.title, "a")
                self.assertEqual(len(snap.upcoming), 2)
                self.assertEqual(c._test_voice.replace_stream.await_count, 1)
                # natural end first, then skip, both racing the same lock
                await asyncio.gather(c.advance(1), c.skip(1))
            snap = await c.snapshot(1)
            self.assertEqual(snap.current.title, "b")
            self.assertEqual(len(snap.upcoming), 1)
        run(go())

    def test_sequential_skips_each_advance(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b"):
                await play(c, n)
            with patch(BUILD, return_value=MagicMock()):
                await c.skip(1)
                await c.skip(1)
            self.assertEqual((await c.snapshot(1)).current.title, "b")
        run(go())


class StopAndStale(unittest.TestCase):
    def test_stop_idle_returns_false(self):
        async def go():
            self.assertFalse(await make().stop(1))
        run(go())

    def test_stop_clears_everything_and_leaves(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b"):
                await play(c, n)
            g = c.generation(1)
            self.assertTrue(await c.stop(1))
            snap = await c.snapshot(1)
            self.assertTrue(snap.is_idle)
            self.assertEqual(snap.upcoming, [])
            c._test_voice.leave.assert_awaited()
            self.assertGreater(c.generation(1), g)
        run(go())

    def test_play_after_stop_starts_new_session(self):
        async def go():
            c = make()
            await play(c, "one")
            await c.stop(1)
            _, now = await play(c, "two")
            self.assertTrue(now)
        run(go())

    def test_late_natural_end_after_stop_does_nothing(self):
        async def go():
            c = make()
            for n in ("cur", "a"):
                await play(c, n)
            await c.stop(1)
            leaves = c._test_voice.leave.await_count
            await c.advance(1)
            self.assertEqual(c._test_voice.replace_stream.await_count, 0)
            self.assertEqual(c._test_voice.leave.await_count, leaves)
            self.assertTrue((await c.snapshot(1)).is_idle)
        run(go())

    def test_stop_during_resolution_cancels_and_installs_nothing(self):
        async def go():
            c = make()
            entered, gate, cancelled = asyncio.Event(), asyncio.Event(), []

            async def resolve(url):
                entered.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise
                return base._FAKE_CDN
            c._test_resolver.resolve = resolve
            with patch(BUILD, return_value=MagicMock()):
                t = asyncio.ensure_future(c.play(1, "slow", 1, "U"))
                await entered.wait()
                self.assertTrue(await c.stop(1))
                with self.assertRaises(PlaybackCancelledError):
                    await t
            self.assertEqual(cancelled, [True])                  # resolver cancelled (kills yt-dlp)
            c._test_voice.play.assert_not_awaited()
            self.assertTrue((await c.snapshot(1)).is_idle)
        run(go())

    def test_stop_during_stream_installation_discards_it(self):
        async def go():
            c = make()
            entered, gate, order = asyncio.Event(), asyncio.Event(), []

            async def vplay(chat, stream):
                entered.set()
                await gate.wait()
                order.append("play_returned")
                return True

            async def vleave(chat):
                order.append("leave")
            c._test_voice.play = vplay
            c._test_voice.leave = vleave
            with patch(BUILD, return_value=MagicMock()):
                t = asyncio.ensure_future(c.play(1, "x", 1, "U"))
                await entered.wait()
                g = c.generation(1)
                s = asyncio.ensure_future(c.stop(1))
                await until(lambda: c.generation(1) != g)    # stop has invalidated the session
                gate.set()
                with self.assertRaises(PlaybackCancelledError):
                    await t
                await s
            self.assertEqual(order, ["play_returned", "leave"])  # stream torn down AFTER install returned
            self.assertTrue((await c.snapshot(1)).is_idle)
        run(go())

    def test_stop_during_skip_resolution(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b"):
                await play(c, n)
            entered, gate = asyncio.Event(), asyncio.Event()

            async def resolve(url):
                entered.set()
                await gate.wait()
                return base._FAKE_CDN
            c._test_resolver.resolve = resolve
            with patch(BUILD, return_value=MagicMock()):
                sk = asyncio.ensure_future(c.skip(1))
                await entered.wait()
                await c.stop(1)
                r = await sk
            self.assertIs(r.outcome, SkipOutcome.CANCELLED)
            c._test_voice.replace_stream.assert_not_awaited()
            snap = await c.snapshot(1)
            self.assertTrue(snap.is_idle)
            self.assertEqual(snap.upcoming, [])
        run(go())

    def test_stop_during_skip_stream_replacement(self):
        async def go():
            c = make()
            for n in ("cur", "a"):
                await play(c, n)
            entered, gate, order = asyncio.Event(), asyncio.Event(), []

            async def repl(chat, stream):
                entered.set()
                await gate.wait()
                order.append("replace_returned")
                return True

            async def vleave(chat):
                order.append("leave")
            c._test_voice.replace_stream = repl
            c._test_voice.leave = vleave
            with patch(BUILD, return_value=MagicMock()):
                sk = asyncio.ensure_future(c.skip(1))
                await entered.wait()
                g = c.generation(1)
                st = asyncio.ensure_future(c.stop(1))
                await until(lambda: c.generation(1) != g)
                gate.set()
                r = await sk
                await st
            self.assertIs(r.outcome, SkipOutcome.CANCELLED)
            self.assertEqual(order, ["replace_returned", "leave"])
            self.assertTrue((await c.snapshot(1)).is_idle)
        run(go())

    def test_stop_cancels_only_its_own_chat(self):
        async def go():
            c = make()
            await play(c, "a", chat=1)
            await play(c, "b", chat=2)
            await c.stop(1)
            self.assertTrue((await c.snapshot(1)).is_idle)
            self.assertFalse((await c.snapshot(2)).is_idle)
        run(go())


class StaleStreamEnd(unittest.TestCase):
    """
    py-tgcalls' update carries only chat_id (no track identity), so a stream-end
    that arrives within STALE_END_WINDOW_SEC of a new stream being installed is
    ignored.  The clock is injected — no sleeps.
    """

    def _ctrl(self):
        c = make()
        c._now = [100.0]
        c._clock = lambda: c._now[0]
        return c

    def test_end_right_after_install_is_ignored(self):
        async def go():
            c = self._ctrl()
            for n in ("cur", "a", "b"):
                await play(c, n)
            c._now[0] += 1.0                      # 1 s after "cur" started
            await c.advance(1, "stream_ended")
            snap = await c.snapshot(1)
            self.assertEqual(snap.current.title, "cur")
            self.assertEqual(len(snap.upcoming), 2)
            c._test_voice.replace_stream.assert_not_awaited()
        run(go())

    def test_delayed_end_from_old_track_does_not_advance_new_track(self):
        async def go():
            c = self._ctrl()
            for n in ("cur", "a", "b"):
                await play(c, n)
            c._now[0] += 50
            with patch(BUILD, return_value=MagicMock()):
                await c.skip(1)                   # a starts now
            c._now[0] += 0.2                      # old stream's end arrives late
            await c.advance(1, "stream_ended")
            snap = await c.snapshot(1)
            self.assertEqual(snap.current.title, "a")
            self.assertEqual([t.title for t in snap.upcoming], ["b"])
        run(go())

    def test_genuine_end_after_window_advances(self):
        async def go():
            c = self._ctrl()
            for n in ("cur", "a"):
                await play(c, n)
            c._now[0] += 100
            with patch(BUILD, return_value=MagicMock()):
                await c.advance(1, "stream_ended")
            self.assertEqual((await c.snapshot(1)).current.title, "a")
        run(go())

    def test_vc_closed_and_kicked_are_never_filtered(self):
        async def go():
            for reason in ("vc_closed", "kicked"):
                c = self._ctrl()
                for n in ("cur", "a"):
                    await play(c, n)
                c._now[0] += 0.1
                with patch(BUILD, return_value=MagicMock()):
                    await c.advance(1, reason)
                self.assertEqual((await c.snapshot(1)).current.title, "a", reason)
        run(go())

    def test_window_never_exceeds_half_a_short_track(self):
        async def go():
            c = self._ctrl()
            c._test_search.search = AsyncMock(
                return_value=SearchResult("short", 2, "https://www.youtube.com/watch?v=shortshort1", "U"))
            await play(c, "short")
            await play(c, "next")
            c._now[0] += 1.5                      # > duration/2 (1 s): genuine for a 2 s track
            with patch(BUILD, return_value=MagicMock()):
                await c.advance(1, "stream_ended")
            self.assertTrue(c._test_voice.replace_stream.await_count == 1)
        run(go())

    def test_stale_event_does_not_consume_a_real_skip(self):
        async def go():
            c = self._ctrl()
            for n in ("cur", "a", "b"):
                await play(c, n)
            c._now[0] += 0.5
            await c.advance(1, "stream_ended")    # stale: ignored, epoch unchanged
            c._now[0] += 100
            with patch(BUILD, return_value=MagicMock()):
                r = await c.skip(1)
            self.assertIs(r.outcome, SkipOutcome.SKIPPED)
        run(go())

    def test_monitor_relays_reason(self):
        from app.playback.monitor import StreamMonitor
        async def go():
            ctrl = MagicMock()
            ctrl.advance = AsyncMock()
            await StreamMonitor(ctrl).on_stream_end(5, "kicked")
            ctrl.advance.assert_awaited_once_with(5, "kicked")
            ctrl.advance.reset_mock()
            await StreamMonitor(ctrl).on_stream_end(5)
            ctrl.advance.assert_awaited_once_with(5, "stream_ended")
        run(go())


class FailedIdleStart(unittest.TestCase):
    """A failed first track must not silently drop tracks queued behind it."""

    @staticmethod
    def _failing_first_resolve(c):
        entered, gate, calls = asyncio.Event(), asyncio.Event(), []

        async def resolve(url):
            calls.append(url)
            if len(calls) == 1:
                entered.set()
                await gate.wait()
                raise base.StreamResolveError("unavailable")
            return base._FAKE_CDN
        c._test_resolver.resolve = resolve
        return entered, gate

    def test_resolver_failure_keeps_and_plays_queued_tracks(self):
        async def go():
            c = make()
            entered, gate = self._failing_first_resolve(c)
            with patch(BUILD, return_value=MagicMock()):
                first = asyncio.ensure_future(c.play(1, "bad", 1, "U"))
                await entered.wait()
                await c.play(1, "q1", 2, "V")
                await c.play(1, "q2", 3, "W")
                gate.set()
                with self.assertRaises(base.StreamResolveError):
                    await first                        # requester still learns it failed
            snap = await c.snapshot(1)
            self.assertFalse(snap.is_idle)
            self.assertEqual(snap.current.title, "q1")
            self.assertEqual([t.title for t in snap.upcoming], ["q2"])
            c._test_voice.leave.assert_not_awaited()
        run(go())

    def test_resolver_failure_with_empty_queue_still_tears_down(self):
        async def go():
            c = make()
            c._test_resolver.resolve = AsyncMock(side_effect=base.StreamResolveError("x"))
            with self.assertRaises(base.StreamResolveError):
                await play(c, "bad")
            self.assertTrue((await c.snapshot(1)).is_idle)
            c._test_voice.leave.assert_awaited()
        run(go())

    def test_every_queued_track_failing_ends_session_after_retries(self):
        async def go():
            c = make()
            c._test_resolver.resolve = AsyncMock(side_effect=base.StreamResolveError("x"))
            entered, gate = asyncio.Event(), asyncio.Event()

            calls = []

            async def resolve(url):
                calls.append(1)
                if len(calls) == 1:
                    entered.set()
                    await gate.wait()
                raise base.StreamResolveError("x")
            c._test_resolver.resolve = resolve
            with patch(BUILD, return_value=MagicMock()):
                first = asyncio.ensure_future(c.play(1, "bad", 1, "U"))
                await entered.wait()
                for i in range(5):
                    await c.play(1, f"q{i}", 2, "V")
                gate.set()
                with self.assertRaises(base.StreamResolveError):
                    await first
            self.assertTrue((await c.snapshot(1)).is_idle)     # same 3-failure policy as /skip
        run(go())

    def test_fallback_is_cancelled_if_session_was_superseded(self):
        async def go():
            c = make()
            await play(c, "cur")
            await play(c, "q")
            old = c.generation(1)
            await c.stop(1)
            await play(c, "new")                              # newer session
            r = await c._advance(1, reason="start_failed", gen=old)
            self.assertIs(r.outcome, SkipOutcome.CANCELLED)
            self.assertEqual((await c.snapshot(1)).current.title, "new")
        run(go())

    def test_voice_join_failure_tells_chat_how_many_were_dropped(self):
        async def go():
            c = make()
            c._notify = AsyncMock()
            entered, gate = asyncio.Event(), asyncio.Event()

            async def vplay(chat, stream):
                entered.set()
                await gate.wait()
                return False
            c._test_voice.play = vplay
            with patch(BUILD, return_value=MagicMock()):
                first = asyncio.ensure_future(c.play(1, "a", 1, "U"))
                await entered.wait()
                await c.play(1, "q1", 2, "V")
                await c.play(1, "q2", 3, "W")
                gate.set()
                with self.assertRaises(base.VoiceChatError):
                    await first
            self.assertTrue((await c.snapshot(1)).is_idle)
            c._notify.assert_awaited_once()
            self.assertIn("2 queued tracks", c._notify.await_args.args[1])
        run(go())

    def test_voice_failure_without_queue_sends_no_notice(self):
        async def go():
            c = make()
            c._notify = AsyncMock()
            c._test_voice.play = AsyncMock(return_value=False)
            with self.assertRaises(base.VoiceChatError):
                await play(c, "a")
            c._notify.assert_not_awaited()
        run(go())

    def test_private_group_also_reports_dropped_tracks(self):
        from app.shared.exceptions import PrivateGroupError
        async def go():
            c = make()
            c._notify = AsyncMock()
            entered, gate = asyncio.Event(), asyncio.Event()

            async def vplay(chat, stream):
                entered.set()
                await gate.wait()
                raise PrivateGroupError("private")
            c._test_voice.play = vplay
            with patch(BUILD, return_value=MagicMock()):
                first = asyncio.ensure_future(c.play(1, "a", 1, "U"))
                await entered.wait()
                await c.play(1, "q1", 2, "V")
                gate.set()
                with self.assertRaises(PrivateGroupError):
                    await first
            self.assertIn("1 queued track ", c._notify.await_args.args[1])
        run(go())


class Formatting(unittest.TestCase):
    def test_queue_excludes_current(self):
        async def go():
            c = make()
            for n in ("cur", "a", "b"):
                await play(c, n)
            txt = controls.format_queue(await c.snapshot(1))
            self.assertIn("2/10", txt)
            self.assertNotIn("cur", txt)
            self.assertIn("1. **a**", txt)
        run(go())

    def test_queue_and_nowplaying_idle_and_empty(self):
        async def go():
            c = make()
            self.assertIn("Nothing", controls.format_queue(await c.snapshot(1)))
            self.assertIn("Nothing", controls.format_now_playing(await c.snapshot(1)))
            await play(c, "cur")
            self.assertIn("empty", controls.format_queue(await c.snapshot(1)))
            self.assertIn("cur", controls.format_now_playing(await c.snapshot(1)))
        run(go())

    def test_every_skip_outcome_has_text(self):
        for o in SkipOutcome:
            self.assertTrue(controls.format_skip(o))


class HandlerWiring(unittest.TestCase):
    """Handlers must only call the controller (mocked)."""

    def _register(self):
        handlers = {}

        class FakeClient:
            def on_message(self, flt):
                def deco(fn):
                    handlers[fn.__name__] = fn
                    return fn
                return deco
        ctrl = MagicMock()
        ctrl.skip = AsyncMock(return_value=MagicMock(outcome=SkipOutcome.DUPLICATE, track=None))
        ctrl.stop = AsyncMock(return_value=True)
        ctrl.snapshot = AsyncMock()
        with patch.object(controls.filters, "command") as cmd:
            controls.register(FakeClient(), ctrl)
        return handlers, ctrl, cmd

    def test_stop_end_leave_are_one_handler(self):
        handlers, _, cmd = self._register()
        self.assertIn((["stop", "end", "leave"],), [c.args for c in cmd.call_args_list])
        self.assertEqual(sorted(handlers), ["cmd_nowplaying", "cmd_queue", "cmd_skip", "cmd_stop"])

    def test_no_pause_resume_handlers_yet(self):
        handlers, _, cmd = self._register()
        flat = str(cmd.call_args_list)
        self.assertNotIn("pause", flat)
        self.assertNotIn("resume", flat)

    def test_skip_and_stop_call_controller(self):
        async def go():
            handlers, ctrl, _ = self._register()
            msg = MagicMock()
            msg.chat.id = 7
            msg.reply_text = AsyncMock()
            await handlers["cmd_skip"](None, msg)
            ctrl.skip.assert_awaited_once_with(7)
            await handlers["cmd_stop"](None, msg)
            ctrl.stop.assert_awaited_once_with(7)
        run(go())


if __name__ == "__main__":
    unittest.main()
