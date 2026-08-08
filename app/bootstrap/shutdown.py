"""
app.bootstrap.shutdown
~~~~~~~~~~~~~~~~~~~~~~
Ordered shutdown functions for every application subsystem.

Shutdown is the reverse of startup.
Each function handles its own errors so a failure in one step does not
prevent the remaining steps from executing.

Shutdown order
--------------
1. Voice chat manager   (leave all active VCs, stop PyTgCalls)
2. Search executors     (drain yt-dlp thread pools)
3. Assistant client     (disconnect Pyrogram user session)
4. Bot client           (disconnect Pyrogram bot session)
5. Database             (close Motor connection pool)

Stage log: [SHUTDOWN]
"""

from __future__ import annotations

from typing import Optional

from pyrogram import Client

from app.infrastructure.database import DatabaseManager
from app.infrastructure.logger import logger
from app.search.resolver import StreamResolver
from app.search.youtube import YouTubeSearch
from app.streaming.voice import VoiceChatManager


async def stop_voice_chat(voice: Optional[VoiceChatManager]) -> None:
    """Leave all active voice chats and stop the PyTgCalls engine."""
    if voice is None:
        return
    try:
        await voice.stop()
        logger.info("[SHUTDOWN] VoiceChatManager stopped")
    except Exception as exc:
        logger.error("[SHUTDOWN] VoiceChatManager.stop() raised: {}", exc)


def stop_search(
    search:   Optional[YouTubeSearch],
    resolver: Optional[StreamResolver],
) -> None:
    """Drain the yt-dlp thread executors so their threads exit cleanly."""
    if search is not None:
        try:
            YouTubeSearch.shutdown()
            logger.info("[SHUTDOWN] YouTubeSearch executor drained")
        except Exception as exc:
            logger.error("[SHUTDOWN] YouTubeSearch.shutdown() raised: {}", exc)

    if resolver is not None:
        try:
            StreamResolver.shutdown()
            logger.info("[SHUTDOWN] StreamResolver executor drained")
        except Exception as exc:
            logger.error("[SHUTDOWN] StreamResolver.shutdown() raised: {}", exc)


async def stop_assistant(assistant: Optional[Client]) -> None:
    """Disconnect the Pyrogram assistant (user) client."""
    if assistant is None:
        return
    try:
        if assistant.is_connected:
            await assistant.stop()
        logger.info("[SHUTDOWN] Assistant client stopped")
    except Exception as exc:
        logger.error("[SHUTDOWN] Assistant.stop() raised: {}", exc)


async def stop_bot(bot: Optional[Client]) -> None:
    """Disconnect the Pyrogram bot client."""
    if bot is None:
        return
    try:
        if bot.is_connected:
            await bot.stop()
        logger.info("[SHUTDOWN] Bot client stopped")
    except Exception as exc:
        logger.error("[SHUTDOWN] Bot.stop() raised: {}", exc)


async def stop_database(db: Optional[DatabaseManager]) -> None:
    """Close the Motor connection pool."""
    if db is None:
        return
    try:
        await db.disconnect()
        logger.info("[SHUTDOWN] Database disconnected")
    except Exception as exc:
        logger.error("[SHUTDOWN] DatabaseManager.disconnect() raised: {}", exc)
