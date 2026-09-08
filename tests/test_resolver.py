"""
tests.test_resolver
~~~~~~~~~~~~~~~~~~~
Unit tests for app.search.resolver — the subprocess-based stream resolver.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

# ── Stub missing prod deps for the test sandbox ───────────────────────────────
# (loguru is not pip-installable in this environment; all others are indirect)
for _mod_name in ["loguru"]:
    if _mod_name not in sys.modules:
        _m = types.ModuleType(_mod_name)
        _m.logger = MagicMock()  # type: ignore[attr-defined]
        sys.modules[_mod_name] = _m

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.shared.exceptions import StreamResolveError, StreamResolveTimeoutError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_process(
    returncode: int = 0,
    stdout: bytes = b"https://rr1.googlevideo.com/fake_audio",
    stderr: bytes = b"",
    raise_on_communicate: Optional[Exception] = None,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None

    async def _communicate():
        if raise_on_communicate is not None:
            raise raise_on_communicate
        proc.returncode = returncode
        return stdout, stderr

    proc.communicate = _communicate
    proc.kill = MagicMock()
    return proc


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStreamResolverSubprocess(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        import importlib
        import app.search.resolver as resolver_mod
        importlib.reload(resolver_mod)
        self.resolver_mod = resolver_mod
        self.Resolver = resolver_mod.StreamResolver

    async def test_success_returns_http_url(self) -> None:
        proc = _fake_process(
            returncode=0,
            stdout=b"https://rr3.googlevideo.com/audio/stream?expire=9999\n",
        )
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://"))

    async def test_nonzero_exit_returns_none(self) -> None:
        proc = _fake_process(
            returncode=1,
            stdout=b"",
            stderr=b"ERROR: [Youtube] test: Requested format is not available.",
        )
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")
        self.assertIsNone(url)

    async def test_empty_stdout_returns_none(self) -> None:
        proc = _fake_process(returncode=0, stdout=b"\n\n")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")
        self.assertIsNone(url)

    async def test_non_http_stdout_returns_none(self) -> None:
        proc = _fake_process(returncode=0, stdout=b"NA\n")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")
        self.assertIsNone(url)

    async def test_first_http_line_taken(self) -> None:
        proc = _fake_process(
            returncode=0,
            stdout=b"https://first.googlevideo.com/\nhttps://second.googlevideo.com/\n",
        )
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")
        self.assertEqual(url, "https://first.googlevideo.com/")

    async def test_cancelled_error_kills_process_group(self) -> None:
        """CancelledError during communicate() triggers _kill_proc_group()."""
        proc = _fake_process(raise_on_communicate=asyncio.CancelledError())

        killed = []

        def _fake_killpg(pgid, sig):
            killed.append((pgid, sig))
            proc.returncode = -15

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
             patch("os.killpg", side_effect=_fake_killpg):
            resolver = self.Resolver()
            with self.assertRaises(asyncio.CancelledError):
                await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")

        self.assertTrue(len(killed) > 0, "killpg should have been called on CancelledError")

    async def test_resolve_timeout_raises_StreamResolveTimeoutError(self) -> None:
        async def _slow_communicate():
            await asyncio.sleep(999)
            return b"", b""

        proc = MagicMock()
        proc.pid = 99
        proc.returncode = None
        proc.communicate = _slow_communicate
        proc.kill = MagicMock()

        killed = []
        def _fake_killpg(pgid, sig):
            killed.append((pgid, sig))
            proc.returncode = -15

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)), \
             patch("os.killpg", side_effect=_fake_killpg), \
             patch.object(self.resolver_mod, "STREAM_RESOLVE_TIMEOUT_SEC", 0.01):
            resolver = self.Resolver()
            with self.assertRaises(StreamResolveTimeoutError):
                await resolver.resolve("https://www.youtube.com/watch?v=test")

    async def test_resolve_success_returns_url(self) -> None:
        expected = "https://rr5.googlevideo.com/audio?expire=9999"
        proc = _fake_process(returncode=0, stdout=expected.encode() + b"\n")

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver.resolve("https://www.youtube.com/watch?v=test")
        self.assertEqual(url, expected)

    async def test_resolve_failure_returns_none(self) -> None:
        proc = _fake_process(returncode=1, stdout=b"", stderr=b"some error")

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            resolver = self.Resolver()
            url = await resolver.resolve("https://www.youtube.com/watch?v=test")
        self.assertIsNone(url)

    async def test_cookies_path_plumbed_into_command(self) -> None:
        cookies = "/tmp/cookies.txt"
        captured_cmd = []

        async def _capture_cmd(*args, **kwargs):
            captured_cmd.extend(args)
            return _fake_process(returncode=0, stdout=b"https://ok.googlevideo.com/\n")

        with patch("asyncio.create_subprocess_exec", side_effect=_capture_cmd), \
             patch("os.path.isfile", return_value=True):
            resolver = self.Resolver(cookies_path=cookies)
            await resolver.resolve("https://www.youtube.com/watch?v=test")

        cmd_str = " ".join(str(x) for x in captured_cmd)
        self.assertIn("--cookies", cmd_str)
        self.assertIn(cookies, cmd_str)

    async def test_shutdown_is_noop(self) -> None:
        self.Resolver.shutdown()  # must not raise

    async def test_semaphore_serialises_concurrent_calls(self) -> None:
        """Two concurrent resolve() calls must run sequentially (semaphore=1)."""
        call_order = []
        call_count = [0]

        async def _slow_proc_factory(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            call_order.append(f"start_{idx}")

            proc = MagicMock()
            proc.pid = 1000 + idx
            proc.returncode = None
            proc.kill = MagicMock()

            async def _communicate():
                await asyncio.sleep(0.02)
                call_order.append(f"end_{idx}")
                proc.returncode = 0
                return b"https://cdn.example.com/\n", b""

            proc.communicate = _communicate
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_slow_proc_factory):
            resolver = self.Resolver()
            results = await asyncio.gather(
                resolver.resolve("https://www.youtube.com/watch?v=AAA"),
                resolver.resolve("https://www.youtube.com/watch?v=BBB"),
            )

        self.assertEqual(len(results), 2)
        # Semaphore ensures: start_0 → end_0 → start_1 → end_1
        self.assertEqual(call_order, ["start_0", "end_0", "start_1", "end_1"],
                         f"Expected sequential execution but got: {call_order}")


class TestExceptionHierarchy(unittest.TestCase):

    def test_timeout_is_subclass_of_resolve_error(self) -> None:
        self.assertTrue(issubclass(StreamResolveTimeoutError, StreamResolveError))

    def test_resolve_error_is_not_subclass_of_voice_chat_error(self) -> None:
        from app.shared.exceptions import VoiceChatError
        self.assertFalse(issubclass(StreamResolveError, VoiceChatError))

    def test_no_results_is_not_subclass_of_resolve_error(self) -> None:
        from app.shared.exceptions import NoResultsError
        self.assertFalse(issubclass(NoResultsError, StreamResolveError))


if __name__ == "__main__":
    unittest.main()
