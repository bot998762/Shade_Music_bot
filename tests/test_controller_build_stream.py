"""
tests.test_controller_build_stream
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests the no-fallback guarantee in _build_stream() and exception hierarchy.

The _build_stream() logic is tested by importing only app.playback.controller
after fully stubbing all heavy deps (pyrogram, pytgcalls, yt_dlp).
The exception hierarchy tests need only app.shared.exceptions.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Complete dep stubs (must be done before any app import) ───────────────────
def _stub(name):
    m = types.ModuleType(name)
    m.logger = MagicMock()  # type: ignore
    return m

for _n in ["loguru"]:
    sys.modules.setdefault(_n, _stub(_n))

# media.py uses MediaStream.Flags.IGNORE — give it a proper stub
_media_flags = MagicMock()
_media_flags.IGNORE = object()

_MediaStream = MagicMock
_MediaStream.Flags = _media_flags

_AudioQuality = MagicMock()
_AudioQuality.HIGH = object()

# pytgcalls stubs
for _n in ["pytgcalls", "pytgcalls.types", "pytgcalls.types.stream", "pytgcalls.filters"]:
    sys.modules.setdefault(_n, _stub(_n))

sys.modules["pytgcalls"].PyTgCalls = MagicMock  # type: ignore
sys.modules["pytgcalls.types"].MediaStream = _MediaStream  # type: ignore
sys.modules["pytgcalls.types"].AudioQuality = _AudioQuality  # type: ignore
sys.modules["pytgcalls.types"].ChatUpdate = MagicMock()  # type: ignore
sys.modules["pytgcalls.types"].Update = MagicMock  # type: ignore
sys.modules["pytgcalls.types.stream"].StreamEnded = MagicMock  # type: ignore
sys.modules["pytgcalls.filters"].chat_update = MagicMock()  # type: ignore

# pyrogram stubs
for _n in ["pyrogram", "pyrogram.errors"]:
    sys.modules.setdefault(_n, _stub(_n))

sys.modules["pyrogram"].Client = type("Client", (), {})  # type: ignore
sys.modules["pyrogram"].filters = MagicMock()  # type: ignore
sys.modules["pyrogram.errors"].UserAlreadyParticipant = type(  # type: ignore
    "UserAlreadyParticipant", (Exception,), {}
)

# yt_dlp stub
for _n in ["yt_dlp", "yt_dlp.utils"]:
    sys.modules.setdefault(_n, _stub(_n))

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.shared.exceptions import StreamResolveError, StreamResolveTimeoutError


def _make_track():
    from app.search.models import SearchResult, Track
    return Track.from_search_result(
        SearchResult("Track", 180, "https://www.youtube.com/watch?v=test", "Artist"),
        1, "User",
    )


async def _run_build_stream(resolver_return=None, resolver_raises=None):
    """
    Invoke _build_stream() on a minimal controller object.

    Bypasses PlaybackController.__init__ with __new__ + manual attribute set.
    Only _resolver and _cookies are needed by _build_stream().
    """
    from app.playback.controller import PlaybackController

    ctrl = PlaybackController.__new__(PlaybackController)
    mock_resolver = MagicMock()
    if resolver_raises is not None:
        mock_resolver.resolve = AsyncMock(side_effect=resolver_raises)
    else:
        mock_resolver.resolve = AsyncMock(return_value=resolver_return)

    ctrl._resolver = mock_resolver
    ctrl._cookies  = None
    return await ctrl._build_stream(_make_track())


class TestBuildStream(unittest.IsolatedAsyncioTestCase):

    async def test_success_calls_build_from_url_not_fallback(self) -> None:
        cdn = "https://rr1.googlevideo.com/audio?e=1234"
        fake_stream = MagicMock()

        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_url",
                   return_value=fake_stream) as mock_url, \
             patch("app.playback.controller.FFmpegStreamBuilder.build_from_youtube") as mock_yt:
            result = await _run_build_stream(resolver_return=cdn)

        mock_url.assert_called_once_with(cdn)
        mock_yt.assert_not_called()
        self.assertIs(result, fake_stream)

    async def test_resolver_none_raises_StreamResolveError(self) -> None:
        """Resolver returns None → StreamResolveError; build_from_youtube NOT called."""
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_youtube") as mock_yt:
            with self.assertRaises(StreamResolveError):
                await _run_build_stream(resolver_return=None)

        # OOM-prevention guarantee: the ntgcalls fallback must NEVER be called.
        mock_yt.assert_not_called()

    async def test_resolver_timeout_propagates_no_fallback(self) -> None:
        with patch("app.playback.controller.FFmpegStreamBuilder.build_from_youtube") as mock_yt:
            with self.assertRaises(StreamResolveTimeoutError):
                await _run_build_stream(resolver_raises=StreamResolveTimeoutError("t/o"))

        mock_yt.assert_not_called()

    async def test_no_fallback_for_all_failure_modes(self) -> None:
        scenarios = [
            ("None return",       None, None),
            ("timeout exc",       None, StreamResolveTimeoutError("t/o")),
            ("resolve error exc", None, StreamResolveError("fail")),
        ]
        for label, ret, exc in scenarios:
            with patch("app.playback.controller.FFmpegStreamBuilder.build_from_youtube") as mock_yt:
                try:
                    await _run_build_stream(resolver_return=ret, resolver_raises=exc)
                except StreamResolveError:
                    pass

            self.assertEqual(mock_yt.call_count, 0,
                             f"build_from_youtube unexpectedly called: {label}")


class TestExceptionHierarchy(unittest.TestCase):

    def test_timeout_is_resolve_error(self) -> None:
        self.assertIsInstance(StreamResolveTimeoutError("x"), StreamResolveError)

    def test_base_not_timeout(self) -> None:
        self.assertNotIsInstance(StreamResolveError("x"), StreamResolveTimeoutError)

    def test_resolve_error_not_voice_chat_error(self) -> None:
        from app.shared.exceptions import VoiceChatError
        self.assertFalse(issubclass(StreamResolveError, VoiceChatError))

    def test_handler_catch_order_correct(self) -> None:
        caught = []
        try:
            raise StreamResolveTimeoutError("timed out")
        except StreamResolveTimeoutError:
            caught.append("timeout")
        except StreamResolveError:
            caught.append("generic")
        self.assertEqual(caught, ["timeout"])

    def test_wrong_order_misclassifies_timeout(self) -> None:
        caught = []
        try:
            raise StreamResolveTimeoutError("timed out")
        except StreamResolveError:       # wrong — catches timeout too
            caught.append("generic")
        except StreamResolveTimeoutError:
            caught.append("timeout")
        self.assertEqual(caught, ["generic"])


if __name__ == "__main__":
    unittest.main()
