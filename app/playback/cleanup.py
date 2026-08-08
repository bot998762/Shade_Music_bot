"""
app.playback.cleanup
~~~~~~~~~~~~~~~~~~~~
CleanupService — voice chat teardown and resource release.

Responsibility
--------------
Leave the voice chat.
Clear the session queue.
Transition state back to IDLE.
Release all per-chat resources.

When cleanup runs
-----------------
After every playback outcome:
  ✓ Success         — queue exhausted naturally
  ✗ Failure         — stream resolution failed
  ✗ Cancellation    — future skip/stop implementation
  ✗ Timeout         — stream monitoring timeout
  ✗ Unexpected exc  — unhandled exception in controller or monitor

Cleanup NEVER silently swallows the failure mode — it logs the reason
and still executes all teardown steps regardless of which step raised.

Stage log: [CLEANUP]
"""

from __future__ import annotations

from app.infrastructure.logger import logger
from app.playback.session import SessionManager
from app.playback.state import StateManager
from app.streaming.voice import VoiceChatManager


class CleanupService:
    """
    Tears down a chat's playback session cleanly.

    Injected into PlaybackController and StreamMonitor so both can trigger
    cleanup without duplicating the teardown logic.
    """

    def __init__(
        self,
        voice:   VoiceChatManager,
        session: SessionManager,
        state:   StateManager,
    ) -> None:
        self._voice   = voice
        self._session = session
        self._state   = state

    async def cleanup(self, chat_id: int, reason: str = "normal") -> None:
        """
        Full teardown for *chat_id*.

        Executes all steps even if an earlier step raises — cleanup must
        always complete or the bot enters an inconsistent state.

        Steps
        -----
        1. Leave voice chat  (releases WebRTC connection + ntgcalls resources)
        2. Clear session     (discards queued tracks + temporary objects)
        3. Transition state  (marks chat as IDLE)

        Parameters
        ----------
        chat_id:
            The group chat to clean up.
        reason:
            Human-readable reason string for the log (success | failure |
            cancelled | timeout | error).
        """
        logger.info("[CLEANUP] Starting  chat_id={}  reason={}", chat_id, reason)
        errors = []

        # ── Step 1: Leave voice chat ───────────────────────────────────────
        try:
            await self._voice.leave(chat_id)
            logger.debug("[CLEANUP] Voice chat released  chat_id={}", chat_id)
        except Exception as exc:
            errors.append(f"voice.leave: {exc}")
            logger.error(
                "[CLEANUP] voice.leave raised  chat_id={}  error={}", chat_id, exc,
            )

        # ── Step 2: Clear session (queue + in-flight track) ────────────────
        try:
            await self._session.clear(chat_id)
            logger.debug("[CLEANUP] Session cleared  chat_id={}", chat_id)
        except Exception as exc:
            errors.append(f"session.clear: {exc}")
            logger.error(
                "[CLEANUP] session.clear raised  chat_id={}  error={}", chat_id, exc,
            )

        # ── Step 3: Transition state to IDLE ──────────────────────────────
        try:
            self._state.transition_to_idle(chat_id)
        except Exception as exc:
            errors.append(f"state.transition_to_idle: {exc}")
            logger.error(
                "[CLEANUP] state.transition raised  chat_id={}  error={}",
                chat_id, exc,
            )

        if errors:
            logger.warning(
                "[CLEANUP] Finished with {} error(s)  chat_id={}  errors={}",
                len(errors), chat_id, errors,
            )
        else:
            logger.info("[CLEANUP] Done  chat_id={}", chat_id)
