"""
app.core.startup
~~~~~~~~~~~~~~~~
Manages the full application lifecycle: initialise → run → shutdown.

The ApplicationLifecycle class is the single entry point that wires
together the database, bot client, and web server. It is intentionally
separate from each sub-system so Phase 1+ can add new services without
touching unrelated code.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from app.core.config import Settings
from app.core.logger import logger
from app.database.connection import DatabaseManager
from app.bot.client import BotClient


class ApplicationLifecycle:
    """
    Orchestrates start-up and graceful shutdown.

    Usage
    -----
    lifecycle = ApplicationLifecycle(settings)
    await lifecycle.start()
    await lifecycle.wait()          # blocks until SIGINT / SIGTERM
    await lifecycle.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db: Optional[DatabaseManager] = None
        self._bot: Optional[BotClient] = None
        self._shutdown_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Run every initialisation step in the correct order."""
        logger.info("═" * 60)
        logger.info("  ShadeMusicBot — starting up")
        logger.info("═" * 60)

        await self._init_database()
        await self._init_bot()
        self._register_signals()

        logger.info("═" * 60)
        logger.info("  All systems operational ✓")
        logger.info("═" * 60)

    async def stop(self) -> None:
        """Gracefully tear down every sub-system in reverse order."""
        logger.info("Shutdown signal received — stopping gracefully…")

        if self._bot:
            await self._bot.stop()
            logger.info("Bot client stopped ✓")

        if self._db:
            await self._db.disconnect()
            logger.info("Database disconnected ✓")

        logger.info("Shutdown complete. Goodbye.")

    async def wait(self) -> None:
        """Block until a shutdown signal is received."""
        await self._shutdown_event.wait()

    # Expose sub-systems for the health endpoint and other modules
    @property
    def db(self) -> Optional[DatabaseManager]:
        return self._db

    @property
    def bot(self) -> Optional[BotClient]:
        return self._bot

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _init_database(self) -> None:
        logger.info("Connecting to MongoDB…")
        self._db = DatabaseManager(self._settings)
        await self._db.connect()
        await self._db.ping()
        logger.info(
            "MongoDB connected — database='{}'",
            self._settings.mongo_db_name,
        )

    async def _init_bot(self) -> None:
        logger.info("Starting Telegram bot client…")
        self._bot = BotClient(self._settings, self._db)  # type: ignore[arg-type]
        await self._bot.start()
        me = await self._bot.get_me()
        logger.info(
            "Bot client ready — @{username} (id={id})",
            username=me.username,
            id=me.id,
        )

    def _register_signals(self) -> None:
        """Register OS signal handlers for clean Docker / Render shutdown."""
        loop = asyncio.get_event_loop()

        def _handle_signal(sig: signal.Signals) -> None:
            logger.info("Received signal {}", sig.name)
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except (NotImplementedError, RuntimeError):
                # Windows / environments that don't support add_signal_handler
                pass
