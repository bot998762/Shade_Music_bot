"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
Thin, testable wrapper around py-tgcalls PyTgCalls.

py-tgcalls 2.x compatibility notes
------------------------------------
* PyPI package  : py-tgcalls  (install name)
* Import name   : pytgcalls   (same as always)
* Exceptions    : AlreadyJoinedError, NoActiveGroupCall  <- from pytgcalls.exceptions
                  TelegramServerError                    <- from ntgcalls (separate pkg)
* Being 'not in call' raises a generic Exception in 2.x; broad catches used.
* Stream end callback: @tgcalls.on_stream_end() still works; update.chat_id is valid.
* on_kicked() and on_closed_voice_chat() decorators are available in 2.x.

Upgrade path
------------
If py-tgcalls releases a new major API version, only this file changes.
Engine and handlers remain untouched.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from ntgcalls import TelegramServerError
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import AlreadyJoinedError, NoActiveGroupCall
from pytgcalls.types import MediaStream, Update

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]


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
        Register the coroutine that is awaited when a stream finishes naturally.

        Called by the lifecycle AFTER MusicEngine is created, breaking the
        circular dependency (engine -> vc_manager -> engine).
        """
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register internal callbacks."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("PyTgCalls engine started")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down PyTgCalls."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Join the voice chat and start streaming.

        Returns True on success, False when no active voice chat exists.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play  chat_id={}", chat_id)
            return True

        except AlreadyJoinedError:
            # Already connected — swap stream instead
            return await self.change_stream(chat_id, stream)

        except NoActiveGroupCall:
            logger.warning("No active voice chat  chat_id={}", chat_id)
            return False

        except TelegramServerError as exc:
            logger.error("Telegram server error during play  chat_id={}  error={}", chat_id, exc)
            return False

        except Exception as exc:
            logger.error("VC play failed  chat_id={}  error={}", chat_id, exc)
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the running stream (used for skip / auto-advance).
        Falls back to a fresh join if the bot is somehow no longer in the VC.
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            logger.debug("Stream changed  chat_id={}", chat_id)
            return True

        except Exception as exc:
            # In py-tgcalls 2.x, being "not in call" raises a generic error;
            # attempt a fresh join before giving up.
            logger.warning(
                "change_stream failed, attempting fresh join  chat_id={}  error={}",
                chat_id, exc,
            )
            return await self.play(chat_id, stream)

    async def leave(self, chat_id: int) -> None:
        """Leave the voice chat and remove from the active set."""
        await self._safe_leave(chat_id)
        self._active.discard(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause the stream. Returns False if not in a VC."""
        try:
            await self._tgcalls.pause(chat_id)
            return True
        except Exception as exc:
            logger.error("pause failed  chat_id={}  error={}", chat_id, exc)
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns False if not in a VC."""
        try:
            await self._tgcalls.resume(chat_id)
            return True
        except Exception as exc:
            logger.error("resume failed  chat_id={}  error={}", chat_id, exc)
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
        """Attach py-tgcalls 2.x event handlers (must be called before start())."""

        @self._tgcalls.on_stream_end()
        async def _on_stream_end(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.info("Stream ended naturally  chat_id={}", chat_id)
            if self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_stream_end callback raised  chat_id={}  error={}",
                        chat_id, exc,
                    )

        @self._tgcalls.on_kicked()
        async def _on_kicked(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("Bot kicked from VC  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)

        @self._tgcalls.on_closed_voice_chat()
        async def _on_closed(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("Voice chat closed  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)
