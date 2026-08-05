"""
app.bot.handlers.base
~~~~~~~~~~~~~~~~~~~~~
Foundation command handlers: /start, /help, /ping, /info.

These are the only handlers in Phase 0. They serve as:
  • A smoke test that the bot is alive and responding.
  • A template for all future handler modules.

Each handler module must expose a ``register(client, db)`` function
that attaches filters to the Pyrogram client. This keeps handler
registration centralised in BotClient._register_handlers().
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from app.core.logger import logger
from app.database.connection import DatabaseManager

# Timestamp recorded when the process started — used for /ping uptime
_START_TIME: float = time.monotonic()


def register(client: Client, db: DatabaseManager) -> None:
    """Attach all base handlers to *client*."""

    # ── /start ────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("start") & (filters.private | filters.group))
    async def cmd_start(c: Client, msg: Message) -> None:
        """Welcome message — also registers the user in the database."""
        user = msg.from_user
        if user:
            # Lazy import to avoid circular deps
            from app.database.repositories.users import UserRepository
            if db.db is not None:
                user_repo = UserRepository(db.db)
                is_new = await user_repo.register_or_update(
                    user_id=user.id,
                    first_name=user.first_name or "",
                    username=user.username,
                    last_name=user.last_name,
                    language_code=user.language_code,
                )
                if is_new:
                    logger.info("New user registered: id={} username=@{}", user.id, user.username)

        await msg.reply_text(
            "🎵 **ShadeMusicBot**\n\n"
            "Hello! I'm your high-quality Telegram Voice Chat music bot.\n\n"
            "Use /help to see all commands, or type `/play <song name>` "
            "in a group with an active voice chat to start listening!\n\n"
            "_Phase 1 — Music Engine_",
            quote=True,
        )

    # ── /help ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("help") & (filters.private | filters.group))
    async def cmd_help(c: Client, msg: Message) -> None:
        await msg.reply_text(
            "🎵 **ShadeMusicBot — Commands**\n\n"
            "**Music (use in a group with active voice chat)**\n"
            "• `/play <song or URL>` — Search YouTube and play\n"
            "• `/skip`   — Skip to the next track _(admins only)_\n"
            "• `/stop`   — Stop playback and clear queue _(admins only)_\n"
            "• `/pause`  — Pause the current track\n"
            "• `/resume` — Resume a paused track\n"
            "• `/queue`  — Show the current queue\n\n"
            "**General**\n"
            "• /start — Start the bot\n"
            "• /help  — Show this message\n"
            "• /ping  — Check latency\n"
            "• /info  — Show bot info\n\n"
            "💡 **Tip:** Make sure a voice chat is already open in the group "
            "before using /play.",
            quote=True,
        )

    # ── /ping ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("ping") & (filters.private | filters.group))
    async def cmd_ping(c: Client, msg: Message) -> None:
        """Reply with current latency and uptime."""
        sent_at = time.monotonic()
        reply = await msg.reply_text("🏓 Pong!", quote=True)
        latency_ms = round((time.monotonic() - sent_at) * 1000, 2)

        uptime_seconds = int(time.monotonic() - _START_TIME)
        uptime_str = _format_uptime(uptime_seconds)

        await reply.edit_text(
            f"🏓 **Pong!**\n\n"
            f"⚡ Latency: `{latency_ms} ms`\n"
            f"🕐 Uptime:  `{uptime_str}`"
        )

    # ── /info ─────────────────────────────────────────────────────────────────
    @client.on_message(filters.command("info") & (filters.private | filters.group))
    async def cmd_info(c: Client, msg: Message) -> None:
        """Show basic bot information."""
        me = await c.get_me()
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        text = (
            "ℹ️ **ShadeMusicBot — Info**\n\n"
            f"• **Name:** {me.first_name}\n"
            f"• **Username:** @{me.username}\n"
            f"• **ID:** `{me.id}`\n"
            f"• **Phase:** `1 — Music Engine`\n"
            f"• **Time:** `{now_utc}`\n"
        )
        await msg.reply_text(text, quote=True)

    logger.debug("Base handlers registered ✓")


def _format_uptime(seconds: int) -> str:
    """Convert a raw second count to a human-readable uptime string."""
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)
