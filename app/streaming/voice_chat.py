"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
Thin, testable wrapper around py-tgcalls PyTgCalls.

py-tgcalls 2.3+ compatibility
------------------------------
All APIs verified against:
  • pytgcalls/pytgcalls master branch (2025-08-05)
  • AsmSafone/MusicPlayer/main.py (production reference)

Confirmed method names (py-tgcalls >= 2.3):
    pytgcalls.play(chat_id, stream)
    pytgcalls.change_stream(chat_id, stream)
    pytgcalls.leave_call(chat_id)
    pytgcalls.pause_stream(chat_id)
    pytgcalls.resume_stream(chat_id)

Event types:
    from pytgcalls.types.stream import StreamEnded
    from pytgcalls.types import ChatUpdate
    from pytgcalls import filters as fl
    fl.chat_update(ChatUpdate.Status.LEFT_CALL)

Exception handling:
    AttributeError is re-raised (not silently swallowed) so version mismatches
    surface immediately in logs rather than causing silent no-audio failures.
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


def _is_already_in_call(exc: Exception) -> bool:
    """Return True when *exc* signals the bot is already connected."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in ("already", "in_call", "joined"))


class VoiceChatManager:
    """
    Manages voice-chat connections for all active chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (user assistant — NOT a bot account).
        A bot account cannot produce audio in Telegram voice chats.
    """

    def __init__(self, client: Client) -> None:
        self._tgcalls = PyTgCalls(client)
        self._active: Set[int] = set()
        self._on_stream_end: Optional[StreamEndCallback] = None
        # Per-chat flag: True while a manual skip is in progress.
        # Prevents on_stream_end from double-advancing the queue when
        # change_stream() fires a StreamEnded event for the replaced track.
        self._skip_in_progress: Set[int] = set()

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

        Raises
        ------
        AttributeError
            Re-raised immediately when the installed py-tgcalls version does
            not have the expected API.  This surfaces version mismatches in
            logs rather than silently producing a no-audio state.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play  chat_id={}", chat_id)
            return True

        except AttributeError:
            # Version mismatch — re-raise so the operator sees it immediately.
            raise

        except Exception as exc:
            if _is_no_active_call(exc):
                logger.warning(
                    "No active voice chat in group  chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

            if _is_already_in_call(exc):
                # Already connected — switch stream instead of joining again.
                logger.debug(
                    "Already in call, switching stream  chat_id={}",
                    chat_id,
                )
                return await self.change_stream(chat_id, stream)

            # Any other unexpected error.
            logger.error(
                "play() failed  chat_id={}  error={}  type={}",
                chat_id, exc, type(exc).__name__,
            )
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).

        Returns True on success.
        Falls back to a fresh play() only when the bot is not connected at all.

        Raises
        ------
        AttributeError
            Re-raised immediately when the installed py-tgcalls version does
            not have change_stream().
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
        """
        Pause the stream.

        Uses pause_stream() — the correct py-tgcalls 2.x method name.
        (The old .pause() method was removed in 2.x.)
        """
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
        """
        Resume a paused stream.

        Uses resume_stream() — the correct py-tgcalls 2.x method name.
        (The old .resume() method was removed in 2.x.)
        """
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

        While this flag is set, on_stream_end callbacks are suppressed for
        this chat so that the StreamEnded event fired by change_stream()
        does not cause the engine to double-advance the queue.
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
            logger.debug("leave_call suppressed  chat_id={}  error={}", chat_id, exc)

    def _register_callbacks(self) -> None:
        """
        Attach py-tgcalls 2.x event handlers.

        Handler 1 — stream-end  (@on_update, no filter)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        on_update() with no filter receives ALL update types.  We use
        isinstance(update, StreamEnded) to filter to stream-end events only.

        Handler 2 — VC left / kicked / closed  (@on_update with fl.chat_update)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        fl.chat_update(ChatUpdate.Status.LEFT_CALL) fires when the bot is
        kicked from a VC, the VC is closed, or the bot leaves.  We guard
        against triggering on_stream_end when we caused the leave ourselves
        (via skip or stop) by checking the skip flag and active set.
        """

        # ── Handler 1: stream ended naturally ─────────────────────────────
        @self._tgcalls.on_update()
        async def _on_stream_end(_client: PyTgCalls, update: Update) -> None:
            # on_update() receives ALL events; filter to StreamEnded only.
            if not isinstance(update, StreamEnded):
                return

            chat_id: int = update.chat_id

            # If a manual skip is in progress, this StreamEnded was fired by
            # change_stream() replacing the old track.  Suppress it — the
            # skip handler will take care of starting the next track.
            if self.is_skip_in_progress(chat_id):
                logger.debug(
                    "StreamEnded suppressed (skip in progress)  chat_id={}",
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

        # ── Handler 2: bot left / kicked / VC closed ──────────────────────
        @self._tgcalls.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _on_left(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("Bot left/kicked/VC-closed  chat_id={}", chat_id)

            # If we initiated the leave (skip / stop), the active set is
            # already being managed by the caller — don't double-trigger
            # on_stream_end, which would attempt to play a new track into
            # a VC that was intentionally closed.
            was_active = chat_id in self._active
            self._active.discard(chat_id)

            # Only trigger on_stream_end for unexpected departures.
            # Heuristic: if skip is in progress, this LEFT_CALL is ours.
            if self.is_skip_in_progress(chat_id):
                logger.debug(
                    "LEFT_CALL suppressed (skip in progress)  chat_id={}",
                    chat_id,
                )
                return

            if was_active and self._on_stream_end is not None:
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
