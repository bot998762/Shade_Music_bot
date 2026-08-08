"""
app.infrastructure.cache
~~~~~~~~~~~~~~~~~~~~~~~~~
Simple in-memory cache with a Redis-compatible interface.

Phase 0: dict-backed, single-process.
Future phase: swap this module's implementation for aioredis/redis-py
without changing any import or call site in the rest of the codebase.

Design
------
* set(key, value, ttl=None) — store a value, optionally with a TTL in seconds.
* get(key) → Optional[Any]  — retrieve a value, returning None if expired or absent.
* delete(key)               — remove a key.
* clear()                   — flush all keys (used on shutdown or test reset).

TTL is enforced lazily on read — no background expiry task needed at this scale.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple


class CacheManager:
    """
    Thread-safe (GIL-protected) in-memory key-value store.

    Safe for use from a single asyncio event loop.
    Not safe for multi-process deployments — use Redis in that case.
    """

    def __init__(self) -> None:
        # Stores (value, expiry_monotonic | None)
        self._store: Dict[str, Tuple[Any, Optional[float]]] = {}

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """
        Store *value* under *key*.

        Parameters
        ----------
        key:
            Cache key string.
        value:
            Any picklable value.
        ttl:
            Time-to-live in seconds.  None means the entry never expires.
        """
        expiry = (time.monotonic() + ttl) if ttl is not None else None
        self._store[key] = (value, expiry)

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieve the value for *key*, or None if absent or expired.
        Expired entries are evicted on read.
        """
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if expiry is not None and time.monotonic() > expiry:
            del self._store[key]
            return None
        return value

    def delete(self, key: str) -> None:
        """Remove *key* from the cache (no-op if absent)."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Flush all entries."""
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)
