"""
app.main
~~~~~~~~
Application coordinator — wires the lifecycle into the event loop.

Start order
-----------
1. Parse and validate configuration (exits with a clear error if invalid).
2. Initialise structured logging.
3. Start the ApplicationLifecycle (DB → Bot → Voice → Playback → Handlers).
4. Inject lifecycle into the health module.
5. Run the FastAPI health server and Telegram bot concurrently.
6. Wait for SIGINT / SIGTERM.
7. Shut down gracefully.

The FastAPI server and the Telegram bot share the same asyncio event loop
via asyncio.gather — no threads or sub-processes required.
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.bootstrap.lifecycle import ApplicationLifecycle
from app.infrastructure.config import get_settings
from app.infrastructure.health import create_app, set_lifecycle
from app.infrastructure.logger import logger, setup_logging
from app.shared.constants import BOT_NAME, BOT_VERSION


async def run() -> None:
    # ── 1. Configuration ──────────────────────────────────────────────────────
    # get_settings() validates all required env vars and sys.exit(1) on failure.
    settings = get_settings()

    # ── 2. Logging ────────────────────────────────────────────────────────────
    setup_logging(settings.log_level)
    logger.info("{} v{} — starting up", BOT_NAME, BOT_VERSION)
    logger.info("Python {}", sys.version.split()[0])

    # ── 3. Lifecycle ──────────────────────────────────────────────────────────
    lifecycle = ApplicationLifecycle(settings)
    await lifecycle.start()

    # ── 4. Health module injection ────────────────────────────────────────────
    set_lifecycle(lifecycle)

    # ── 5. FastAPI / uvicorn health server ────────────────────────────────────
    fastapi_app = create_app()
    uvicorn_config = uvicorn.Config(
        app=fastapi_app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        access_log=False,   # loguru handles all logging
    )
    server = uvicorn.Server(uvicorn_config)
    # Prevent uvicorn from installing its own signal handlers —
    # ApplicationLifecycle owns signal handling.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    logger.info(
        "Health server starting on http://{}:{}",
        settings.app_host, settings.app_port,
    )

    # ── 6 & 7. Run concurrently, then shut down ───────────────────────────────
    try:
        await asyncio.gather(
            server.serve(),
            lifecycle.wait(),  # blocks until SIGINT / SIGTERM
        )
    finally:
        server.should_exit = True
        await lifecycle.stop()
