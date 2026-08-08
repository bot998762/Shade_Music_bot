"""
app.bootstrap.lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~
ApplicationLifecycle — orchestrates startup and graceful shutdown.

Responsibility
--------------
Call the startup functions in the correct order.
Hold references to all sub-system objects so shutdown can reach them.
Block on the shutdown event (SIGINT / SIGTERM).
Call the shutdown functions in reverse order.

The lifecycle object is also injected into the health module so
health.py can inspect bot/db state without a circular import.

Usage
-----
    lifecycle = ApplicationLifecycle(settings)
    await lifecycle.start()
    await lifecycle.wait()        # blocks until signal received
    await lifecycle.stop()
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from pyrogram import Client

from app.bootstrap import shutdown as _shutdown
from app.bootstrap import startup as _startup
from app.infrastructure.config import Settings
from app.infrastructure.database import DatabaseManager
from app.infrastructure.logger import logger
from app.playback.cleanup import CleanupService
from app.playback.controller import PlaybackController
from app.playback.monitor import StreamMonitor
from app.playback.session import SessionManager
from app.playback.state import StateManager
from app.search.resolver import StreamResolver
from app.search.youtube import YouTubeSearch
from app.shared.constants import BOT_NAME, BOT_VERSION
from app.streaming.voice import VoiceChatManager


class ApplicationLifecycle:
    """
    Manages the full application lifecycle.

    All sub-system references are stored here so:
      * Shutdown can reach every resource.
      * The health endpoint can inspect bot/db state.
      * Future phases can introspect the running engine without globals.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings         = settings
        self._shutdown_event   = asyncio.Event()

        # Sub-system references (populated during start())
        self.db:          Optional[DatabaseManager]    = None
        self.bot_client:  Optional[Client]             = None
        self._assistant:  Optional[Client]             = None   # private: user acct
        self._voice:      Optional[VoiceChatManager]   = None
        self._search:     Optional[YouTubeSearch]      = None
        self._resolver:   Optional[StreamResolver]     = None
        self._session:    Optional[SessionManager]     = None
        self._state:      Optional[StateManager]       = None
        self._cleanup:    Optional[CleanupService]     = None
        self._monitor:    Optional[StreamMonitor]      = None
        self.controller:  Optional[PlaybackController] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialise all sub-systems in the correct order."""
        _banner()

        # 1. Database
        self.db = await _startup.init_database(self._settings)

        # 2. Bot client
        self.bot_client = await _startup.init_bot_client(self._settings)

        # 3. VC client (assistant or bot fallback)
        vc_client, self._assistant = await _startup.init_vc_client(
            self._settings, self.bot_client,
        )

        # 4. Search
        self._search, self._resolver = _startup.init_search(self._settings)

        # 5. Voice chat manager
        self._voice = await _startup.init_voice_chat(vc_client)

        # 6. Session + state managers
        self._session, self._state = _startup.init_session_and_state()

        # 7. Cleanup service
        self._cleanup = _startup.init_cleanup(
            voice=self._voice,
            session=self._session,
            state=self._state,
        )

        # 8. Notify function — sends "Now Playing" messages via the bot
        notify_fn = _make_notify(self.bot_client)

        # 9. Playback controller — sole playback authority, created before monitor
        #    so the monitor can receive a reference to it.
        self.controller = _startup.init_controller(
            search=self._search,
            resolver=self._resolver,
            voice=self._voice,
            session=self._session,
            state=self._state,
            cleanup=self._cleanup,
            notify_fn=notify_fn,
            settings=self._settings,
        )

        # 10. Stream monitor — passive observer only.
        #     Wired into VoiceChatManager so it receives stream-end events
        #     and relays them to controller.advance(chat_id).
        self._monitor = _startup.init_monitor(
            voice=self._voice,
            controller=self.controller,
        )

        # 11. Handler registration
        _startup.init_handlers(
            bot_client=self.bot_client,
            db=self.db,
            controller=self.controller,
        )

        # 12. OS signal handlers
        self._register_signals()

        logger.info("=" * 60)
        logger.info("  All systems operational")
        logger.info("=" * 60)

    async def stop(self) -> None:
        """Shut down all sub-systems in reverse startup order."""
        logger.info("[SHUTDOWN] Starting graceful shutdown...")

        await _shutdown.stop_voice_chat(self._voice)
        _shutdown.stop_search(self._search, self._resolver)
        await _shutdown.stop_assistant(self._assistant)
        await _shutdown.stop_bot(self.bot_client)
        await _shutdown.stop_database(self.db)

        logger.info("[SHUTDOWN] Complete. Goodbye.")

    async def wait(self) -> None:
        """Block until a SIGINT / SIGTERM is received."""
        await self._shutdown_event.wait()

    # ── Signal handling ───────────────────────────────────────────────────────

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()

        def _handle(sig: signal.Signals) -> None:
            logger.info("Received signal {}", sig.name)
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle, sig)
            except (NotImplementedError, RuntimeError):
                # Windows / some CI environments do not support add_signal_handler
                pass


# ── Private helpers ────────────────────────────────────────────────────────────

def _banner() -> None:
    logger.info("=" * 60)
    logger.info("  {} v{}", BOT_NAME, BOT_VERSION)
    logger.info("=" * 60)


def _make_notify(bot_client: Client):
    """
    Return an async callable that sends a message via the bot client.

    Errors are suppressed so a failed notification never crashes the monitor.
    """
    async def _notify(chat_id: int, text: str) -> None:
        try:
            await bot_client.send_message(chat_id, text)
        except Exception as exc:
            logger.debug("notify failed  chat_id={}  error={}", chat_id, exc)

    return _notify
