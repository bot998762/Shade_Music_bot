"""
app.api.health
~~~~~~~~~~~~~~
Minimal FastAPI application exposing health and readiness endpoints.

Render requires a web service to respond on its assigned PORT.
The health check URL is configured in render.yaml to point at /health.

Endpoints
---------
GET /           → 200  Simple alive check
GET /health     → 200  Detailed health (DB connectivity, bot status)
GET /metrics    → 200  Basic runtime counters (future: Prometheus)
"""

from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Module-level reference to the lifecycle object.
# Set by main.py after the lifecycle is initialised so the health route
# can inspect bot/db state without a circular import.
_lifecycle: Any = None
_start_time: float = time.monotonic()


def set_lifecycle(lifecycle: Any) -> None:
    """Inject the ApplicationLifecycle instance into the health module."""
    global _lifecycle
    _lifecycle = lifecycle


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(
        title="ShadeMusicBot",
        description="Telegram Voice Chat Music Bot — health API",
        version="0.1.0",
        docs_url=None,   # disable Swagger UI in production
        redoc_url=None,
    )

    # ── Routes ────────────────────────────────────────────────────────────────

    @app.get("/", include_in_schema=False)
    async def root() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "ShadeMusicBot"})

    @app.get("/health")
    async def health() -> JSONResponse:
        """
        Detailed health check used by Render's health-check probe.

        Returns HTTP 200 when everything is operational.
        Returns HTTP 503 when a critical sub-system is degraded.
        """
        uptime = int(time.monotonic() - _start_time)
        db_ok = _check_db()
        bot_ok = _check_bot()

        payload: Dict[str, Any] = {
            "status": "healthy" if (db_ok and bot_ok) else "degraded",
            "uptime_seconds": uptime,
            "subsystems": {
                "database": "up" if db_ok else "down",
                "bot": "up" if bot_ok else "down",
            },
        }
        status_code = 200 if (db_ok and bot_ok) else 503
        return JSONResponse(payload, status_code=status_code)

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        """Lightweight runtime counters — placeholder for Phase N."""
        uptime = int(time.monotonic() - _start_time)
        return JSONResponse(
            {
                "uptime_seconds": uptime,
                # Future phases will populate:
                # "songs_played_total": ...,
                # "active_voice_chats": ...,
                # "queued_tracks_total": ...,
            }
        )

    return app


# ── Private helpers ────────────────────────────────────────────────────────────

def _check_db() -> bool:
    """Return True if the database manager is connected."""
    if _lifecycle is None:
        return False
    db = getattr(_lifecycle, "db", None)
    return db is not None and db.client is not None


def _check_bot() -> bool:
    """Return True if the Pyrogram client is connected."""
    if _lifecycle is None:
        return False
    bot = getattr(_lifecycle, "bot", None)
    if bot is None:
        return False
    client = getattr(bot, "_client", None)
    return client is not None and getattr(client, "is_connected", False)
