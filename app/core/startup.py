"""
app.core.startup
~~~~~~~~~~~~~~~~
Manages the full application lifecycle: initialise -> run -> shutdown.

Phase 1 init order
------------------
1. Database  (MongoDB Atlas)
2. Bot client (Pyrogram bot)
3. Assistant client (Pyrogram user — optional but strongly recommended)
4. Voice-chat manager (PyTgCalls)
5. Music engine (orchestrator)
6. Music handlers  (registered onto bot client)
7. OS signals

Fixes applied (audit 2026-08-05)
---------------------------------
* asyncio.get_running_loop() replaces deprecated get_event_loop() in
  _register_signals().
* YouTubeService.shutdown() called in stop() so the executor is cleanly
  drained on process exit.
* owner_id passed to register_music_handlers so admin guard works.
* Phase label updated to Phase 1.
* WARNING log clearly explains consequences of missing ASSISTANT_SESSION.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from pyrogram import Client

from app.core.config import Settings
from app.core.logger import logger
from app.database.connection import DatabaseManager
from app.bot.client import BotClient


class ApplicationLifecycle:
    """
    Orchestrates startup and graceful shutdown.

    Usage
    -----
    lifecycle = ApplicationLifecycle(settings)
    await lifecycle.start()
    await lifecycle.wait()       # blocks until SIGINT / SIGTERM
    await lifecycle.stop()
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db: Optional[DatabaseManager] = None
        self._bot: Optional[BotClient] = None
        self._assistant: Optional[Client] = None
        self._shutdown_event = asyncio.Event()
        self._yt_service = None   # kept for shutdown

        # Phase 1 sub-systems (imported lazily to keep Phase 0 import-clean)
        self._vc_manager = None
        self._engine = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("=" * 60)
        logger.info("  ShadeMusicBot v1.0 — Phase 1 (Music Engine)")
        logger.info("=" * 60)

        await self._init_database()
        await self._init_bot()
        await self._init_music_engine()
        self._register_signals()

        logger.info("=" * 60)
        logger.info("  All systems operational")
        logger.info("=" * 60)

    async def stop(self) -> None:
        logger.info("Shutdown signal received — stopping gracefully...")

        if self._vc_manager is not None:
            await self._vc_manager.stop()
            logger.info("Voice-chat manager stopped")

        if self._assistant is not None:
            try:
                await self._assistant.stop()
                logger.info("Assistant client stopped")
            except Exception:
                pass

        if self._bot is not None:
            await self._bot.stop()
            logger.info("Bot client stopped")

        if self._db is not None:
            await self._db.disconnect()
            logger.info("Database disconnected")

        # Shut down the yt-dlp executor so its threads are released cleanly.
        if self._yt_service is not None:
            self._yt_service.shutdown()

        logger.info("Shutdown complete. Goodbye.")

    async def wait(self) -> None:
        await self._shutdown_event.wait()

    # ── Properties (exposed for health endpoint) ──────────────────────────────

    @property
    def db(self) -> Optional[DatabaseManager]:
        return self._db

    @property
    def bot(self) -> Optional[BotClient]:
        return self._bot

    # ── Private init steps ────────────────────────────────────────────────────

    async def _init_database(self) -> None:
        logger.info("Connecting to MongoDB...")
        self._db = DatabaseManager(self._settings)
        await self._db.connect()
        await self._db.ping()
        await self._db.ensure_indexes()
        logger.info(
            "MongoDB connected — database='{}'",
            self._settings.mongo_db_name,
        )

    async def _init_bot(self) -> None:
        logger.info("Starting Telegram bot client...")
        self._bot = BotClient(self._settings, self._db)  # type: ignore[arg-type]
        await self._bot.start()
        me = await self._bot.get_me()
        logger.info(
            "Bot client ready — @{username} (id={id})",
            username=me.username,
            id=me.id,
        )

    async def _init_music_engine(self) -> None:
        """
        Build and wire together the full Phase 1 music stack:
          YouTubeService -> VoiceChatManager -> MusicEngine
        Then register music handlers onto the running bot client.
        """
        from app.player.queue import QueueManager
        from app.player.engine import MusicEngine
        from app.services.youtube import YouTubeService
        from app.streaming.voice_chat import VoiceChatManager
        from app.bot import handlers as music_handlers

        # ── YouTube service ────────────────────────────────────────────────
        yt_service = YouTubeService(cookies_path=self._settings.cookies_path)
        self._yt_service = yt_service  # store reference for shutdown
        logger.info("YouTube service initialised")

        # ── Voice-chat client (user assistant or bot fallback) ─────────────
        vc_client = await self._resolve_vc_client()

        # ── VoiceChatManager (callback set after engine is created) ────────
        vc_manager = VoiceChatManager(vc_client)

        # ── Notify helper — lets engine send "Now Playing" via bot ─────────
        bot_raw = self._bot.client  # type: ignore[union-attr]

        async def _notify(chat_id: int, text: str) -> None:
            try:
                await bot_raw.send_message(chat_id, text)
            except Exception as exc:
                logger.debug("notify failed chat_id={}: {}", chat_id, exc)

        # ── MusicEngine ────────────────────────────────────────────────────
        engine = MusicEngine(
            queue=QueueManager(),
            vc=vc_manager,
            yt=yt_service,
            notify_fn=_notify,
            max_queue_size=self._settings.max_queue_size,
        )

        # ── Wire stream-end callback (breaks the circular dep) ────────────
        vc_manager.set_on_stream_end(engine.on_stream_end)

        # ── Start PyTgCalls ────────────────────────────────────────────────
        await vc_manager.start()

        # ── Register music command handlers ────────────────────────────────
        # Pass owner_id so the admin guard on /skip and /stop works.
        self._bot.register_music_handlers(  # type: ignore[union-attr]
            engine,
            owner_id=self._settings.owner_id,
        )

        # Keep references for clean shutdown
        self._vc_manager = vc_manager
        self._engine = engine

        logger.info("Music engine ready")

    async def _resolve_vc_client(self) -> Client:
        """
        Return the Pyrogram client that PyTgCalls will use to join VCs.

        Priority:
          1. ASSISTANT_SESSION string -> user client (strongly recommended)
          2. Bot client itself (only works with 'Manage Voice Chats' admin perm)

        IMPORTANT: A bot account cannot produce audio in Telegram group voice
        chats without explicit admin-level "Manage Voice Chats" permission.
        Even with that permission, some Telegram server versions reject audio
        from bot accounts entirely.  ALWAYS set ASSISTANT_SESSION.
        """
        session = self._settings.assistant_session

        if session:
            logger.info("Starting assistant (user) client for voice chats...")
            assistant = Client(
                name="ShadeMusicAssistant",
                api_id=self._settings.api_id,
                api_hash=self._settings.api_hash,
                session_string=session,
                in_memory=True,
            )
            await assistant.start()
            me = await assistant.get_me()
            logger.info(
                "Assistant client ready — {} (id={})",
                me.first_name,
                me.id,
            )
            self._assistant = assistant
            return assistant

        logger.warning(
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  ASSISTANT_SESSION is NOT set.                          ║\n"
            "║                                                          ║\n"
            "║  The bot will attempt to join voice chats using its own  ║\n"
            "║  bot account.  This WILL NOT produce audio in most       ║\n"
            "║  groups — Telegram requires a real user account to       ║\n"
            "║  stream audio into voice chats.                          ║\n"
            "║                                                          ║\n"
            "║  Set ASSISTANT_SESSION in your Render environment to     ║\n"
            "║  fix this.  See render.yaml for generation instructions. ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )
        return self._bot.client  # type: ignore[union-attr]

    # ── OS signal registration ────────────────────────────────────────────────

    def _register_signals(self) -> None:
        # get_running_loop() is correct inside a running coroutine.
        # get_event_loop() is deprecated for this use in Python 3.10+.
        loop = asyncio.get_running_loop()

        def _handle(sig: signal.Signals) -> None:
            logger.info("Received signal {}", sig.name)
            self._shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle, sig)
            except (NotImplementedError, RuntimeError):
                pass
