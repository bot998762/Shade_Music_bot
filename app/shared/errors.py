"""
app.shared.errors
~~~~~~~~~~~~~~~~~
User-facing error message templates.

All strings that are sent to Telegram users live here.
Handlers format these with .format(**kwargs) — never hardcode in handlers.

Adding a message here automatically makes it available across all handlers
without touching business logic files.
"""

from __future__ import annotations

# ── /play ─────────────────────────────────────────────────────────────────────
PLAY_NO_QUERY = (
    "❌ Please provide a song name or URL.\n\n"
    "**Usage:** `/play Believer Imagine Dragons`"
)

PLAY_RATE_LIMITED = (
    "⏳ Slow down! Please wait {cooldown:.0f} seconds between play requests."
)

PLAY_NO_RESULTS = "❌ No results found for: **{query}**"

PLAY_QUEUE_FULL = (
    "❌ Queue is full ({max_size} tracks). "
    "Wait for the current track to finish."
)

PLAY_NO_VOICE_CHAT = (
    "❌ Could not join the voice chat.\n\n"
    "Make sure a voice chat is active and the assistant has permission to join."
)

PLAY_PRIVATE_GROUP = (
    "❌ The assistant is not in this group.\n\n"
    "Please ask an admin to add **@Shade_music_assistant** to the group, "
    "then try again."
)

PLAY_PLAYLIST_NOT_SUPPORTED = (
    "❌ Playlist URLs are not supported yet.\n\n"
    "Please send a single song URL or search by name instead."
)

PLAY_UNEXPECTED_ERROR = (
    "❌ An unexpected error occurred. Please try again."
)

PLAY_STREAM_TIMEOUT = (
    "❌ Stream resolution timed out. Please try again in a moment."
)

PLAY_STREAM_FAILED = (
    "❌ Could not extract the audio stream.\n\n"
    "The video may be unavailable, age-restricted, or region-locked. "
    "Please try a different song."
)

# ── Now playing ───────────────────────────────────────────────────────────────
NOW_PLAYING = (
    "🎵 **Now Playing**\n\n"
    "**{title}**\n"
    "⏱ {duration}  •  👤 {uploader}\n"
    "Requested by: {requested_by}"
)

ADDED_TO_QUEUE = (
    "✅ **Added to Queue**\n\n"
    "**{title}**\n"
    "⏱ {duration}  •  👤 {uploader}\n"
    "Requested by: {requested_by}"
)

PLAY_CANCELLED = "⏹ Playback was stopped before it could start."

# ── Playback controls (/skip /stop /end /leave /queue /nowplaying) ────────────
NOTHING_PLAYING   = "ℹ️ Nothing is playing right now."
SKIP_DONE         = "⏭ Skipped."
SKIP_QUEUE_ENDED = "⏭ Skipped. The queue is empty, so playback has ended."
SKIP_DUPLICATE    = "ℹ️ Already moved on to the next track."
SKIP_FAILED       = "❌ Could not play the next track. Playback has ended."
STOP_DONE         = "⏹ Stopped. The queue was cleared and the assistant left the voice chat."
QUEUE_EMPTY       = "📭 The queue is empty."
QUEUE_HEADER      = "📜 **Up next** ({count}/{cap})"
NOW_PLAYING_LINE  = (
    "🎵 **Now Playing**\n\n"
    "**{title}**\n"
    "⏱ {duration}  •  👤 {uploader}\n"
    "Requested by: {requested_by}\n"
    "Up next: {upcoming}/{cap}"
)

# ── General ───────────────────────────────────────────────────────────────────
SEARCHING    = "🔍 Searching for **{query}**…"
LOADING_URL  = "⏳ Loading URL…"
