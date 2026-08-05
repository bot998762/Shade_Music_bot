"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
VoiceChatManager — wrapper around py-tgcalls PyTgCalls.

Verified API for py-tgcalls==2.3.3 (import name: pytgcalls)
------------------------------------------------------------
Confirmed from production bots and PyPI source:

    from pytgcalls import PyTgCalls
    from pytgcalls import filters as fl
    from pytgcalls.types import Update, ChatUpdate, MediaStream, AudioQuality
    from pytgcalls.types.stream import StreamAudioEnded      ← submodule, NOT pytgcalls.types
    from pytgcalls.exceptions import AlreadyJoinedError, NoActiveGroupCall

    client = PyTgCalls(pyrogram_client)
    await client.start()
    await client.play(chat_id, MediaStream(...))
    await client.change_stream(chat_id, MediaStream(...))
    await client.leave_call(chat_id)
    await client.pause_stream(chat_id)
    await client.resume_stream(chat_id)

    Event decorator:
        @client.on_update()                                    # all events
        @client.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))

    ChatUpdate.Status.LEFT_CALL  — VC closed/bot kicked (2.x name)
    StreamAudioEnded              — audio track finished (in pytgcalls.types.stream)
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.exceptions import AlreadyJoinedError, NoActiveGroupCall
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, Update
from pytgcalls.types.stream import StreamAudioEnded

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]


class VoiceChatManager:
    """
    Manages voice-chat connections for all active group chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (user assistant account).
        Must be a user account — bot accounts cannot produce audio in
        Telegram voice chats without special permissions and server support.
    """

    def __init__(self, client: Client) -> None:
        self._tgcalls = PyTgCalls(client)
        self._active: Set[int] = set()
        self._on_stream_end: Optional[StreamEndCallback] = None
        self._skip_in_progress: Set[int] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_on_stream_end(self, callback: StreamEndCallback) -> None:
        """Register the callback invoked when a stream finishes naturally."""
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register event handlers."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("PyTgCalls engine started (py-tgcalls==2.3.3)")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Join the voice chat and begin streaming.

        Returns True on success, False when no active voice chat exists.
        Automatically falls back to change_stream() if already connected.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play  chat_id={}", chat_id)
            return True

        except AlreadyJoinedError:
            logger.debug("Already in call, switching stream  chat_id={}", chat_id)
            return await self.change_stream(chat_id, stream)

        except NoActiveGroupCall:
            logger.warning("No active voice chat in group  chat_id={}", chat_id)
            return False

        except Exception as exc:
            logger.error(
                "play() failed  chat_id={}  error={}  type={}",
                chat_id, exc, type(exc).__name__,
            )
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).

        Falls back to a fresh play() if the bot is not connected to the VC.
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            self._active.add(chat_id)
            logger.debug("Stream changed  chat_id={}", chat_id)
            return True

        except NoActiveGroupCall:
            logger.warning(
                "Not in VC during change_stream, attempting join  chat_id={}",
                chat_id,
            )
            self._active.discard(chat_id)
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

        except Exception as exc:
            logger.error(
                "change_stream failed  chat_id={}  error={}  type={}",
                chat_id, exc, type(exc).__name__,
            )
            return False

    async def leave(self, chat_id: int) -> None:
        """Leave the voice chat."""
        await self._safe_leave(chat_id)
        self._active.discard(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause the current stream. Returns False on failure."""
        try:
            await self._tgcalls.pause_stream(chat_id)
            logger.debug("Stream paused  chat_id={}", chat_id)
            return True
        except Exception as exc:
            logger.error("pause_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns False on failure."""
        try:
            await self._tgcalls.resume_stream(chat_id)
            logger.debug("Stream resumed  chat_id={}", chat_id)
            return True
        except Exception as exc:
            logger.error("resume_stream failed  chat_id={}  error={}", chat_id, exc)
            return False

    # ── Skip guard ────────────────────────────────────────────────────────────

    def begin_skip(self, chat_id: int) -> None:
        """
        Mark a manual skip as in-progress.

        While this flag is set, StreamAudioEnded events for this chat are
        suppressed, preventing change_stream() from double-advancing the queue.
        """
        self._skip_in_progress.add(chat_id)

    def end_skip(self, chat_id: int) -> None:
        """Clear the skip-in-progress flag."""
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
        Register py-tgcalls 2.3.x event handlers.

        Handler 1 — StreamAudioEnded
            Import path: pytgcalls.types.stream.StreamAudioEnded
            Fires when the audio track finishes playing naturally.
            Uses @on_update() with no filter; we isinstance-check inside.

        Handler 2 — ChatUpdate LEFT_CALL
            Fires when the VC is closed by an admin or the bot is kicked.
            Filter: fl.chat_update(ChatUpdate.Status.LEFT_CALL)
            Name in 2.x: LEFT_CALL  (not CLOSED_VOICE_CHAT)
        """

        # ── Handler 1: stream finished naturally ───────────────────────────
        @self._tgcalls.on_update()
        async def _on_stream_ended(_client: PyTgCalls, update: Update) -> None:
            if not isinstance(update, StreamAudioEnded):
                return

            chat_id: int = update.chat_id

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

        # ── Handler 2: VC closed / bot kicked ──────────────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.LEFT_CALL)
        )
        async def _on_vc_left(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("VC closed/left  chat_id={}", chat_id)

            was_active = chat_id in self._active
            self._active.discard(chat_id)

            if self.is_skip_in_progress(chat_id):
                return

            if was_active and self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_vc_left callback raised  chat_id={}  error={}",
                        chat_id, exc,
                    )

        logger.debug(
            "PyTgCalls handlers registered: "
            "on_update(StreamAudioEnded) + "
            "on_update(fl.chat_update(LEFT_CALL))"
        )
