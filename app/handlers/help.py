"""
app.handlers.help
~~~~~~~~~~~~~~~~~
/help command handler.

Responsibility
--------------
Validate the message.
Read the command registry.
Generate command help from registered entries.
Display usage examples.

Future compatibility
--------------------
Help is generated dynamically from handlers.registry.get_all_commands().
When future phases add new commands, they register themselves with the
registry and /help automatically displays them — no changes to this file.

Rules
-----
No business logic.
No hardcoded command list.
No playback calls.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app.handlers.registry import CommandInfo, get_all_commands, register_command
from app.infrastructure.logger import logger
from app.shared.constants import BOT_NAME, BOT_VERSION


def register(client: Client) -> None:
    """
    Attach the /help handler to *client*.

    Parameters
    ----------
    client:
        Running Pyrogram Client (bot account).
    """
    register_command(CommandInfo(
        command="help",
        description="Show this help message",
        usage="/help",
        group_only=False,
    ))

    @client.on_message(filters.command("help") & (filters.private | filters.group))
    async def cmd_help(c: Client, msg: Message) -> None:
        """Generate help text from the command registry."""
        # ── Read command registry ──────────────────────────────────────────
        commands = get_all_commands()

        # ── Separate by context ────────────────────────────────────────────
        general_cmds = [cmd for cmd in commands if not cmd.group_only]
        group_cmds   = [cmd for cmd in commands if cmd.group_only]

        # ── Build help text ────────────────────────────────────────────────
        lines = [f"🎵 **{BOT_NAME} — Commands**\n"]

        if group_cmds:
            lines.append("**Music** _(use in a group with an active voice chat)_")
            for cmd in group_cmds:
                entry = f"• `/{cmd.command}` — {cmd.description}"
                if cmd.usage:
                    entry += f"\n  _Usage:_ `{cmd.usage}`"
                lines.append(entry)
            lines.append("")

        if general_cmds:
            lines.append("**General**")
            for cmd in general_cmds:
                entry = f"• `/{cmd.command}` — {cmd.description}"
                lines.append(entry)
            lines.append("")

        lines.append(
            "💡 **Tip:** Make sure a voice chat is already open in the group "
            "before using /play."
        )
        lines.append(f"\n_v{BOT_VERSION}_")

        await msg.reply_text("\n".join(lines), quote=True)
        logger.debug("[HELP] Sent  user_id={}", getattr(msg.from_user, "id", "?"))

    logger.debug("[HELP] Handler registered ✓")
