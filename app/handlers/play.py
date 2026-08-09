"""
app.handlers.play
~~~~~~~~~~~~~~~~~
/play command handler.

Responsibility
--------------
Receive the /play command from Telegram.
Validate the query.
Apply rate limiting.
Delegate ALL logic to PlaybackController.
Edit the interim message with the result.
Catch exceptions and report them cleanly to the user.

Rules
-----
No business logic in this file.
No YouTube search calls.
No stream resolution.
No voice chat logic.
The handler only calls PlaybackController — nothing else.
"""

from __future__ import annotations

import time
from typing import Dict

from pyrogram import Client, filters
from pyrogram.types import Message

from app.handlers.registry import CommandInfo, register_command
from app.infrastructure.logger import logger
from app.playback.controller import PlaybackController
from app.shared.constants import PLAY_COOLDOWN_SECONDS
from app.shared.errors import (
    ADDED_TO_QUEUE,
    NOW_PLAYING,
    PLAY_NO_QUERY,
    PLAY_NO_RESULTS,
    PLAY_NO_VOICE_CHAT,
    PLAY_PRIVATE_GROUP,
    PLAY_QUEUE_FULL,
    PLAY_RATE_LIMITED,
    PLAY_UNEXPECTED_ERROR,
    SEARCHING,
)
from app.shared.exceptions import (
    NoResultsError,
    PrivateGroupError,
    QueueFullError,
    VoiceChatError,
)
from app.shared.validators import normalise_query, validate_play_query

# ── Per-user rate limiting ────────────────────────────────────────────────────
# Maps user_id → monotonic timestamp of last /play call.
# Module-level dict is safe for a single-process bot.
_LAST_PLAY: Dict[int, float] = {}


def _is_rate_limited(user_id: int) -> bool:
    """Return True when the user called /play within the cooldown window."""
    now  = time.monotonic()
    last = _LAST_PLAY.get(user_id, 0.0)
    if now - last < PLAY_COOLDOWN_SECONDS:
        return True
    _LAST_PLAY[user_id] = now
    return False


def register(client: Client, controller: PlaybackController) -> None:
    """
    Attach the /play handler to *client*.

    Parameters
    ----------
    client:
        Running Pyrogram Client (bot account).
    controller:
        Shared PlaybackController instance.
    """
    register_command(CommandInfo(
        command="play",
        description="Search YouTube and play in the voice chat",
        usage="/play <song name or URL>",
        group_only=True,
    ))

    @client.on_message(filters.command("play") & filters.group)
    async def cmd_play(c: Client, msg: Message) -> None:
        """Search YouTube and start (or queue) playback."""
        # ── Validation ────────────────────────────────────────────────────
        raw_query = " ".join(msg.command[1:]).strip() if len(msg.command) > 1 else ""
        is_valid, reason = validate_play_query(raw_query)

        if not is_valid:
            await msg.reply_text(PLAY_NO_QUERY, quote=True)
            return

        query = normalise_query(raw_query)
        user  = msg.from_user
        if user is None:
            return

        # ── Rate limiting ──────────────────────────────────────────────────
        if _is_rate_limited(user.id):
            await msg.reply_text(
                PLAY_RATE_LIMITED.format(cooldown=PLAY_COOLDOWN_SECONDS),
                quote=True,
            )
            return

        # ── Interim message ────────────────────────────────────────────────
        interim = await msg.reply_text(
            SEARCHING.format(query=query),
            quote=True,
        )

        # ── Delegate to PlaybackController ─────────────────────────────────
        try:
            track, is_playing_now = await controller.play(
                chat_id=msg.chat.id,
                query=query,
                requested_by_id=user.id,
                requested_by_name=user.first_name or user.username or "Unknown",
            )
        except NoResultsError:
            await interim.edit_text(PLAY_NO_RESULTS.format(query=query))
            return
        except QueueFullError as exc:
            await interim.edit_text(str(exc))
            return
        except PrivateGroupError:
            await interim.edit_text(PLAY_PRIVATE_GROUP)
            return
        except VoiceChatError:
            await interim.edit_text(PLAY_NO_VOICE_CHAT)
            return
        except Exception as exc:
            logger.error(
                "[PLAY] Unhandled error  chat_id={}  error={}",
                msg.chat.id, exc,
            )
            await interim.edit_text(PLAY_UNEXPECTED_ERROR)
            return

        # ── Report result ──────────────────────────────────────────────────
        if is_playing_now:
            text = NOW_PLAYING.format(
                title=track.title,
                duration=track.formatted_duration,
                uploader=track.uploader,
                requested_by=track.requested_by_name,
            )
        else:
            text = ADDED_TO_QUEUE.format(
                title=track.title,
                duration=track.formatted_duration,
                uploader=track.uploader,
                requested_by=track.requested_by_name,
            )

        await interim.edit_text(text)
        logger.info(
            "[PLAY] {} '{}' for user={} in chat={}",
            "Playing" if is_playing_now else "Queued",
            track.title,
            user.id,
            msg.chat.id,
        )

    logger.debug("[PLAY] Handler registered ✓")
