"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
VoiceChatManager — thin, testable wrapper around pytgcalls PyTgCalls.

API Reference (pytgcalls >= 0.9.x, the ntgcalls-backed stable branch)
----------------------------------------------------------------------
Package name on PyPI  : pytgcalls          (NOT "py-tgcalls")
Import name           : pytgcalls          (unchanged)
Backend               : ntgcalls           (pre-built wheels, no compilation)

Verified API surface (pytgcalls >= 0.9.0):
    from pytgcalls import PyTgCalls
    from pytgcalls.types import MediaStream, AudioQuality, AudioParameters, Update
    from pytgcalls.types import ChatUpdate, StreamAudioEnded
    from pytgcalls import filters as fl

    client = PyTgCalls(pyrogram_client)
    await client.start()
    await client.play(chat_id, MediaStream(...))
    await client.change_stream(chat_id, MediaStream(...))
    await client.leave_call(chat_id)
    await client.pause_stream(chat_id)
    await client.resume_stream(chat_id)

Event handler registration (pytgcalls >= 0.9):
    @client.on_update()                                        # all events
    @client.on_update(fl.chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT))

Event types to filter on:
    StreamAudioEnded   — audio track finished naturally
    ChatUpdate         — VC lifecycle events (closed, kicked, etc.)

Breaking changes vs prior (py-tgcalls 2.x) naming:
    StreamEnded        → StreamAudioEnded   (import path changed)
    ChatUpdate.Status.LEFT_CALL → ChatUpdate.Status.CLOSED_VOICE_CHAT
    MediaStream.Flags.IGNORE    → MediaStream.Flags.IGNORE  (same, kept)

This module is the ONLY place in the codebase that imports pytgcalls.
All other modules depend on VoiceChatManager's typed interface, making
future library upgrades a one-file change.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import (
    AudioQuality,
    ChatUpdate,
    MediaStream,
    Update,
)
from pytgcalls.types import StreamAudioEnded

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]

# MTProto error substrings that mean "no active voice chat in this group".
_NO_CALL_PHRASES = (
    "no_active_group_call",
    "groupcall_not_found",
    "not_found",
    "no active",
    "chat not found",
)

# Errors that mean we are already in the call.
_ALREADY_IN_PHRASES = (
    "already",
    "in_call",
    "joined",
    "already_joined",
)


def _is_no_active_call(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _NO_CALL_PHRASES)


def _is_already_in_call(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _ALREADY_IN_PHRASES)


