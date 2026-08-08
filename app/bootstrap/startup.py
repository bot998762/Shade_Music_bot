"""
app.bootstrap.startup
~~~~~~~~~~~~~~~~~~~~~
Ordered initialisation functions for every application subsystem.

Each function has one job: initialise one sub-system and return it.
They are called in order by ApplicationLifecycle.start() in lifecycle.py.

Startup order
-------------
1. Database          (MongoDB Atlas)
2. Bot client        (Pyrogram bot account)
3. VC client         (Pyrogram assistant account or bot fallback)
4. Search            (YouTubeSearch + StreamResolver)
5. Streaming         (VoiceChatManager)
6. Playback session  (SessionManager + StateManager)
7. Cleanup service   (CleanupService)
8. Stream monitor    (StreamMonitor — wired into VoiceChatManager)
9. Playback controller (PlaybackController)
10. Handler registration (start, help, play)

Each function logs its completion so startup progress is visible in
the Render dashboard.

Stage log: [STARTUP]
"""

from __future__ import annotations

from typing import Optional

from pyrogram import Client

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
from app.streaming.voice import VoiceChatManager


# ── 1. Database ────────────────────────────────────────────────────────────────

async def init_database(settings: Settings) -> DatabaseManager:
    """Connect to MongoDB, ping it, and ensure indexes exist."""
    logger.info("[STARTUP] Connecting to MongoDB...")
    db = DatabaseManager(
        mongo_uri=settings.mongo_uri,
        db_name=settings.mongo_db_name,
    )
    await db.connect()
    await db.ping()
    await db.ensure_indexes()
    logger.info(
        "[STARTUP] MongoDB connected  database='{}'",
        settings.mongo_db_name,
    )
    return db


# ── 2. Bot client ──────────────────────────────────────────────────────────────

async def init_bot_client(settings: Settings) -> Client:
    """Start the Pyrogram bot client."""
    logger.info("[STARTUP] Starting Telegram bot client...")
    bot = Client(
        name="ShadeMusicBot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,    # no session files on Render's ephemeral FS
        no_updates=False,
    )
    await bot.start()
    me = await bot.get_me()
    logger.info(
        "[STARTUP] Bot client ready  @{}  id={}",
        me.username, me.id,
    )
    return bot


# ── 3. VC client ───────────────────────────────────────────────────────────────

async def init_vc_client(
    settings: Settings,
    bot_client: Client,
) -> tuple[Client, Optional[Client]]:
    """
    Return (vc_client, assistant_client).

    vc_client:        the Pyrogram client PyTgCalls uses to join VCs.
    assistant_client: the Pyrogram user client if ASSISTANT_SESSION is set,
                      else None (so lifecycle can stop it on shutdown).

    Priority:
      1. ASSISTANT_SESSION → user account (strongly recommended)
      2. bot_client itself → requires Manage Voice Chats admin permission

    IMPORTANT: A bot account usually cannot produce audio in Telegram voice
    chats. ALWAYS set ASSISTANT_SESSION for production deployments.
    """
    session = settings.assistant_session

    if session:
        logger.info("[STARTUP] Starting assistant (user) client for voice chats...")
        assistant = Client(
            name="ShadeMusicAssistant",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            session_string=session,
            in_memory=True,
        )
        await assistant.start()
        me = await assistant.get_me()
        logger.info(
            "[STARTUP] Assistant client ready  {}  id={}",
            me.first_name, me.id,
        )
        return assistant, assistant

    # No ASSISTANT_SESSION
    logger.warning(
        "[STARTUP] ╔══════════════════════════════════════════════════════════╗\n"
        "[STARTUP] ║  ASSISTANT_SESSION is NOT set.                          ║\n"
        "[STARTUP] ║                                                          ║\n"
        "[STARTUP] ║  The bot will attempt to join voice chats using its own  ║\n"
        "[STARTUP] ║  bot account.  This WILL NOT produce audio in most       ║\n"
        "[STARTUP] ║  groups — Telegram requires a real user account to       ║\n"
        "[STARTUP] ║  stream audio into voice chats.                          ║\n"
        "[STARTUP] ║                                                          ║\n"
        "[STARTUP] ║  Set ASSISTANT_SESSION in your Render environment to     ║\n"
        "[STARTUP] ║  fix this.  See render.yaml for generation instructions. ║\n"
        "[STARTUP] ╚══════════════════════════════════════════════════════════╝"
    )
    return bot_client, None


