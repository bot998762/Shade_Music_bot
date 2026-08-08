"""
app.handlers.start
~~~~~~~~~~~~~~~~~~
/start command handler.

Responsibility
--------------
Validate the message.
Read bot information.
Read current version and phase.
Generate a professional welcome message.
Display available commands.

Rules
-----
No heavy work.
No database writes beyond lazy user registration.
No playback logic.
No business logic — this is only a command receiver.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app.handlers.registry import CommandInfo, get_all_commands, register_command
from app.infrastructure.database import DatabaseManager
from app.infrastructure.logger import logger
from app.shared.constants import BOT_NAME, BOT_PHASE, BOT_VERSION


def register(client: Client, db: DatabaseManager) -> None:
    """
    Attach the /start handler to *client*.

    Parameters
    ----------
    client:
        Running Pyrogram Client (bot account).
    db:
        DatabaseManager for lazy user registration.
    """
    register_command(CommandInfo(
        command="start",
        description="Start the bot and show the welcome message",
        usage="/start",
        group_only=False,
    ))

    @client.on_message(filters.command("start") & (filters.private | filters.group))
    async def cmd_start(c: Client, msg: Message) -> None:
        """Welcome message — registers new users in the database."""
        # ── Validation ────────────────────────────────────────────────────
        if msg.from_user is None:
            return

        user = msg.from_user

        # ── Lazy user registration ─────────────────────────────────────────
        try:
            is_new = await db.users.register_or_update(
                user_id=user.id,
                first_name=user.first_name or "",
                username=user.username,
                last_name=user.last_name,
                language_code=user.language_code,
            )
            if is_new:
                logger.info(
                    "[START] New user registered  id={}  username=@{}",
                    user.id, user.username,
                )
        except Exception as exc:
            # DB failure must never crash the welcome message
            logger.warning("[START] DB registration failed: {}", exc)

        # ── Read bot info ──────────────────────────────────────────────────
        try:
            me = await c.get_me()
            bot_name = me.first_name or BOT_NAME
        except Exception:
            bot_name = BOT_NAME

        # ── Build commands list from the registry ──────────────────────────
        # The registry is fully populated before any user command arrives,
        # so this is always complete and never requires a hardcoded list.
        commands = get_all_commands()
        group_lines   = [
            f"  • `/{cmd.command}` — {cmd.description}"
            for cmd in commands if cmd.group_only
        ]
        general_lines = [
            f"  • `/{cmd.command}` — {cmd.description}"
            for cmd in commands if not cmd.group_only
        ]

        cmd_block = ""
        if group_lines:
            cmd_block += "🎧 **Music** _(in a group voice chat)_\n"
            cmd_block += "\n".join(group_lines) + "\n\n"
        if general_lines:
            cmd_block += "⚙️ **General**\n"
            cmd_block += "\n".join(general_lines)

        # ── Generate welcome message ───────────────────────────────────────
        text = (
            f"🎵 **{bot_name}**\n\n"
            "Your high-quality Telegram Voice Chat music bot.\n\n"
            f"{cmd_block}\n\n"
            "**To get started:** open or join a voice chat in your group, then send:\n"
            "`/play Believer Imagine Dragons`\n\n"
            f"_v{BOT_VERSION} — {BOT_PHASE}_"
        )

        await msg.reply_text(text, quote=True)
        logger.debug("[START] Welcome sent  user_id={}", user.id)

    logger.debug("[START] Handler registered ✓")
