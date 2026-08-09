"""
app.streaming.voice
~~~~~~~~~~~~~~~~~~~~
VoiceChatManager — wrapper around py-tgcalls PyTgCalls (v2.3.3).

Responsibility
--------------
Join / leave voice chats.
Change the active stream.
Fire callbacks when a stream ends naturally or a VC is force-closed.
Nothing else.

No searching.  No stream URL resolution.  No queue management.

Verified API surface for py-tgcalls==2.3.3
-------------------------------------------
Source confirmed from pytgcalls/pytgcalls master (pyrogram_client.py):

Imports that work:
    from pytgcalls import PyTgCalls
    from pytgcalls import filters as fl
    from pytgcalls.types import Update, ChatUpdate, MediaStream, AudioQuality
    from pytgcalls.types.stream import StreamEnded as StreamAudioEnded

ChatUpdate.Status values:
    CLOSED_VOICE_CHAT   — GroupCallDiscarded (admin ended the VC)
    KICKED              — ChannelForbidden / ChatForbidden (bot removed)
    LEFT_GROUP          — bot left the group entirely

Exception routing
-----------------
DO NOT import from pytgcalls.exceptions — class names change across minor
versions. Route exceptions by inspecting message strings (stable across 2.x).

Public-group auto-join
----------------------
The assistant account starts with an empty peer cache (in_memory=True).
For a group the assistant has never joined, get_chat(numeric_id) raises
CHANNEL_INVALID because the access_hash is unknown.

Resolution flow:
  1. try get_chat(chat_id) via assistant — succeeds if already a member.
  2. On failure: use the BOT client (always a member, received the /play)
     to look up the group via get_chat(chat_id) — bot is always in the group.
  3. If the group has a username (public): assistant.join_chat(username).
  4. After join: assistant.get_chat(chat_id) to populate peer cache.
  5. If no username (private): raise PrivateGroupError (caught by handler).
  6. If join fails: return False (no broken VC state, no retry loop).

Stage log: [VOICE]
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pyrogram.errors import UserAlreadyParticipant
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate, MediaStream, Update
from pytgcalls.types.stream import StreamEnded as StreamAudioEnded

from app.infrastructure.logger import logger
from app.shared.exceptions import PrivateGroupError
from app.streaming.media import AUDIO_QUALITY, IGNORE_VIDEO

StreamEndCallback = Callable[[int], Awaitable[None]]

# ── Exception message patterns ─────────────────────────────────────────────────
# Stable across py-tgcalls 2.x versions.
_NO_CALL = (
    "no_active_group_call",
    "groupcall_not_found",
    "no active",
    "not found",
    "not in",
    "not_in",
    "noactivegroupcall",
)
_ALREADY_JOINED = (
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
        Bot accounts cannot produce audio in Telegram voice chats — always
        use a real user account via ASSISTANT_SESSION.
    bot_client:
        Optional started Pyrogram Client (bot account).
        Used ONLY for group info lookup during auto-join.
        The bot is always a member of the group (it received /play), so it
        can resolve a group's public username even when the assistant cannot.
    """

    def __init__(self, client: Client, bot_client: Optional[Client] = None) -> None:
        self._client    = client                    # assistant (VC account)
        self._bot       = bot_client                # bot (info lookup only)
        self._tgcalls   = PyTgCalls(client)
        self._active:           Set[int] = set()
        self._skip_in_progress: Set[int] = set()
        self._on_stream_end:    Optional[StreamEndCallback] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def set_on_stream_end(self, callback: StreamEndCallback) -> None:
        """Register the callback invoked when a stream finishes naturally."""
        self._on_stream_end = callback

    async def start(self) -> None:
        """Start the PyTgCalls engine and register event handlers."""
        self._register_callbacks()
        await self._tgcalls.start()
        logger.info("[VOICE] PyTgCalls engine started (py-tgcalls==2.3.3)")

    async def stop(self) -> None:
        """Leave all active voice chats and shut down the engine."""
        for chat_id in list(self._active):
            await self._safe_leave(chat_id)
        self._active.clear()
        logger.info("[VOICE] VoiceChatManager stopped")

    # ── Playback control ──────────────────────────────────────────────────────

    async def play(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Ensure assistant membership, join the voice chat, and begin streaming.

        Returns True on success.
        Returns False when no active voice chat exists or auto-join is not possible.

        Membership flow
        ---------------
        Case 1 — Already a member:
            get_chat() succeeds → resolve_peer() OK → tgcalls.play()

        Case 2 — Not a member, public group (has username):
            get_chat(via assistant) fails →
            bot_client.get_chat() to get username →
            assistant.join_chat(username) →
            assistant.get_chat() to warm peer cache →
            resolve_peer() → tgcalls.play()

        Case 3 — Not a member, private group (no username):
            Raises PrivateGroupError.
            Handler shows: ask admin to add @Shade_music_assistant.

        Case 4 — join_chat() fails:
            Logged as ERROR. Returns False. No broken VC state.

        Stage log: [VOICE] join+play
        """
        # ── Step 1: Try to populate peer cache via assistant ───────────────
        already_member = await self._ensure_peer_cached(chat_id)

        if not already_member:
            # ── Step 2: Assistant is NOT in the group. Try auto-join. ─────
            joined = await self._try_auto_join(chat_id)
            if not joined:
                return False

            # ── Step 3: Re-warm peer cache after joining ───────────────────
            try:
                await self._client.get_chat(chat_id)
                logger.debug(
                    "[VOICE] Peer cache warmed after join  chat_id={}", chat_id
                )
            except Exception as exc:
                logger.error(
                    "[VOICE] get_chat() failed after join — cannot resolve peer  "
                    "chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

        # ── Step 4: Verify peer is resolvable ─────────────────────────────
        try:
            peer = await self._client.resolve_peer(chat_id)
            logger.debug(
                "[VOICE] resolve_peer OK  chat_id={}  peer={}", chat_id, peer,
            )
        except Exception as resolve_exc:
            logger.error(
                "[VOICE] resolve_peer() FAILED after membership check — "
                "chat_id={}  error={}",
                chat_id, resolve_exc,
            )
            return False

        # ── Step 5: Join VC and start stream ──────────────────────────────
        try:
            await self._tgcalls.play(chat_id, stream)
            self._active.add(chat_id)
            logger.info("[VOICE] Join+play  chat_id={}", chat_id)
            return True

        except Exception as exc:
            exc_type = type(exc).__name__

            if _match(exc, _ALREADY_JOINED):
                logger.debug(
                    "[VOICE] Already in call ({}), switching stream  chat_id={}",
                    exc_type, chat_id,
                )
                return await self.change_stream(chat_id, stream)

            if _match(exc, _NO_CALL):
                logger.warning(
                    "[VOICE] No active voice chat  chat_id={}  error={}",
                    chat_id, exc,
                )
                return False

            logger.error(
                "[VOICE] play() failed  chat_id={}  type={}  error={}",
                chat_id, exc_type, exc,
            )
            return False

    async def change_stream(self, chat_id: int, stream: MediaStream) -> bool:
        """
        Replace the currently running stream (used for skip / auto-advance).

        Falls back to a fresh play() if the bot is not in the VC.

        Stage log: [VOICE] change
        """
        try:
            await self._tgcalls.change_stream(chat_id, stream)
            self._active.add(chat_id)
            logger.debug("[VOICE] Stream changed  chat_id={}", chat_id)
            return True

        except Exception as exc:
            if _match(exc, _NO_CALL):
                logger.warning(
                    "[VOICE] Not in VC during change_stream, attempting join  "
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
                        "[VOICE] Fresh join after change_stream failure  "
                        "chat_id={}  error={}",
                        chat_id, exc2,
                    )
                    self._active.discard(chat_id)
                    return False

            logger.error(
                "[VOICE] change_stream failed  chat_id={}  type={}  error={}",
                chat_id, type(exc).__name__, exc,
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
            logger.debug("[VOICE] Stream paused  chat_id={}", chat_id)
            return True
        except Exception as exc:
            logger.error(
                "[VOICE] pause_stream failed  chat_id={}  error={}", chat_id, exc,
            )
            return False

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns False on failure."""
        try:
            await self._tgcalls.resume_stream(chat_id)
            logger.debug("[VOICE] Stream resumed  chat_id={}", chat_id)
            return True
        except Exception as exc:
            logger.error(
                "[VOICE] resume_stream failed  chat_id={}  error={}", chat_id, exc,
            )
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
        """Return True when the bot is streaming in this chat."""
        return chat_id in self._active

    # ── Private: membership helpers ───────────────────────────────────────────

    async def _ensure_peer_cached(self, chat_id: int) -> bool:
        """
        Attempt to populate the assistant's in-memory peer cache.

        Returns True  — assistant is (or was already) a member; peer cached.
        Returns False — assistant is NOT a member of this chat.

        A successful get_chat() is the definitive signal that the assistant
        is a member, because Telegram only returns full channel info to members.
        """
        try:
            await self._client.get_chat(chat_id)
            logger.debug(
                "[VOICE] Peer cache OK (assistant is member)  chat_id={}", chat_id
            )
            return True
        except Exception as exc:
            logger.info(
                "[VOICE] Assistant is not a member of chat_id={}  "
                "(get_chat error: {}) — attempting auto-join",
                chat_id, exc,
            )
            return False

    async def _try_auto_join(self, chat_id: int) -> bool:
        """
        Attempt to auto-join a public group using the bot client for lookup.

        Flow:
          1. Use bot_client.get_chat() to learn the group's username.
             (The bot always knows the group — it received the /play command.)
          2. If the group has a username → assistant.join_chat(username).
          3. If no username (private group) → raise PrivateGroupError.
          4. join_chat raises → log + return False.

        Returns True when the assistant successfully joined.
        Returns False on any failure — caller will return False to controller,
        which raises VoiceChatError with a clear user-facing message.
        """
        # ── Get group info via bot client ──────────────────────────────────
        info_client = self._bot if self._bot is not None else self._client

        try:
            chat = await info_client.get_chat(chat_id)
        except Exception as exc:
            logger.error(
                "[VOICE] Could not fetch group info for auto-join  "
                "chat_id={}  error={}",
                chat_id, exc,
            )
            return False

        username: Optional[str] = getattr(chat, "username", None)

        if not username:
            # Private group — cannot auto-join without an invite link.
            # Raise PrivateGroupError (subclass of VoiceChatError) so the
            # handler can show the specific manual-add message instead of
            # the generic "Could not join voice chat" message.
            logger.warning(
                "[VOICE] Group is PRIVATE — assistant must be added manually  "
                "chat_id={}",
                chat_id,
            )
            raise PrivateGroupError(
                f"Group {chat_id} is private. "
                "An admin must add the assistant account manually."
            )

        # ── Attempt join via public username ───────────────────────────────
        logger.info(
            "[VOICE] Joining PUBLIC group @{}  chat_id={}", username, chat_id
        )
        try:
            await self._client.join_chat(username)
            logger.info(
                "[VOICE] Assistant joined @{}  chat_id={}", username, chat_id
            )
            return True

        except UserAlreadyParticipant:
            # Race condition: assistant joined between our check and join attempt.
            logger.debug(
                "[VOICE] Assistant was already in group @{}  chat_id={}",
                username, chat_id,
            )
            return True

        except Exception as exc:
            logger.error(
                "[VOICE] join_chat(@{}) failed  chat_id={}  error={}",
                username, chat_id, exc,
            )
            return False

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _safe_leave(self, chat_id: int) -> None:
        try:
            await self._tgcalls.leave_call(chat_id)
            logger.info("[VOICE] Left VC  chat_id={}", chat_id)
        except Exception as exc:
            logger.debug(
                "[VOICE] leave_call suppressed  chat_id={}  error={}", chat_id, exc,
            )

    def _register_callbacks(self) -> None:
        """
        Register py-tgcalls 2.3.3 event handlers.

        Three separate handlers (not one monolithic on_update) for clarity.
        StreamAudioEnded is caught via isinstance() because it requires
        a general on_update() — there is no filter for it in 2.3.3.
        """

        # ── 1: Audio track finished naturally ──────────────────────────────
        @self._tgcalls.on_update()
        async def _on_update(_client: PyTgCalls, update: Update) -> None:
            if not isinstance(update, StreamAudioEnded):
                return

            chat_id: int = update.chat_id

            if self.is_skip_in_progress(chat_id):
                logger.debug(
                    "[VOICE] StreamAudioEnded suppressed (skip in progress)  "
                    "chat_id={}",
                    chat_id,
                )
                return

            logger.info("[VOICE] Stream ended naturally  chat_id={}", chat_id)
            self._active.discard(chat_id)
            await self._fire_stream_end(chat_id)

        # ── 2: VC closed by admin ──────────────────────────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.CLOSED_VOICE_CHAT)
        )
        async def _on_vc_closed(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("[VOICE] VC closed by admin  chat_id={}", chat_id)
            was_active = chat_id in self._active
            self._active.discard(chat_id)
            if not self.is_skip_in_progress(chat_id) and was_active:
                await self._fire_stream_end(chat_id)

        # ── 3: Bot kicked from group ───────────────────────────────────────
        @self._tgcalls.on_update(
            fl.chat_update(ChatUpdate.Status.KICKED)
        )
        async def _on_kicked(_client: PyTgCalls, update: Update) -> None:
            chat_id = update.chat_id
            logger.warning("[VOICE] Bot kicked from chat  chat_id={}", chat_id)
            was_active = chat_id in self._active
            self._active.discard(chat_id)
            if not self.is_skip_in_progress(chat_id) and was_active:
                await self._fire_stream_end(chat_id)

        logger.debug(
            "[VOICE] Handlers registered: "
            "StreamAudioEnded + CLOSED_VOICE_CHAT + KICKED"
        )

    async def _fire_stream_end(self, chat_id: int) -> None:
        """Safely invoke the registered on_stream_end callback."""
        if self._on_stream_end is not None:
            try:
                await self._on_stream_end(chat_id)
            except Exception as exc:
                logger.error(
                    "[VOICE] _fire_stream_end callback raised  "
                    "chat_id={}  error={}",
                    chat_id, exc,
                )
