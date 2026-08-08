"""
app.infrastructure.logger
~~~~~~~~~~~~~~~~~~~~~~~~~~
Production logging via loguru.

Two sinks:
  • stdout  — coloured, human-readable (visible in Render dashboard)
  • file    — rotating log files under ./logs/ (useful for local debugging)

Import ``logger`` from this module everywhere.
Never import loguru directly from other modules — all log config lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

__all__ = ["logger", "setup_logging"]

_LOG_DIR = Path("logs")

_FMT_CONSOLE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
    "<level>{message}</level>"
)
_FMT_FILE = (
    "{time:YYYY-MM-DD HH:mm:ss} | "
    "{level: <8} | "
    "{name}:{function}:{line} — "
    "{message}"
)


def setup_logging(level: str = "INFO") -> None:
    """
    Configure loguru for production use.

    Parameters
    ----------
    level:
        One of DEBUG | INFO | WARNING | ERROR | CRITICAL.
    """
    logger.remove()

    # ── Console sink ──────────────────────────────────────────────────────────
    logger.add(
        sys.stdout,
        level=level,
        format=_FMT_CONSOLE,
        colorize=True,
        backtrace=True,
        diagnose=False,   # disable variable values in tracebacks (security)
        enqueue=False,    # synchronous — avoids ordering issues in async code
    )

    # ── File sink (rotating) ──────────────────────────────────────────────────
    _LOG_DIR.mkdir(exist_ok=True)
    logger.add(
        _LOG_DIR / "shadebot_{time:YYYY-MM-DD}.log",
        level=level,
        format=_FMT_FILE,
        rotation="00:00",     # new file every midnight
        retention="7 days",
        compression="gz",
        backtrace=True,
        diagnose=False,
        enqueue=True,         # async-safe writes for file sink
        encoding="utf-8",
    )

    logger.info("Logging initialised at level={}", level)
