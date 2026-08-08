"""
main.py — ShadeMusicBot Entry Point
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Thin entry point that delegates to app.main.run().

Run with:
    python main.py

Or via Docker:
    CMD ["python", "-u", "main.py"]
"""

from __future__ import annotations

import asyncio

from app.main import run

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
