"""
tests.test_resolver
~~~~~~~~~~~~~~~~~~~
Unit tests for app.search.resolver — the subprocess-based stream resolver.

Tests verify:
  - Successful yt-dlp exit → returns URL string
  - Non-zero yt-dlp exit → returns None (no exception)
  - Timeout → kills subprocess process group + raises StreamResolveTimeoutError
  - CancelledError → kills subprocess + re-raises
  - Empty/non-http stdout → returns None
  - Only tv_embedded player client in command (not mweb/web — Deno hang fix)
  - Cookies path plumbing
  - Concurrency gate (semaphore limits to 1 simultaneous call)
  - shutdown() is a no-op

All yt-dlp subprocess calls are mocked — no network access.
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

    # ── Critical: player client selection (Deno-hang fix) ────────────────────

    async def test_command_uses_tv_embedded_not_mweb_or_web(self) -> None:
        """
        The subprocess command MUST use tv_embedded and NOT mweb or web.

        mweb/web trigger Deno PO token generation.  On Render free tier,
        Deno JIT compilation on cold start takes > 30 s, causing consistent
        StreamResolveTimeoutError.  tv_embedded returns stream URLs without
        PO tokens — Deno is never invoked.
        """
        captured_cmd: list[str] = []

        async def _capture(*args, **kwargs):
            captured_cmd.extend(args)
            return _fake_process(
                returncode=0,
                stdout=b"https://rr1.googlevideo.com/audio\n",
            )

        with patch("asyncio.create_subprocess_exec", side_effect=_capture):
            resolver = self.Resolver()
            await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")

        cmd_str = " ".join(str(x) for x in captured_cmd)

        # tv_embedded must be present
        self.assertIn("tv_embedded", cmd_str,
                      "tv_embedded must be in the yt-dlp command")

        # mweb and web must be absent — they invoke Deno and hang on Render
        self.assertNotIn("mweb", cmd_str,
                         "mweb must NOT appear — it invokes Deno and hangs on Render")
        # Note: "web" is a substring of "tv_embedded", so check the extractor-args value
        extractor_arg_idx = captured_cmd.index("--extractor-args") + 1 \
            if "--extractor-args" in captured_cmd else -1
        if extractor_arg_idx > 0:
            extractor_val = captured_cmd[extractor_arg_idx]
            # Should be exactly "youtube:player_client=tv_embedded"
            # Must not contain bare "web" or "mweb" as clients
            self.assertNotIn("mweb", extractor_val)
            clients_part = extractor_val.split("player_client=")[-1] if "player_client=" in extractor_val else ""
            client_list = [c.strip() for c in clients_part.split(",")]
            for c in client_list:
                self.assertNotEqual(c, "web",
                    "web client must NOT be used — it invokes Deno and hangs on Render")
                self.assertNotEqual(c, "mweb",
                    "mweb client must NOT be used — it invokes Deno and hangs on Render")

    async def test_command_has_correct_format_selector(self) -> None:
        """Format selector must be the explicit bestaudio/best chain."""
        captured_cmd: list[str] = []

        async def _capture(*args, **kwargs):
            captured_cmd.extend(args)
            return _fake_process(returncode=0, stdout=b"https://cdn.example.com/\n")

        with patch("asyncio.create_subprocess_exec", side_effect=_capture):
            resolver = self.Resolver()
            await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")

        self.assertIn("--format", captured_cmd)
        fmt_idx = captured_cmd.index("--format") + 1
        fmt = captured_cmd[fmt_idx]
        self.assertIn("bestaudio", fmt)
        self.assertIn("best", fmt)

    async def test_command_uses_print_url(self) -> None:
        """--print url must be in the command for clean URL-only stdout."""
        captured_cmd: list[str] = []

        async def _capture(*args, **kwargs):
            captured_cmd.extend(args)
            return _fake_process(returncode=0, stdout=b"https://cdn.example.com/\n")

        with patch("asyncio.create_subprocess_exec", side_effect=_capture):
            resolver = self.Resolver()
            await resolver._resolve_subprocess("https://www.youtube.com/watch?v=test")

        self.assertIn("--print", captured_cmd)
        print_idx = captured_cmd.index("--print") + 1
        self.assertEqual(captured_cmd[print_idx], "url")

    # ── Core subprocess behaviour ─────────────────────────────────────────────

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
            stderr=b"ERROR: [Youtube] test: Video unavailable",
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

        self.assertTrue(len(killed) > 0,
                        "killpg should have been called on CancelledError")

    # ── Public resolve() API ──────────────────────────────────────────────────

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

    # ── Concurrency gate ──────────────────────────────────────────────────────

    async def test_semaphore_serialises_concurrent_calls(self) -> None:
        """Two concurrent resolve() calls run sequentially (semaphore value=1)."""
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
        # Semaphore must force sequential execution: start_0 → end_0 → start_1 → end_1
        self.assertEqual(call_order, ["start_0", "end_0", "start_1", "end_1"],
                         f"Expected sequential (semaphore=1) but got: {call_order}")


class TestExceptionHierarchy(unittest.TestCase):
    """Verify exception subclass relationships used in handler catch order."""

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
