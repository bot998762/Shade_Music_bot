"""
app.streaming.media
~~~~~~~~~~~~~~~~~~~
Audio quality configuration and MediaStream type aliases.

Centralises all pytgcalls MediaStream and AudioQuality constants so
the rest of the codebase never imports directly from pytgcalls.
When pytgcalls releases a new API (v3.x), only this file changes.

Constants
---------
AUDIO_QUALITY   — quality level passed to MediaStream (HIGH by default)
IGNORE_VIDEO    — MediaStream flag to discard the video track
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream

# Re-export so callers can: from app.streaming.media import AUDIO_QUALITY
__all__ = ["AUDIO_QUALITY", "IGNORE_VIDEO", "AudioQuality", "MediaStream"]

# ── Quality ───────────────────────────────────────────────────────────────────
AUDIO_QUALITY = AudioQuality.HIGH

# ── Flags ─────────────────────────────────────────────────────────────────────
# Discards the video track — we only stream audio.
IGNORE_VIDEO = MediaStream.Flags.IGNORE
