"""
app.bot.client
~~~~~~~~~~~~~~
Thin wrapper around the Pyrogram Client.

Responsibilities
----------------
* Build and configure the Pyrogram Client from Settings.
* Register all handler groups on startup.
* Expose ``get_me()`` so the lifecycle layer can log the bot identity.

Why Pyrogram?
-------------
Future phases need pytgcalls for voice-chat streaming, and pytgcalls
integrates natively with Pyrogram. Starting with Pyrogram avoids a
library migration between phases.
"""

from __future__ import annotations

from typing import Optional

import pyrogram
from pyrogram import Client
from pyrogram.types import User

from app.core.config import Settings
from app.core.logger import logger
from app.database.connection import DatabaseManager


class BotClient:
    """
    Wraps a Pyrogram ``Client`` and manages handler registration.

    Parameters
    ----------
    settings:
        Application settings (tokens, API credentials, etc.).
    db:
        DatabaseManager instance — passed into handlers that need DB access.
    """

    def __init__(self, settings: Settings, db: DatabaseManager) -> None:
        self._settings = settings
        self._db = db
        self._client: Optional[Client] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise the Pyrogram client and register all handlers."""
        self._client = Client(
            name="ShadeMusicBot",
            api_id=self._settings.api_id,
            api_hash=self._settings.api_hash,
            bot_token=self._settings.bot_token,
            # Store session in-memory; no session files on Render's ephemeral FS
            in_memory=True,
            # Disable unwanted noise from pyrogram's own logger
            no_updates=False,
        )

        self._register_handlers()
        await self._client.start()

    async def stop(self) -> None:
        """Stop the Pyrogram client cleanly."""
        if self._client is not None and self._client.is_connected:
            await self._client.stop()

    async def get_me(self) -> User:
        """Return the bot's own Telegram User object."""
        if not self._client:
            raise RuntimeError("BotClient has not been started.")
        return await self._client.get_me()

    @property
    def client(self) -> Client:
        """Access the raw Pyrogram Client (for advanced usage)."""
        if not self._client:
            raise RuntimeError("BotClient has not been started.")
        return self._client

    def register_music_handlers(self, engine: object) -> None:
        """
        Attach Phase 1 music handlers to the running client.

        Called by the lifecycle layer AFTER MusicEngine is fully initialised
        so the engine reference is never None when a command arrives.
        """
        from app.bot.handlers import music as music_handlers

        music_handlers.register(self._client, engine)  # type: ignore[arg-type]
        logger.debug("Music handler group registered")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        """
        Attach base (Phase 0) handlers.

        Music handlers are registered separately via register_music_handlers()
        once the engine is ready, so no handler ever receives a None engine.
        """
        from app.bot.handlers import base as base_handlers

        base_handlers.register(self._client, self._db)  # type: ignore[arg-type]
        logger.debug("Base handler group registered")
