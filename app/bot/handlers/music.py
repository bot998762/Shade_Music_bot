"""
app.bot.handlers.music
~~~~~~~~~~~~~~~~~~~~~~
Telegram command handlers for Phase 1 music playback.

Every handler follows the same pattern:
  1. Validate inputs quickly (synchronous).
  2. Send an interim "searching…" / "processing…" message.
  3. Delegate all logic to MusicEngine (async, can be slow).
  4. Edit the interim message with the result.
  5. Catch every exception and report it to the user without crashing.

These handlers are intentionally thin — no business logic lives here.

Fixes applied (audit 2026-08-05)
---------------------------------
* format_now_playing() shared helper used for Now Playing text — consistent
  format between immediate play and auto-advance notifications.
* Per-user rate limiting on /play (3-second cooldown) to prevent executor
  saturation from rapid-fire requests.
* Admin/owner check on /skip and /stop — any group admin or the bot owner
  can control playback; regular users cannot.
* /help updated to include all Phase 1 music commands.
"""

from __future__ import annotations

import time
from typing import Dict

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ChatMemberStatus

from app.core.logger import logger
from app.player.engine import MusicEngine, format_now_playing

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Maps user_id → unix timestamp of last /play invocation.
# Module-level dict is safe for a single-process bot.
_PLAY_LAST_CALL: Dict[int, float] = {}
_PLAY_COOLDOWN_SEC = 3.0


def _is_rate_limited(user_id: int) -> bool:
    """Return True when the user called /play less than _PLAY_COOLDOWN_SEC ago."""
    now = time.monotonic()
    last = _PLAY_LAST_CALL.get(user_id, 0.0)
    if now - last < _PLAY_COOLDOWN_SEC:
        return True
    _PLAY_LAST_CALL[user_id] = now
    return False


# ── Admin check ───────────────────────────────────────────────────────────────

async def _is_admin_or_owner(
    client: Client,
    chat_id: int,
    user_id: int,
    owner_id: int,
) -> bool:
    """
    Return True when *user_id* is the bot owner, a group admin, or the group
    owner.  Falls back to True on API errors (defensive: don't block users
    when Telegram is flaky).
    """
    if user_id == owner_id:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception as exc:
        logger.debug(
            "Admin check failed chat={} user={} — defaulting to allow: {}",
            chat_id, user_id, exc,
        )
        return True  # fail-open so legitimate users aren't blocked by API hiccups


