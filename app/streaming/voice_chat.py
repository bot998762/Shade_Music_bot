"""
app.streaming.voice_chat
~~~~~~~~~~~~~~~~~~~~~~~~
Thin, testable wrapper around py-tgcalls PyTgCalls.

Exception-handling strategy
----------------------------
py-tgcalls has broken exception API compatibility across minor versions —
names like AlreadyJoinedError and NoActiveGroupCall have been added,
renamed, or removed between 2.0.x and 2.3.x.

To avoid ImportError on startup and to stay compatible with every current
and future release, this module imports NO named exception from
pytgcalls.exceptions.  All pytgcalls call-sites catch the bare Exception
base class and inspect the message string where the error type matters.

The only external exception we import is ntgcalls.NTgCallsError, which
comes from the separate ntgcalls package and has been stable.

Event-API compatibility note (py-tgcalls >= 2.1)
-------------------------------------------------
The legacy per-event decorator methods  — on_stream_end(), on_kicked(),
on_closed_voice_chat() — were removed in py-tgcalls ~2.1.  The current
API is a single on_update() handler with optional filter objects:

    @tgcalls.on_update()                              # all updates
    @tgcalls.on_update(fl.chat_update(Status.LEFT))  # chat-level events

Stream-end updates are identified by isinstance() checks against the
StreamEnded type (introduced in ~2.1, previously StreamAudioEnded).
Both names are attempted at import time so the file stays compatible
with every 2.x release.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import MediaStream, Update

# ---------------------------------------------------------------------------
# ChatUpdate import — available in py-tgcalls >= 2.1.
# We guard with try/except to keep backward compat with 2.0.x.
# ---------------------------------------------------------------------------
try:
    from pytgcalls.types import ChatUpdate as _ChatUpdate  # type: ignore[attr-defined]
    _CHAT_UPDATE_CLS = _ChatUpdate
    _LEFT_CALL_STATUS = _ChatUpdate.Status.LEFT_CALL  # type: ignore[attr-defined]
    _HAS_CHAT_UPDATE = True
except (ImportError, AttributeError):
    _CHAT_UPDATE_CLS = None  # type: ignore[assignment]
    _LEFT_CALL_STATUS = None
    _HAS_CHAT_UPDATE = False

# ---------------------------------------------------------------------------
# Stream-end type: StreamEnded (>= 2.1) or StreamAudioEnded (2.0.x).
# ---------------------------------------------------------------------------
_STREAM_ENDED_CLS: Optional[type] = None
try:
    from pytgcalls.types.stream import StreamEnded as _SE  # type: ignore[attr-defined]
    _STREAM_ENDED_CLS = _SE
except ImportError:
    pass

if _STREAM_ENDED_CLS is None:
    try:
        from pytgcalls.types.stream import StreamAudioEnded as _SAE  # type: ignore[attr-defined]
        _STREAM_ENDED_CLS = _SAE
    except ImportError:
        pass

# If we still have nothing, fall back to the generic Update base so that
# the isinstance check below always passes (stream events will still fire).
if _STREAM_ENDED_CLS is None:
    _STREAM_ENDED_CLS = Update  # type: ignore[assignment]

from app.core.logger import logger

StreamEndCallback = Callable[[int], Awaitable[None]]

# Error message fragments that indicate there is no active voice chat.
# These come from Telegram's MTProto layer and are stable across versions.
_NO_CALL_PHRASES = (
    "no_active_group_call",
    "groupcall_not_found",
    "not_found",
    "no active",
)


def _is_no_active_call(exc: Exception) -> bool:
    """Return True when *exc* indicates no voice chat is running in the group."""
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

        Returns True on success, False when no active voice chat exists in
        the group.

        If the bot is already connected, falls back to change_stream so the
        caller does not need to track connection state explicitly.
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

            # Any other failure (including "already joined" variants) —
            # attempt change_stream, which also handles a fresh join if needed.
            logger.debug(
                "play() raised, retrying via change_stream  chat_id={}  error={}",
                chat_id, exc,
            )
            return await self.change_stream(chat_id, stream)

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).

        Falls back to a fresh play() call if the bot is somehow no longer
        connected.
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

            # Likely "not in call" — attempt a completely fresh join.
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
        """
        Attach py-tgcalls event handlers (must be called before start()).

        py-tgcalls >= 2.1 replaced the legacy per-event decorators
        (on_stream_end, on_kicked, on_closed_voice_chat) with a unified
        on_update() dispatcher.  We try to register with the new API first
        and fall back to the old one so the same code works on every 2.x
        release.
        """
        registered = self._try_register_new_api()
        if not registered:
            self._register_legacy_api()

    # ── New API (py-tgcalls >= 2.1) ───────────────────────────────────────────

    def _try_register_new_api(self) -> bool:
        """
        Register handlers using the on_update() API introduced in py-tgcalls 2.1.

        Returns True when registration succeeded, False when the API is not
        present (older version — caller should fall back to legacy API).
        """
        if not hasattr(self._tgcalls, "on_update"):
            return False

        # ── Stream-end handler ────────────────────────────────────────────
        @self._tgcalls.on_update()
        async def _on_stream_end_new(_client: PyTgCalls, update: Update) -> None:
            # Skip chat-level updates (kicked / VC closed) — handled below.
            if _CHAT_UPDATE_CLS is not None and isinstance(update, _CHAT_UPDATE_CLS):
                return
            # Accept only stream-end events.
            if not isinstance(update, _STREAM_ENDED_CLS):
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

        # ── Chat-update handler (kicked / VC closed / bot left) ───────────
        if _HAS_CHAT_UPDATE and _LEFT_CALL_STATUS is not None:
            try:
                @self._tgcalls.on_update(fl.chat_update(_LEFT_CALL_STATUS))
                async def _on_left_new(_client: PyTgCalls, update: Update) -> None:
                    chat_id: int = update.chat_id
                    logger.warning(
                        "Bot left/kicked/VC-closed  chat_id={}", chat_id
                    )
                    self._active.discard(chat_id)
                    if self._on_stream_end is not None:
                        try:
                            await self._on_stream_end(chat_id)
                        except Exception as exc:
                            logger.error(
                                "on_left callback raised  chat_id={}  error={}",
                                chat_id, exc,
                            )
            except Exception as exc:
                logger.debug(
                    "Could not register chat_update filter handler: {}", exc
                )
        else:
            # Fallback: catch all updates and filter by type at runtime.
            @self._tgcalls.on_update()
            async def _on_left_fallback(
                _client: PyTgCalls, update: Update
            ) -> None:
                if _CHAT_UPDATE_CLS is None:
                    return
                if not isinstance(update, _CHAT_UPDATE_CLS):
                    return
                chat_id: int = update.chat_id
                logger.warning(
                    "Bot left/kicked/VC-closed (fallback)  chat_id={}", chat_id
                )
                self._active.discard(chat_id)
                if self._on_stream_end is not None:
                    try:
                        await self._on_stream_end(chat_id)
                    except Exception as exc:
                        logger.error(
                            "on_left_fallback raised  chat_id={}  error={}",
                            chat_id, exc,
                        )

        logger.debug("PyTgCalls handlers registered via new on_update() API")
        return True

    # ── Legacy API (py-tgcalls 2.0.x) ────────────────────────────────────────

    def _register_legacy_api(self) -> None:
        """
        Register handlers using the legacy decorator methods present in
        py-tgcalls 2.0.x (on_stream_end, on_kicked, on_closed_voice_chat).

        This path is only reached when on_update() is absent.
        """
        logger.debug(
            "on_update() not found — falling back to legacy PyTgCalls API"
        )

        @self._tgcalls.on_stream_end()  # type: ignore[attr-defined]
        async def _on_stream_end_legacy(
            _client: PyTgCalls, update: Update
        ) -> None:
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

        @self._tgcalls.on_kicked()  # type: ignore[attr-defined]
        async def _on_kicked_legacy(
            _client: PyTgCalls, update: Update
        ) -> None:
            chat_id = update.chat_id
            logger.warning("Bot kicked from VC  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)

        @self._tgcalls.on_closed_voice_chat()  # type: ignore[attr-defined]
        async def _on_closed_legacy(
            _client: PyTgCalls, update: Update
        ) -> None:
            chat_id = update.chat_id
            logger.warning("Voice chat closed  chat_id={}", chat_id)
            self._active.discard(chat_id)
            if self._on_stream_end is not None:
                await self._on_stream_end(chat_id)
