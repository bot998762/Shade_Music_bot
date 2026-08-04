"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
Thin, testable wrapper around py-tgcalls PyTgCalls.

py-tgcalls 2.x compatibility
------------------------------
All APIs verified against:
  • pytgcalls/pytgcalls master branch (2025-08-05)
  • AsmSafone/MusicPlayer/main.py (production reference)

Confirmed removals (were present in pre-2.x, removed in 2.0+):
  REMOVED: on_stream_end()         → on_update() + isinstance(update, StreamEnded)
  REMOVED: on_kicked()             → on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
  REMOVED: on_closed_voice_chat()  → on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
  REMOVED: .pause()                → .pause_stream()
  REMOVED: .resume()               → .resume_stream()

All method names verified in AsmSafone/MusicPlayer main.py:
  pytgcalls.play(chat_id, stream)
  pytgcalls.change_stream(chat_id, stream)
  pytgcalls.leave_call(chat_id)
  pytgcalls.pause_stream(chat_id)
  pytgcalls.resume_stream(chat_id)

Event types verified:
  from pytgcalls.types.stream import StreamEnded   (current — NOT StreamAudioEnded)
  from pytgcalls.types import ChatUpdate
  from pytgcalls import filters as fl
  fl.chat_update(ChatUpdate.Status.LEFT_CALL)      covers kicked + VC-closed + left

Exception handling:
  No named exceptions imported from pytgcalls.exceptions — those names have
  changed across minor versions.  All call-sites catch bare Exception and
  inspect the message string where the error type matters.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate, MediaStream, Update
from pytgcalls.types.stream import StreamEnded

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]

# MTProto error substrings that mean "no active voice chat in this group".
_NO_CALL_PHRASES = (
    "no_active_group_call",
    "groupcall_not_found",
    "not_found",
    "no active",
)


def _is_no_active_call(exc: Exception) -> bool:
    """Return True when *exc* signals that no voice chat is running."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _NO_CALL_PHRASES)


class VoiceChatManager:
    """
    Manages voice-chat connections for all active chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (bot or user assistant).
    """

    def __init__(self, client: Client) -> None:
        self._tgcalls = PyTgCalls(client)
        self._active: Set[int] = set()
        self._on_stream_end: Optional[StreamEndCallback] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_on_stream_end(self, callback: StreamEndCallback) -> None:
        """
        Register the coroutine awaited when a stream finishes naturally.

        Called by the lifecycle layer AFTER MusicEngine is created, breaking
        the circular dependency (engine -> vc_manager -> engine).
        """
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register internal callbacks."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("PyTgCalls engine started")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Join the voice chat and start streaming.

        Returns True on success, False when no active voice chat exists.
        If already connected, falls back to change_stream() automatically.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play  chat_id={}", chat_id)
            return True

        except Exception as exc:
            if _is_no_active_call(exc):
                logger.warning(
                    "No active voice chat in group  chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

            # Any other error (e.g. "already in call") — try change_stream.
            logger.debug(
                "play() raised, retrying via change_stream  chat_id={}  error={}",
                chat_id, exc,
            )
            return await self.change_stream(chat_id, stream)

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).
        Falls back to a fresh play() if the bot is not connected.
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            self._active.add(chat_id)
            logger.debug("Stream changed  chat_id={}", chat_id)
            return True

        except Exception as exc:
            if _is_no_active_call(exc):
                logger.warning(
                    "No active VC during change_stream  chat_id={}  error={}",
                    chat_id, exc,
                )
                self._active.discard(chat_id)
                return False

            logger.warning(
                "change_stream failed, attempting fresh join  "
                "chat_id={}  error={}",
                chat_id, exc,
            )
            try:
                await self._tgcalls.play(chat_id, stream)
                self._active.add(chat_id)
                return True
            except Exception as exc2:
                logger.error(
                    "Fresh join after change_stream failure also failed  "
                    "chat_id={}  error={}",
                    chat_id, exc2,
                )
                self._active.discard(chat_id)
                return False

    async def leave(self, chat_id: int) -> None:
        """Leave the voice chat and remove from the active set."""
        await self._safe_leave(chat_id)
        self._active.discard(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """
        Pause the stream.

        Calls pause_stream() — the correct 2.x method name.
        (The old .pause() method was removed in py-tgcalls 2.x.)
        """
        try:
            await self._tgcalls.pause_stream(chat_id)
            return True
        except Exception as exc:
            logger.error("pause_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    async def resume(self, chat_id: int) -> bool:
        """
        Resume a paused stream.

        Calls resume_stream() — the correct 2.x method name.
        (The old .resume() method was removed in py-tgcalls 2.x.)
        """
        try:
            await self._tgcalls.resume_stream(chat_id)
            return True
        except Exception as exc:
            logger.error("resume_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    # ── State queries ─────────────────────────────────────────────────────────

    def is_active(self, chat_id: int) -> bool:
        return chat_id in self._active

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _safe_leave(self, chat_id: int) -> None:
        try:
            await self._tgcalls.leave_call(chat_id)
            logger.info("Left VC  chat_id={}", chat_id)
        except Exception as exc:
            logger.debug("leave_call suppressed  chat_id={}  error={}", chat_id, exc)

    def _register_callbacks(self) -> None:
        """
        Attach py-tgcalls 2.x event handlers.

        Handler 1 — stream-end  (@on_update, no filter)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        on_update() with no filter receives ALL update types.  We use
        isinstance(update, StreamEnded) to filter to stream-end events only.
        StreamEnded is imported from pytgcalls.types.stream (not StreamAudioEnded,
        which is the older name — verified in AsmSafone/MusicPlayer main.py 2025).

        Handler 2 — VC left / kicked / closed  (@on_update with fl.chat_update)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        fl.chat_update(ChatUpdate.Status.LEFT_CALL) fires when the bot is
        kicked from a VC, the VC is closed by an admin, or the bot leaves.
        This replaces the old on_kicked(), on_closed_voice_chat(), on_left()
        decorators — all removed in py-tgcalls 2.x.
        Verified in AsmSafone/MusicPlayer main.py (2025).
        """

        # ── Handler 1: stream ended naturally ─────────────────────────────
        @self._tgcalls.on_update()
        async def _on_stream_end(_client: PyTgCalls, update: Update) -> None:
            # on_update() receives ALL events; filter to StreamEnded only.
            if not isinstance(update, StreamEnded):
                return
            chat_id: int = update.chat_id
            logger.info("Stream ended naturally  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_stream_end callback raised  chat_id={}  error={}",
                        chat_id, exc,
                    )

        # ── Handler 2: bot left / kicked / VC closed ──────────────────────
        @self._tgcalls.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _on_left(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("Bot left/kicked/VC-closed  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_left callback raised  chat_id={}  error={}",
                        chat_id, exc,
                    )

        logger.debug(
            "PyTgCalls handlers registered: on_update(StreamEnded) + "
            "on_update(fl.chat_update(LEFT_CALL))"
        )
