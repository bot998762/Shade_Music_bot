"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
Thin, testable wrapper around pytgcalls ``PyTgCalls``.

Responsibilities
----------------
* Own the ``PyTgCalls`` instance and its lifecycle (start / stop).
* Translate high-level intent (play, skip, pause, leave) into pytgcalls
  API calls.
* Track which chats are currently active so the engine can query state
  without asking Telegram.
* Deliver stream-end events to the engine via a settable callback, breaking
  the circular dependency that would arise if the engine were injected at
  construction time.

Upgrade path
------------
If pytgcalls releases a new major API version, only this file changes.
The engine and handlers remain untouched.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, Update
from pytgcalls.exceptions import (
    AlreadyJoinedError,
    NoActiveGroupCall,
    NotInGroupCallError,
    GroupCallNotFound,
)

from app.core.logger import logger

# Type alias for the stream-end callback supplied by MusicEngine
StreamEndCallback = Callable[[int], Awaitable[None]]


class VoiceChatManager:
    """
    Manages voice-chat connections for all active chats.

    Parameters
    ----------
    client:
        A started Pyrogram ``Client`` (bot or user assistant).
    """

    def __init__(self, client: Client) -> None:
        self._tgcalls = PyTgCalls(client)
        self._active: Set[int] = set()
        self._on_stream_end: Optional[StreamEndCallback] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_on_stream_end(self, callback: StreamEndCallback) -> None:
        """
        Register the coroutine that is awaited when a stream finishes naturally.

        Called by the lifecycle layer AFTER ``MusicEngine`` is created, which
        avoids the circular dependency (engine → vc_manager → engine).
        """
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register internal callbacks."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("PyTgCalls engine started ✓")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down PyTgCalls."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Join the voice chat in *chat_id* and start streaming *stream*.

        If already connected, the running stream is replaced.
        Returns ``True`` on success, ``False`` when the VC does not exist.
        """
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("VC join+play — chat_id={}", chat_id)
            return True

        except AlreadyJoinedError:
            # Bot is in the VC already — swap the stream instead
            return await self.change_stream(chat_id, stream)

        except NoActiveGroupCall:
            logger.warning("No active voice chat in chat_id={}", chat_id)
            return False

        except Exception as exc:
            logger.error("VC play failed — chat_id={} error={}", chat_id, exc)
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream with *stream* (used for skip).

        Falls back to a full join if the bot is somehow no longer in the VC.
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            logger.debug("Stream changed — chat_id={}", chat_id)
            return True

        except (NotInGroupCallError, GroupCallNotFound):
            logger.warning(
                "change_stream: not in VC — attempting fresh join for chat_id={}",
                chat_id,
            )
            return await self.play(chat_id, stream)

        except Exception as exc:
            logger.error("change_stream failed — chat_id={} error={}", chat_id, exc)
            return False

    async def leave(self, chat_id: int) -> None:
        """Leave the voice chat in *chat_id* and remove it from the active set."""
        await self._safe_leave(chat_id)
        self._active.discard(chat_id)

    async def pause(self, chat_id: int) -> bool:
        """Pause the stream. Returns ``False`` if not in the VC."""
        try:
            await self._tgcalls.pause(chat_id)
            return True
        except (NotInGroupCallError, GroupCallNotFound):
            return False
        except Exception as exc:
            logger.error("pause failed — chat_id={} error={}", chat_id, exc)
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns ``False`` if not in the VC."""
        try:
            await self._tgcalls.resume(chat_id)
            return True
        except (NotInGroupCallError, GroupCallNotFound):
            return False
        except Exception as exc:
            logger.error("resume failed — chat_id={} error={}", chat_id, exc)
            return False

    # ── State queries ─────────────────────────────────────────────────────────

    def is_active(self, chat_id: int) -> bool:
        """Return ``True`` when the bot is currently in the VC for *chat_id*."""
        return chat_id in self._active

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _safe_leave(self, chat_id: int) -> None:
        """Leave a VC, suppressing errors if already disconnected."""
        try:
            await self._tgcalls.leave_call(chat_id)
            logger.info("Left VC — chat_id={}", chat_id)
        except Exception as exc:
            # Already left or never joined — not an error worth crashing over
            logger.debug("leave_call suppressed error — chat_id={} error={}", chat_id, exc)

    def _register_callbacks(self) -> None:
        """Attach pytgcalls event handlers (must be called before start())."""

        @self._tgcalls.on_stream_end()
        async def _on_stream_end(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.info("Stream ended naturally — chat_id={}", chat_id)
            if self._on_stream_end is not None:
                try:
                    await self._on_stream_end(chat_id)
                except Exception as exc:
                    logger.error(
                        "on_stream_end callback raised — chat_id={} error={}",
                        chat_id,
                        exc,
                    )

        @self._tgcalls.on_kicked()
        async def _on_kicked(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("Bot was kicked from VC — chat_id={}", chat_id)
            self._active.discard(chat_id)
            # Treat kick like a stream end so the engine cleans up
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)

        @self._tgcalls.on_closed_voice_chat()
        async def _on_closed(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("Voice chat was closed — chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)
