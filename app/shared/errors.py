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

PLAY_UNEXPECTED_ERROR = (
    "❌ An unexpected error occurred. Please try again."
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

# ── General ───────────────────────────────────────────────────────────────────
SEARCHING = "🔍 Searching for **{query}**…"
