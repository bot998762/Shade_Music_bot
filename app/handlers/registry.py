"""
app.handlers.registry
~~~~~~~~~~~~~~~~~~~~~~
Command registry — the single source of truth for all bot commands.

Every handler module calls ``register_command()`` when it registers its
handlers with the Pyrogram client.  ``/help`` reads the registry at
request time, so future phases automatically appear in help output
without touching help.py.

Usage
-----
From a handler module:

    from app.handlers.registry import register_command, CommandInfo

    def register(client, ...):
        register_command(CommandInfo(
            command="play",
            description="Search YouTube and play in voice chat",
            usage="/play <song name or URL>",
            group_only=True,
        ))
        @client.on_message(...)
        async def cmd_play(...): ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CommandInfo:
    """Metadata for a single bot command."""

    command:     str              # without leading slash, e.g. "play"
    description: str              # one-line description shown in /help
    usage:       Optional[str] = None    # usage example, e.g. "/play <query>"
    group_only:  bool          = False   # True if the command only works in groups


# ── Registry ──────────────────────────────────────────────────────────────────
# Module-level list — populated during startup when handlers register.
_registry: List[CommandInfo] = []


def register_command(info: CommandInfo) -> None:
    """Add a command to the registry. Called once per command at startup."""
    _registry.append(info)


def get_all_commands() -> List[CommandInfo]:
    """Return a snapshot of all registered commands, in registration order."""
    return list(_registry)
