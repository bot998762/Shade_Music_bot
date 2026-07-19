"""
app.database.connection
~~~~~~~~~~~~~~~~~~~~~~~
Manages the Motor (async MongoDB) connection lifecycle.

A single DatabaseManager instance is created at startup and shared across
the application via dependency injection. Repositories receive the database
object rather than importing a global, keeping them easily testable.
"""

from __future__ import annotations

from typing import Optional

import motor.motor_asyncio as motor
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.core.logger import logger


class DatabaseManager:
    """
    Wraps a Motor AsyncIOMotorClient.

    Attributes
    ----------
    client:
        The raw Motor client. Exposed for advanced usage; prefer using
        ``db`` for everyday collection access.
    db:
        The selected database handle. Passed to repositories.
    """

    def __init__(self, settings: object) -> None:
        # Avoid a hard import of Settings here to keep the DB layer decoupled
        self._uri: str = settings.mongo_uri  # type: ignore[attr-defined]
        self._db_name: str = settings.mongo_db_name  # type: ignore[attr-defined]
        self.client: Optional[motor.AsyncIOMotorClient] = None
        self.db: Optional[motor.AsyncIOMotorDatabase] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the Motor connection pool."""
        self.client = motor.AsyncIOMotorClient(
            self._uri,
            serverSelectionTimeoutMS=10_000,  # 10 s — fail fast on bad URI
            connectTimeoutMS=10_000,
            socketTimeoutMS=30_000,
            maxPoolSize=10,
            minPoolSize=1,
            retryWrites=True,
            retryReads=True,
        )
        self.db = self.client[self._db_name]
        logger.debug("Motor client created for database='{}'", self._db_name)

    async def disconnect(self) -> None:
        """Close the Motor connection pool gracefully."""
        if self.client:
            self.client.close()
            self.client = None
            self.db = None

    async def ping(self) -> None:
        """
        Verify that the server is reachable.

        Raises
        ------
        RuntimeError
            If the database is not connected or the ping fails.
        """
        if self.db is None:
            raise RuntimeError("DatabaseManager.connect() must be called first.")
        try:
            await self.db.command("ping")
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            raise RuntimeError(
                f"MongoDB ping failed — check MONGO_URI and network access: {exc}"
            ) from exc

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def ensure_indexes(self) -> None:
        """
        Create all collection indexes.

        Called once at startup. Idempotent — safe to call on every restart.
        Each repository declares its own indexes here for easy discovery.
        """
        if self.db is None:
            raise RuntimeError("Database not connected.")

        # Users
        await self.db.users.create_index("user_id", unique=True)
        await self.db.users.create_index("username")

        # Chats
        await self.db.chats.create_index("chat_id", unique=True)

        # (Future phases will add more indexes here)
        logger.debug("Database indexes verified ✓")
