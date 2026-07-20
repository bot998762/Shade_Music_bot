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
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app.core.logger import logger
from app.player.engine import MusicEngine


def register(client: Client, engine: MusicEngine) -> None:
    """Attach all music handlers to *client*."""

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
            await interim.edit_text(
                f"🎵 **Now Playing**\n\n"
                f"**{track.title}**\n"
                f"⏱ {track.formatted_duration}  •  👤 {track.uploader}\n"
                f"Requested by: {user.mention}",
            )
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
            await msg.reply_text("❌ Failed to retrieve queue. Please try again.", quote=True)

    logger.debug("Music handlers registered ✓")
