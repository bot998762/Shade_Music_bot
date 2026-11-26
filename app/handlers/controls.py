"""
app.handlers.controls
~~~~~~~~~~~~~~~~~~~~~
Playback control commands: /skip /stop /end /leave /queue /nowplaying.

Responsibility
--------------
Validate the message, call PlaybackController, display the result.

Rules
-----
No playback logic, no queue access, no voice calls: everything goes through
the controller.  /stop, /end and /leave are ONE operation (controller.stop).

Not in this module (deferred): /pause and /resume — the py-tgcalls 2.3.3
method names could not be verified from reliable documentation or source.
Permissions: until DJ mode (a later phase) any group member can use these.
"""

from __future__ import annotations

from typing import List

from pyrogram import Client, filters
from pyrogram.types import Message

from app.handlers.registry import CommandInfo, register_command
from app.infrastructure.logger import logger
from app.playback.controller import PlaybackController, PlaybackSnapshot, SkipOutcome
from app.shared.errors import (
    NOTHING_PLAYING,
    NOW_PLAYING_LINE,
    QUEUE_EMPTY,
    QUEUE_HEADER,
    SKIP_DONE,
    SKIP_DUPLICATE,
    SKIP_FAILED,
    SKIP_QUEUE_ENDED,
    STOP_DONE,
)

_STOP_COMMANDS = ["stop", "end", "leave"]


def format_now_playing(snap: PlaybackSnapshot) -> str:
    """Text for /nowplaying (pure function — unit tested)."""
    t = snap.current
    if snap.is_idle or t is None:
        return NOTHING_PLAYING
    return NOW_PLAYING_LINE.format(
        title=t.title,
        duration=t.formatted_duration,
        uploader=t.uploader,
        requested_by=t.requested_by_name,
        upcoming=len(snap.upcoming),
        cap=snap.cap,
    )


def format_queue(snap: PlaybackSnapshot) -> str:
    """Text for /queue: upcoming tracks only, current track excluded."""
    if snap.is_idle and not snap.upcoming:
        return NOTHING_PLAYING
    if not snap.upcoming:
        return QUEUE_EMPTY
    lines: List[str] = [QUEUE_HEADER.format(count=len(snap.upcoming), cap=snap.cap), ""]
    for i, t in enumerate(snap.upcoming, start=1):
        lines.append(f"{i}. **{t.title}** — {t.formatted_duration}  ({t.requested_by_name})")
    return "\n".join(lines)


def format_skip(outcome: SkipOutcome) -> str:
    """Reply text for a /skip outcome (the new track is announced by the controller)."""
    return {
        SkipOutcome.QUEUE_ENDED:     SKIP_QUEUE_ENDED,
        SkipOutcome.FAILED:          SKIP_FAILED,
        SkipOutcome.DUPLICATE:       SKIP_DUPLICATE,
        SkipOutcome.STALE:           SKIP_DUPLICATE,
        SkipOutcome.CANCELLED:       NOTHING_PLAYING,
        SkipOutcome.NOTHING_PLAYING: NOTHING_PLAYING,
        SkipOutcome.SKIPPED:         SKIP_DONE,
    }[outcome]


def register(client: Client, controller: PlaybackController) -> None:
    """Attach the playback-control handlers to *client*."""
    register_command(CommandInfo(command="skip", description="Skip the current track",
                                 usage="/skip", group_only=True))
    register_command(CommandInfo(command="stop", description="Stop playback, clear the queue and leave the voice chat",
                                 usage="/stop", group_only=True))
    register_command(CommandInfo(command="end", description="Same as /stop",
                                 usage="/end", group_only=True))
    register_command(CommandInfo(command="leave", description="Same as /stop",
                                 usage="/leave", group_only=True))
    register_command(CommandInfo(command="queue", description="Show the upcoming tracks",
                                 usage="/queue", group_only=True))
    register_command(CommandInfo(command="nowplaying", description="Show the current track",
                                 usage="/nowplaying", group_only=True))

    @client.on_message(filters.command("skip") & filters.group)
    async def cmd_skip(c: Client, msg: Message) -> None:
        if msg.from_user is None:
            return
        try:
            result = await controller.skip(msg.chat.id)
        except Exception as exc:
            logger.error("[SKIP] Unhandled error  chat_id={}  error={}", msg.chat.id, exc)
            await msg.reply_text(SKIP_FAILED, quote=True)
            return
        # A successful skip already triggers the controller's Now Playing message.
        await msg.reply_text(format_skip(result.outcome), quote=True)

    @client.on_message(filters.command(_STOP_COMMANDS) & filters.group)
    async def cmd_stop(c: Client, msg: Message) -> None:
        if msg.from_user is None:
            return
        try:
            had_session = await controller.stop(msg.chat.id)
        except Exception as exc:
            logger.error("[STOP] Unhandled error  chat_id={}  error={}", msg.chat.id, exc)
            await msg.reply_text("❌ Could not stop playback cleanly. Please try again.", quote=True)
            return
        await msg.reply_text(STOP_DONE if had_session else NOTHING_PLAYING, quote=True)

    @client.on_message(filters.command("queue") & filters.group)
    async def cmd_queue(c: Client, msg: Message) -> None:
        await msg.reply_text(format_queue(await controller.snapshot(msg.chat.id)), quote=True)

    @client.on_message(filters.command("nowplaying") & filters.group)
    async def cmd_nowplaying(c: Client, msg: Message) -> None:
        await msg.reply_text(format_now_playing(await controller.snapshot(msg.chat.id)), quote=True)

    logger.debug("[CONTROLS] Handlers registered ✓")