class VoiceChatManager:
    """
    Manages voice-chat connections for all active group chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (user assistant — NOT a bot account).
        A bot account cannot produce audio in Telegram voice chats without
        special Manage Voice Chats admin permission, and even then Telegram
        servers often reject audio from bots. Always use a user account.

    Design notes
    ------------
    * This class is the sole consumer of the pytgcalls library. All other
      modules depend only on VoiceChatManager's typed interface.
    * The skip-guard pattern prevents double-advancing the queue: when
      change_stream() fires a StreamAudioEnded event for the replaced track,
      we suppress that event so on_stream_end() is not called twice.
    * Locks and sets are safe for single-process, single-event-loop use.
      Multi-process deployments would need Redis-backed state.
    """

    def __init__(self, client: Client) -> None:
        self._tgcalls = PyTgCalls(client)
        self._active: Set[int] = set()
        self._on_stream_end: Optional[StreamEndCallback] = None
        # Per-chat skip-in-progress guard.
        self._skip_in_progress: Set[int] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_on_stream_end(self, callback: StreamEndCallback) -> None:
        """
        Register the callback awaited when a stream finishes naturally.

        Called by the lifecycle layer AFTER MusicEngine is created, breaking
        the circular dependency (engine → vc_manager → engine).
        """
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register internal event handlers."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("PyTgCalls engine started (pytgcalls >= 0.9.x)")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down the engine."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Join the voice chat and begin streaming.

        Returns True on success, False when no active voice chat exists in
        the group (the group must have an open voice chat before we call this).

        If the bot is already connected, falls back to change_stream()
        automatically rather than raising or returning False.

        Raises
        ------
        AttributeError
            Immediately re-raised when the installed pytgcalls version does
            not have the expected API. This surfaces version mismatches in
            logs immediately rather than silently producing a no-audio state.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play  chat_id={}", chat_id)
            return True

        except AttributeError:
            # Version mismatch — surface immediately.
            raise

        except Exception as exc:
            if _is_no_active_call(exc):
                logger.warning(
                    "No active voice chat in group  chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

            if _is_already_in_call(exc):
                # Already connected — switch the stream instead.
                logger.debug(
                    "Already in call, switching stream  chat_id={}",
                    chat_id,
                )
                return await self.change_stream(chat_id, stream)

            logger.error(
                "play() failed  chat_id={}  error={}  type={}",
                chat_id, exc, type(exc).__name__,
            )
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).

        Returns True on success. Falls back to a fresh play() only when the
        bot is not currently connected to the VC at all.

        Raises
        ------
        AttributeError
            Re-raised immediately when the installed version lacks change_stream().
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            self._active.add(chat_id)
            logger.debug("Stream changed  chat_id={}", chat_id)
            return True

        except AttributeError:
            raise

        except Exception as exc:
            if _is_no_active_call(exc):
                # Not connected at all — attempt a fresh join.
                logger.warning(
                    "Not in VC during change_stream, attempting join  "
                    "chat_id={}  error={}",
                    chat_id, exc,
                )
                self._active.discard(chat_id)
                try:
                    await self._tgcalls.play(chat_id, stream)
                    self._active.add(chat_id)
                    return True
                except AttributeError:
                    raise
                except Exception as exc2:
                    logger.error(
                        "Fresh join after change_stream failure also failed  "
                        "chat_id={}  error={}",
                        chat_id, exc2,
                    )
                    self._active.discard(chat_id)
                    return False

            logger.error(
                "change_stream failed  chat_id={}  error={}  type={}",
                chat_id, exc, type(exc).__name__,
            )
            return False

    async def leave(self, chat_id: int) -> None:
        """Leave the voice chat and remove from the active set."""
        await self._safe_leave(chat_id)
        self._active.discard(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause the current stream. Returns False on failure."""
        try:
            await self._tgcalls.pause_stream(chat_id)
            logger.debug("Stream paused  chat_id={}", chat_id)
            return True
        except AttributeError:
            raise
        except Exception as exc:
            logger.error("pause_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns False on failure."""
        try:
            await self._tgcalls.resume_stream(chat_id)
            logger.debug("Stream resumed  chat_id={}", chat_id)
            return True
        except AttributeError:
            raise
        except Exception as exc:
            logger.error("resume_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    # ── Skip guard ────────────────────────────────────────────────────────────

    def begin_skip(self, chat_id: int) -> None:
        """
        Mark a manual skip as in-progress for *chat_id*.

        While this flag is set, StreamAudioEnded events for this chat are
        suppressed. This prevents change_stream() from double-advancing the
        queue: change_stream() fires StreamAudioEnded for the replaced track,
        which would otherwise trigger on_stream_end() a second time.
        """
        self._skip_in_progress.add(chat_id)

    def end_skip(self, chat_id: int) -> None:
        """Clear the skip-in-progress flag for *chat_id*."""
        self._skip_in_progress.discard(chat_id)

    def is_skip_in_progress(self, chat_id: int) -> bool:
        return chat_id in self._skip_in_progress

    # ── State queries ─────────────────────────────────────────────────────────

    def is_active(self, chat_id: int) -> bool:
        return chat_id in self._active

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _safe_leave(self, chat_id: int) -> None:
        try:
            await self._tgcalls.leave_call(chat_id)
            logger.info("Left VC  chat_id={}", chat_id)
        except Exception as exc:
            # Suppress errors on leave — the VC may already be closed.
            logger.debug("leave_call suppressed  chat_id={}  error={}", chat_id, exc)

    def _register_callbacks(self) -> None:
        """
        Attach pytgcalls 0.9.x event handlers.

        Handler 1 — StreamAudioEnded (audio track finished naturally)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        @on_update() with no filter receives ALL update objects.
        We use isinstance(update, StreamAudioEnded) to filter.

        Note: The event class is StreamAudioEnded in pytgcalls 0.9.x.
        Earlier versions used StreamEnded; py-tgcalls 2.x used a different
        import path entirely. This module targets the 0.9.x stable API.

        Handler 2 — ChatUpdate CLOSED_VOICE_CHAT
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Fires when:
          • The voice chat is closed by an admin.
          • The bot is kicked from the VC.
          • The bot leaves voluntarily (via leave_call()).

        We suppress this on intentional leaves (skip/stop guard) and on
        cases where we initiated the leave ourselves (active set already
        cleared by the caller).
        """

        # ── Handler 1: stream finished naturally ───────────────────────────
        @self._tgcalls.on_update()
        async def _on_stream_audio_ended(
            _client: PyTgCalls, update: Update
        ) -> None:
            # on_update() with no filter fires for ALL event types.
            if not isinstance(update, StreamAudioEnded):
                return

            chat_id: int = update.chat_id

            # Suppress events fired by change_stream() during a manual skip.
            if self.is_skip_in_progress(chat_id):
                logger.debug(
                    "StreamAudioEnded suppressed (skip in progress)  chat_id={}",
                    chat_id,
                )
                return

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

        # ── Handler 2: VC closed / bot kicked / left ───────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT)
        )
        async def _on_vc_closed(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("VC closed/kicked  chat_id={}", chat_id)

            was_active = chat_id in self._active
            self._active.discard(chat_id)

            # Don't trigger on_stream_end for intentional leaves.
            if self.is_skip_in_progress(chat_id):
                logger.debug(
                    "CLOSED_VOICE_CHAT suppressed (skip in progress)  chat_id={}",
                    chat_id,
                )
                return

            if was_active and self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_vc_closed callback raised  chat_id={}  error={}",
                        chat_id, exc,
                    )

        logger.debug(
            "PyTgCalls handlers registered: "
            "on_update(StreamAudioEnded) + "
            "on_update(fl.chat_update(CLOSED_VOICE_CHAT))"
        )
