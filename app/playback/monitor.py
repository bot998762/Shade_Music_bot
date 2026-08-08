"""
app.playback.monitor
~~~~~~~~~~~~~~~~~~~~
StreamMonitor — passive observer of stream-end events.

Responsibility
--------------
Receive a stream-end event from VoiceChatManager.
Log the event.
Call PlaybackController.advance(chat_id).
Return.

Nothing else.

What this module does NOT do
-----------------------------
* Does not dequeue tracks.
* Does not resolve stream URLs.
* Does not build MediaStream objects.
* Does not retry playback.
* Does not update playback state.
* Does not trigger cleanup.
* Does not make any playback decision of any kind.

Authority
---------
PlaybackController is the sole playback authority.
StreamMonitor is the event relay between VoiceChatManager and
PlaybackController. It carries the event — it never acts on it.

Stage log: [MONITOR]
"""

from __future__ import annotations

from app.infrastructure.logger import logger


class StreamMonitor:
    """
    Passive observer wired between VoiceChatManager and PlaybackController.

    VoiceChatManager fires on_stream_end(chat_id) when a track ends.
    StreamMonitor logs the event and delegates to controller.advance().
    All decisions about what happens next are made by PlaybackController.

    Parameters
    ----------
    controller:
        The PlaybackController instance. StreamMonitor holds a reference
        only to call advance() — it reads no controller state.
    """

    def __init__(self, controller) -> None:
        # Type annotation uses string to avoid importing controller here,
        # though the import would not create a cycle. String annotation keeps
        # this module's import footprint minimal and its role unambiguous.
        self._controller = controller

    async def on_stream_end(self, chat_id: int) -> None:
        """
        Receive a stream-end event and delegate to PlaybackController.

        Called by VoiceChatManager._fire_stream_end() on:
          - StreamAudioEnded  (track finished naturally)
          - CLOSED_VOICE_CHAT (admin ended the VC)
          - KICKED            (bot removed from group)

        Stage log: [MONITOR]
        """
        logger.info("[MONITOR] Stream ended — delegating to controller  chat_id={}", chat_id)
        await self._controller.advance(chat_id)
