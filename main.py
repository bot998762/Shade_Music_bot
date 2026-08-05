"""
main.py
~~~~~~~
ShadeMusicBot — Application Entry Point

Start order
-----------
1. Parse & validate configuration (exits with a clear error if invalid).
2. Initialise structured logging.
3. Start the ApplicationLifecycle (DB → Bot).
4. Run the FastAPI health server and the Pyrogram bot concurrently.
5. Wait for SIGINT / SIGTERM, then shut down gracefully.

The FastAPI server and the Telegram bot run in the same asyncio event loop
via asyncio.gather, so no threads or sub-processes are needed.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.core.config import get_settings
from app.core.logger import logger, setup_logging
from app.core.startup import ApplicationLifecycle
from app.api.health import create_app, set_lifecycle


async def run() -> None:
    # ── 1. Configuration ──────────────────────────────────────────────────────
    # get_settings() validates all required env vars and exits with a
    # human-readable error on failure — no silent crashes.
    settings = get_settings()

    # ── 2. Logging ────────────────────────────────────────────────────────────
    setup_logging(settings.log_level)
    logger.info("ShadeMusicBot v1.0.0 — Phase 1 (Music Engine)")
    logger.info("Python {}", sys.version.split()[0])

    # ── 3. Lifecycle ──────────────────────────────────────────────────────────
    lifecycle = ApplicationLifecycle(settings)
    await lifecycle.start()

    # ── 4. Inject lifecycle into health module ────────────────────────────────
    set_lifecycle(lifecycle)

    # ── 5. Build FastAPI / uvicorn server ────────────────────────────────────
    fastapi_app = create_app()
    uvicorn_config = uvicorn.Config(
        app=fastapi_app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        # Disable uvicorn's default access log spam; loguru handles logging
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    # Prevent uvicorn from installing its own signal handlers
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    logger.info(
        "Health server starting on http://{}:{}",
        settings.app_host,
        settings.app_port,
    )

    # ── 6. Run concurrently ───────────────────────────────────────────────────
    try:
        await asyncio.gather(
            server.serve(),
            lifecycle.wait(),   # blocks until SIGINT/SIGTERM
        )
    finally:
        # ── 7. Graceful shutdown ──────────────────────────────────────────────
        server.should_exit = True
        await lifecycle.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
