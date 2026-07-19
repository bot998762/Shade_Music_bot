"""
app.core.logger
~~~~~~~~~~~~~~~
Production logging using loguru.

Two sinks are registered:
  • stdout  — coloured, human-readable output (visible in Render dashboard)
  • file    — rotating log files under ./logs/ (useful for local debugging)

Import `logger` from this module wherever structured logging is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# Re-export so other modules do: from app.core.logger import logger
__all__ = ["logger", "setup_logging"]

_LOG_DIR = Path("logs")
_LOG_FORMAT_CONSOLE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
    "<level>{message}</level>"
)
_LOG_FORMAT_FILE = (
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
        Passed directly to loguru's ``level`` parameter.
    """
    # Remove the default loguru handler
    logger.remove()

    # ── Console sink ──────────────────────────────────────────────────────────
    logger.add(
        sys.stdout,
        level=level,
        format=_LOG_FORMAT_CONSOLE,
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
        format=_LOG_FORMAT_FILE,
        rotation="00:00",      # new file every midnight
        retention="7 days",    # keep last 7 days
        compression="gz",      # compress old files
        backtrace=True,
        diagnose=False,
        enqueue=True,          # async-safe writes for file sink
        encoding="utf-8",
    )

    logger.info("Logging initialised at level={}", level)
