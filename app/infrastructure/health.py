"""
app.infrastructure.health
~~~~~~~~~~~~~~~~~~~~~~~~~~
Minimal FastAPI application exposing health and readiness endpoints.

Render requires a web service to respond on its assigned PORT.
The health check URL is configured in render.yaml to point at /health.

Endpoints
---------
GET /           → 200  Simple alive check
GET /health     → 200 | 503  Detailed health (DB connectivity, bot status)
GET /metrics    → 200  Basic runtime counters (future: Prometheus export)

Lifecycle injection
-------------------
``set_lifecycle(lifecycle)`` is called by bootstrap after all subsystems
are initialised, so the health route can inspect bot/db state without
creating a circular import.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.shared.constants import BOT_NAME, BOT_VERSION

# Module-level reference — set by bootstrap after lifecycle is ready.
_lifecycle: Any = None
_start_time: float = time.monotonic()


def set_lifecycle(lifecycle: Any) -> None:
    """Inject the ApplicationLifecycle instance into the health module."""
    global _lifecycle
    _lifecycle = lifecycle


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title=BOT_NAME,
        description=f"{BOT_NAME} — health API",
        version=BOT_VERSION,
        docs_url=None,   # disable Swagger UI in production
        redoc_url=None,
    )

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": BOT_NAME})

    @app.get("/health")
    async def health() -> JSONResponse:
        """
        Detailed health check used by Render's health-check probe.

        Returns HTTP 200 when everything is operational.
        Returns HTTP 503 when a critical sub-system is degraded.
        """
        uptime = int(time.monotonic() - _start_time)
        db_ok  = _check_db()
        bot_ok = _check_bot()

        payload: Dict[str, Any] = {
            "status":         "healthy" if (db_ok and bot_ok) else "degraded",
            "version":        BOT_VERSION,
            "uptime_seconds": uptime,
            "subsystems": {
                "database": "up" if db_ok else "down",
                "bot":      "up" if bot_ok else "down",
            },
        }
        status_code = 200 if (db_ok and bot_ok) else 503
        return JSONResponse(payload, status_code=status_code)

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        """Lightweight runtime counters — placeholder for future Prometheus export."""
        uptime = int(time.monotonic() - _start_time)
        return JSONResponse({
            "uptime_seconds": uptime,
            # Future phases populate:
            # "songs_played_total": ...,
            # "active_voice_chats": ...,
            # "queued_tracks_total": ...,
        })

    return app


# ── Private health checkers ────────────────────────────────────────────────────

def _check_db() -> bool:
    """Return True if the database manager is connected."""
    if _lifecycle is None:
        return False
    db = getattr(_lifecycle, "db", None)
    return db is not None and getattr(db, "is_connected", False)


def _check_bot() -> bool:
    """Return True if the Pyrogram client is connected."""
    if _lifecycle is None:
        return False
    bot_client = getattr(_lifecycle, "bot_client", None)
    if bot_client is None:
        return False
    # bot_client is a Pyrogram Client directly (not a wrapper).
    # Client.is_connected is a bool property indicating the MTProto connection.
    return getattr(bot_client, "is_connected", False)