def register(client: Client, engine: MusicEngine, owner_id: int = 0) -> None:
    """
    Attach all music handlers to *client*.

    Parameters
    ----------
    client:
        Pyrogram Client (bot account).
    engine:
        Shared MusicEngine instance.
    owner_id:
        Telegram user ID of the bot owner.  Users with this ID can control
        playback regardless of group admin status.
    """

    # ── /play ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("play") & filters.group)
    async def cmd_play(c: Client, msg: Message) -> None:
        query = " ".join(msg.command[1:]).strip() if len(msg.command) > 1 else ""

        if not query:
            await msg.reply_text(
                "❌ Please provide a song name or URL.\n\n"
                "**Usage:** `/play Believer Imagine Dragons`",
                quote=True,
            )
            return

        user = msg.from_user
        if user is None:
            return

        # Rate limit — prevents executor saturation from rapid-fire requests
        if _is_rate_limited(user.id):
            await msg.reply_text(
                f"⏳ Slow down! Please wait {_PLAY_COOLDOWN_SEC:.0f} seconds "
                "between play requests.",
                quote=True,
            )
            return

        interim = await msg.reply_text(f"🔍 Searching for **{query}**…", quote=True)

        try:
            track, is_now_playing = await engine.play(
                chat_id=msg.chat.id,
                query=query,
                requested_by_id=user.id,
                requested_by_name=user.first_name or user.username or "Unknown",
            )
        except ValueError as exc:
            await interim.edit_text(f"❌ {exc}")
            return
        except RuntimeError as exc:
            await interim.edit_text(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error("/play unhandled error in chat={}: {}", msg.chat.id, exc)
            await interim.edit_text("❌ An unexpected error occurred. Please try again.")
            return

        if is_now_playing:
            # Use shared helper so format matches auto-advance notifications
            await interim.edit_text(format_now_playing(track))
        else:
            upcoming = await engine.get_upcoming(msg.chat.id)
            position = len(upcoming)  # track was just added; it is last in list
            await interim.edit_text(
                f"✅ **Added to Queue**\n\n"
                f"**{track.title}**\n"
                f"⏱ {track.formatted_duration}  •  👤 {track.uploader}\n"
                f"Position in queue: **#{position}**",
            )

    # ── /skip ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("skip") & filters.group)
    async def cmd_skip(c: Client, msg: Message) -> None:
        if not engine.is_active(msg.chat.id):
            await msg.reply_text("❌ Nothing is playing right now.", quote=True)
            return

        user = msg.from_user
        if user is None:
            return

        if not await _is_admin_or_owner(c, msg.chat.id, user.id, owner_id):
            await msg.reply_text(
                "❌ Only group admins can skip tracks.", quote=True
            )
            return

        try:
            next_track = await engine.skip(msg.chat.id)
        except Exception as exc:
            logger.error("/skip error in chat={}: {}", msg.chat.id, exc)
            await msg.reply_text("❌ Failed to skip. Please try again.", quote=True)
            return

        if next_track is not None:
            await msg.reply_text(
                f"⏭ **Skipped!**\n\n"
                f"🎵 **Now Playing:** {next_track.title}\n"
                f"⏱ {next_track.formatted_duration}  •  👤 {next_track.uploader}",
                quote=True,
            )
        else:
            await msg.reply_text(
                "⏭ **Skipped!**\n\nQueue is empty — leaving the voice chat.",
                quote=True,
            )

    # ── /stop ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("stop") & filters.group)
    async def cmd_stop(c: Client, msg: Message) -> None:
        if not engine.is_active(msg.chat.id):
            await msg.reply_text("❌ Nothing is playing right now.", quote=True)
            return

        user = msg.from_user
        if user is None:
            return

        if not await _is_admin_or_owner(c, msg.chat.id, user.id, owner_id):
            await msg.reply_text(
                "❌ Only group admins can stop playback.", quote=True
            )
            return

        try:
            await engine.stop(msg.chat.id)
            await msg.reply_text(
                "⏹ **Stopped!**\n\nPlayback stopped and queue cleared.",
                quote=True,
            )
        except Exception as exc:
            logger.error("/stop error in chat={}: {}", msg.chat.id, exc)
            await msg.reply_text("❌ Failed to stop. Please try again.", quote=True)

    # ── /pause ────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("pause") & filters.group)
    async def cmd_pause(c: Client, msg: Message) -> None:
        if not engine.is_active(msg.chat.id):
            await msg.reply_text("❌ Nothing is playing right now.", quote=True)
            return

        try:
            ok = await engine.pause(msg.chat.id)
            if ok:
                await msg.reply_text("⏸ **Paused.**", quote=True)
            else:
                await msg.reply_text("❌ Could not pause the stream.", quote=True)
        except Exception as exc:
            logger.error("/pause error in chat={}: {}", msg.chat.id, exc)
            await msg.reply_text("❌ Failed to pause. Please try again.", quote=True)

    # ── /resume ───────────────────────────────────────────────────────────────
    @client.on_message(filters.command("resume") & filters.group)
    async def cmd_resume(c: Client, msg: Message) -> None:
        if not engine.is_active(msg.chat.id):
            await msg.reply_text("❌ Nothing is playing right now.", quote=True)
            return

        try:
            ok = await engine.resume(msg.chat.id)
            if ok:
                await msg.reply_text("▶️ **Resumed.**", quote=True)
            else:
                await msg.reply_text("❌ Could not resume the stream.", quote=True)
        except Exception as exc:
            logger.error("/resume error in chat={}: {}", msg.chat.id, exc)
            await msg.reply_text("❌ Failed to resume. Please try again.", quote=True)

    # ── /queue ────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("queue") & filters.group)
    async def cmd_queue(c: Client, msg: Message) -> None:
        try:
            current = engine.get_current_track(msg.chat.id)
            upcoming = await engine.get_upcoming(msg.chat.id)

            if current is None and not upcoming:
                await msg.reply_text("📭 **Queue is empty.**", quote=True)
                return

            lines: list[str] = ["🎵 **Music Queue**\n"]

            if current is not None:
                lines.append(
                    f"▶️ **Now Playing:**\n"
                    f"   {current.title}  [{current.formatted_duration}]\n"
                    f"   👤 {current.uploader}\n"
                )

            if upcoming:
                lines.append("📋 **Up Next:**")
                display = upcoming[:10]
                for i, track in enumerate(display, start=1):
                    lines.append(
                        f"   {i}. {track.title}  [{track.formatted_duration}]"
                    )
                if len(upcoming) > 10:
                    lines.append(f"\n   _…and {len(upcoming) - 10} more track(s)_")
            else:
                lines.append("_No more tracks in queue._")

            await msg.reply_text("\n".join(lines), quote=True)

        except Exception as exc:
            logger.error("/queue error in chat={}: {}", msg.chat.id, exc)
            await msg.reply_text(
                "❌ Failed to retrieve queue. Please try again.", quote=True
            )

    logger.debug("Music handlers registered ✓")
