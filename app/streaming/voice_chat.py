"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
VoiceChatManager — wrapper around py-tgcalls PyTgCalls.

Verified API surface for py-tgcalls==2.3.3
-------------------------------------------
Source confirmed from pytgcalls/pytgcalls master (pyrogram_client.py):

Imports that work:
    from pytgcalls import PyTgCalls
    from pytgcalls import filters as fl
    from pytgcalls.types import Update, ChatUpdate, MediaStream, AudioQuality
    from pytgcalls.types.stream import StreamAudioEnded   ← pytgcalls.types.stream submodule

ChatUpdate.Status values (from pyrogram_client.py source):
    ChatUpdate.Status.CLOSED_VOICE_CHAT   ← GroupCallDiscarded
    ChatUpdate.Status.KICKED              ← ChannelForbidden/ChatForbidden
    ChatUpdate.Status.LEFT_GROUP          ← bot left the group
    ChatUpdate.Status.INVITED_VOICE_CHAT

DO NOT import from pytgcalls.exceptions — the exception class names change
across minor versions (AlreadyJoinedError missing in 2.3.3).
Exception routing is done by inspecting exception message strings instead,
which is stable across all 2.x releases.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import AudioQuality, ChatUpdate, MediaStream, Update
from pytgcalls.types.stream import StreamAudioEnded

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]

# Substrings that identify "not in / no active VC" errors across 2.x versions.
_NO_CALL_PHRASES = (
    "no_active_group_call",
    "groupcall_not_found",
    "no active",
    "not found",
    "not in",
    "not_in",
    "noactivegroupcall",
)

# Substrings that identify "already joined" errors across 2.x versions.
_ALREADY_JOINED_PHRASES = (
    "already",
    "alreadyjoined",
    "already_joined",
    "in_call",
)


def _match(exc: Exception, phrases: tuple) -> bool:
    msg = str(exc).lower().replace(" ", "")
    return any(p.replace(" ", "") in msg for p in phrases)


class VoiceChatManager:
    """
    Manages voice-chat connections for all active group chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (user assistant account).
        Must be a user account — bot accounts cannot produce audio in
        Telegram voice chats without the right permissions.
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

        except Exception as exc:
            exc_type = type(exc).__name__

            if _match(exc, _ALREADY_JOINED_PHRASES):
                logger.debug(
                    "Already in call ({}), switching stream  chat_id={}",
                    exc_type, chat_id,
                )
                return await self.change_stream(chat_id, stream)

            if _match(exc, _NO_CALL_PHRASES):
                logger.warning(
                    "No active voice chat  chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

            logger.error(
                "play() failed  chat_id={}  type={}  error={}",
                chat_id, exc_type, exc,
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

        except Exception as exc:
            if _match(exc, _NO_CALL_PHRASES):
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
                except Exception as exc2:
                    logger.error(
                        "Fresh join after change_stream failure  "
                        "chat_id={}  error={}",
                        chat_id, exc2,
                    )
                    self._active.discard(chat_id)
                    return False

            logger.error(
                "change_stream failed  chat_id={}  type={}  error={}",
                chat_id, type(exc).__name__, exc,
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

        Suppresses the StreamAudioEnded event fired by change_stream() so the
        queue is not double-advanced.
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
        Register py-tgcalls 2.3.3 event handlers.

        Verified ChatUpdate.Status values from pytgcalls/pytgcalls source:
          CLOSED_VOICE_CHAT — GroupCallDiscarded (admin ended the VC)
          KICKED            — ChannelForbidden / ChatForbidden (bot removed)
          LEFT_GROUP        — bot left the group entirely

        We handle all three with separate decorated functions so each fires
        independently. StreamAudioEnded is caught via isinstance() inside
        the unfiltered on_update() handler.
        """

        # ── Handler 1: audio track finished naturally ──────────────────────
        @self._tgcalls.on_update()
        async def _on_update(_client: PyTgCalls, update: Update) -> None:
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
            await self._fire_stream_end(chat_id)

        # ── Handler 2: VC closed by admin ─────────────────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT)
        )
        async def _on_vc_closed(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("VC closed by admin  chat_id={}", chat_id)
            was_active = chat_id in self._active
            self._active.discard(chat_id)
            if not self.is_skip_in_progress(chat_id) and was_active:
                await self._fire_stream_end(chat_id)

        # ── Handler 3: bot kicked from group ──────────────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.KICKED)
        )
        async def _on_kicked(_client: PyTgCalls, update: Update) -> None:
            chat_id: int = update.chat_id
            logger.warning("Bot kicked from chat  chat_id={}", chat_id)
            was_active = chat_id in self._active
            self._active.discard(chat_id)
            if not self.is_skip_in_progress(chat_id) and was_active:
                await self._fire_stream_end(chat_id)

        logger.debug(
            "PyTgCalls handlers registered: "
            "StreamAudioEnded + CLOSED_VOICE_CHAT + KICKED"
        )

    async def _fire_stream_end(self, chat_id: int) -> None:
        """Safely invoke the registered on_stream_end callback."""
        if self._on_stream_end is not None:
            try:
                await self._on_stream_end(chat_id)
            except Exception as exc:
                logger.error(
                    "_fire_stream_end callback raised  chat_id={}  error={}",
                    chat_id, exc,
                )
