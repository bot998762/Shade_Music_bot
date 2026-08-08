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

Stage log: [VOICE]
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional, Set

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import ChatUpdate, MediaStream, Update
from pytgcalls.types.stream import StreamEnded as StreamAudioEnded

from app.infrastructure.logger import logger
from app.shared.exceptions import AssistantNotMemberError
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

# ── Pyrogram peer error patterns ───────────────────────────────────────────────
# Raised when the assistant is not a member of a private supergroup, or when
# the peer cannot be resolved from the int chat_id alone.
_PEER_INVALID = (
    "channelinvalid",
    "channel_invalid",
    "peerinvalid",
    "peer_invalid",
    "peerflood",
)
# Raised when trying to join a group that requires an invite link.
_INVITE_REQUIRED = (
    "invitehashinvalid",
    "invite_hash_invalid",
    "invitehashexpired",
    "invite_hash_expired",
    "channelprivate",
    "channel_private",
    "usernotinenabled",
    "chatwriteforbidden",
)


def _match(exc: Exception, phrases: tuple) -> bool:
    msg = str(exc).lower().replace(" ", "").replace("_", "")
    return any(p.replace("_", "") in msg for p in phrases)


class VoiceChatManager:
    """
    Manages voice-chat connections for all active group chats.

    Parameters
    ----------
    client:
        A started Pyrogram Client (user assistant account).
        Bot accounts cannot produce audio in Telegram voice chats — always
        use a real user account via ASSISTANT_SESSION.
    """

    def __init__(self, client: Client) -> None:
        self._client  = client
        self._tgcalls = PyTgCalls(client)
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
        Join the voice chat and begin streaming.

        Returns True on success.
        Returns False when no active voice chat exists in the group.
        Returns False (with a clear error log) when the assistant is not a
        member and cannot join automatically.
        Automatically falls back to change_stream() if already connected.

        Membership handling
        -------------------
        If get_chat() fails with CHANNEL_INVALID (assistant not in group):
          1. Inspect the group's username via a public lookup attempt.
          2. Public group (has username): attempt join_chat(username).
             If join succeeds, the peer is now cached — proceed normally.
          3. Private group (no username, needs invite): return False immediately
             with a clear log — caller surfaces the "add assistant" message.

        Peer-cache fix (preserved from previous fix)
        ---------------------------------------------
        in_memory=True starts with an empty peer cache.  PyTgCalls calls
        resolve_peer(chat_id) internally.  Without the peer cached, pyrofork
        sends channels.GetChannels with access_hash=0 → 400 CHANNEL_INVALID.
        get_chat() populates the cache with the real access_hash.
        resolve_peer() verifies the peer is resolvable before calling tgcalls.

        Stage log: [VOICE] join+play
        """
        peer_ok = await self._ensure_peer(chat_id)
        if not peer_ok:
            return False

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

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _ensure_peer(self, chat_id: int) -> bool:
        """
        Ensure the assistant's in-memory peer cache holds a valid, resolvable
        peer for *chat_id* before PyTgCalls calls resolve_peer() internally.

        Returns True  — peer is in cache, safe to call tgcalls.play().
        Returns False — peer cannot be resolved; caller should abort and
                        surface the appropriate user message.

        Resolution order
        ----------------
        1. get_chat(chat_id) — populates the peer cache with the real
           access_hash.  For public groups this always succeeds (public info
           visible without membership).  For private groups this raises
           CHANNEL_INVALID when the assistant is not a member.

        2. If step 1 fails with a peer/channel error:
           a. Attempt join_chat(chat_id) — pyrofork can join public supergroups
              using channels.JoinChannel.  On success the peer is now cached
              and the assistant is a member.
           b. If join_chat raises INVITE_HASH / CHANNEL_PRIVATE / similar:
              the group is private and requires an invite link.  Log clearly
              and return False so the caller surfaces the right user message.
           c. If join_chat raises anything else (flood, network, etc.):
              log the error and return False.

        3. Verify with resolve_peer() — confirms the access_hash is actually
           in the in-memory storage before handing off to PyTgCalls.

        Why not invite links
        --------------------
        We never generate or use invite links.  A ChatInviteLink is only
        visible to admins.  The assistant account is not an admin.
        Attempting to use an invite link we don't have raises INVITE_HASH_INVALID.
        The correct action for private groups is: an admin adds the assistant
        manually.  We surface a clear message asking for exactly that.
        """
        # ── Step 1: populate peer cache ────────────────────────────────────
        try:
            await self._client.get_chat(chat_id)
            logger.debug(
                "[VOICE] Peer cache populated via get_chat  chat_id={}", chat_id
            )

        except Exception as get_exc:
            if _match(get_exc, _PEER_INVALID):
                # Peer not in cache — assistant is likely not a member.
                # Try to join as a public supergroup first.
                logger.info(
                    "[VOICE] get_chat() raised peer error — attempting auto-join  "
                    "chat_id={}  error={}",
                    chat_id, get_exc,
                )
                joined = await self._try_join(chat_id)
                if not joined:
                    return False
                # join succeeded — peer is now cached, continue to step 3
            else:
                # Network error, flood wait, etc. — not a membership issue.
                logger.warning(
                    "[VOICE] get_chat() failed (non-peer error)  "
                    "chat_id={}  error={}",
                    chat_id, get_exc,
                )
                # Don't abort: peer may already be cached from startup
                # get_dialogs().  Let resolve_peer() decide below.

        # ── Step 3: verify peer is resolvable ─────────────────────────────
        try:
            peer = await self._client.resolve_peer(chat_id)
            logger.debug(
                "[VOICE] resolve_peer OK  chat_id={}  peer={}", chat_id, peer
            )
            return True

        except Exception as resolve_exc:
            logger.error(
                "[VOICE] resolve_peer() FAILED — peer not in cache after get_chat.  "
                "The assistant must be a member of this group before /play works.  "
                "chat_id={}  error={}",
                chat_id, resolve_exc,
            )
            return False

    async def _try_join(self, chat_id: int) -> bool:
        """
        Attempt to join *chat_id* as a public supergroup.

        Public supergroups have a username; pyrofork can join them directly
        via channels.JoinChannel.  Private groups have no username and require
        an invite link that the assistant does not have.

        Returns True  — assistant joined successfully.
        Returns False — group is private or join failed; caller returns False
                        so the user sees the "add assistant" message.

        We deliberately do NOT try invite links:
          - ChatInviteLink objects are only visible to group admins.
          - The assistant account is not an admin in the target group.
          - Any invite link we might guess or construct raises INVITE_HASH_INVALID.
          - The correct fix for private groups is a human admin adding the assistant.
        """
        # First, try to get the group's username via a public info request.
        # For public supergroups this works even without membership.
        username: Optional[str] = None
        try:
            # Use the raw int ID — pyrofork can still fetch public channel info
            # even if channels.GetChannels with access_hash=0 fails, because
            # resolve_peer may find it via contact/search paths.
            # If this also raises, we fall through to the private-group path.
            chat_info = await self._client.get_chat(chat_id)
            username = getattr(chat_info, "username", None)
        except Exception:
            pass  # can't get username — treat as private

        if not username:
            # Private group: no username available. Cannot join without invite.
            logger.warning(
                "[VOICE] Cannot auto-join — group is private (no public username).  "
                "An admin must add the assistant account to the group manually.  "
                "chat_id={}",
                chat_id,
            )
            raise AssistantNotMemberError("private")

        # Public group: attempt join via username.
        try:
            await self._client.join_chat(username)
            logger.info(
                "[VOICE] Auto-joined public group  chat_id={}  username=@{}",
                chat_id, username,
            )
            return True

        except Exception as join_exc:
            if _match(join_exc, _INVITE_REQUIRED):
                logger.warning(
                    "[VOICE] Auto-join rejected — group appears private despite "
                    "having a username (restricted join, channel, or approval-required).  "
                    "An admin must add the assistant manually.  "
                    "chat_id={}  username=@{}  error={}",
                    chat_id, username, join_exc,
                )
                raise AssistantNotMemberError("join_failed")
            else:
                logger.error(
                    "[VOICE] Auto-join attempt failed  "
                    "chat_id={}  username=@{}  error={}",
                    chat_id, username, join_exc,
                )
                raise AssistantNotMemberError("join_failed")

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