# ── 4. Search ──────────────────────────────────────────────────────────────────

def init_search(settings: Settings) -> tuple[YouTubeSearch, StreamResolver]:
    """Initialise YouTubeSearch and StreamResolver."""
    logger.info("[STARTUP] Initialising search (YouTubeSearch + StreamResolver)...")
    search   = YouTubeSearch(cookies_path=settings.cookies_path)
    resolver = StreamResolver(cookies_path=settings.cookies_path)
    logger.info("[STARTUP] Search initialised")
    return search, resolver


# ── 5. Streaming ───────────────────────────────────────────────────────────────

async def init_voice_chat(vc_client: Client) -> VoiceChatManager:
    """Start the VoiceChatManager (PyTgCalls engine)."""
    logger.info("[STARTUP] Starting VoiceChatManager (PyTgCalls)...")
    voice = VoiceChatManager(vc_client)
    await voice.start()
    logger.info("[STARTUP] VoiceChatManager ready")
    return voice


# ── 6. Playback session ────────────────────────────────────────────────────────

def init_session_and_state() -> tuple[SessionManager, StateManager]:
    """Create the in-memory session and state managers."""
    session = SessionManager()
    state   = StateManager()
    logger.debug("[STARTUP] SessionManager + StateManager created")
    return session, state


# ── 7. Cleanup service ─────────────────────────────────────────────────────────

def init_cleanup(
    voice:   VoiceChatManager,
    session: SessionManager,
    state:   StateManager,
) -> CleanupService:
    """Create the CleanupService."""
    cleanup = CleanupService(voice=voice, session=session, state=state)
    logger.debug("[STARTUP] CleanupService created")
    return cleanup


# ── 8. Stream monitor ──────────────────────────────────────────────────────────

def init_monitor(
    voice:      VoiceChatManager,
    controller: "PlaybackController",
) -> StreamMonitor:
    """
    Create the StreamMonitor and wire it into VoiceChatManager.

    StreamMonitor is a passive observer. It holds only a reference to
    PlaybackController so it can call controller.advance(chat_id) when a
    stream-end event fires. All playback decisions are made by the controller.
    """
    monitor = StreamMonitor(controller=controller)
    # Wire: VoiceChatManager fires stream-end → StreamMonitor relays to controller
    voice.set_on_stream_end(monitor.on_stream_end)
    logger.debug("[STARTUP] StreamMonitor wired into VoiceChatManager")
    return monitor


# ── 9. Playback controller ─────────────────────────────────────────────────────

def init_controller(
    search:       YouTubeSearch,
    resolver:     StreamResolver,
    voice:        VoiceChatManager,
    session:      SessionManager,
    state:        StateManager,
    cleanup:      CleanupService,
    notify_fn,
    settings:     Settings,
) -> PlaybackController:
    """Create the PlaybackController."""
    controller = PlaybackController(
        search=search,
        resolver=resolver,
        voice=voice,
        session=session,
        state=state,
        cleanup=cleanup,
        notify=notify_fn,
        max_queue=settings.max_queue_size,
        cookies_path=settings.cookies_path,
    )
    logger.info("[STARTUP] PlaybackController created")
    return controller


# ── 10. Handlers ───────────────────────────────────────────────────────────────

def init_handlers(
    bot_client:  Client,
    db:          DatabaseManager,
    controller:  PlaybackController,
) -> None:
    """Register all command handlers onto the bot client."""
    logger.info("[STARTUP] Registering command handlers...")

    # Import here to avoid any accidental early import of handler modules
    from app.handlers import help as help_handler
    from app.handlers import play as play_handler
    from app.handlers import start as start_handler

    # Registration order determines handler group priority
    start_handler.register(bot_client, db)
    help_handler.register(bot_client)
    play_handler.register(bot_client, controller)

    logger.info("[STARTUP] Handlers registered: /start /help /play")
