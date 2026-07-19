"""
app.database.repositories.users
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the ``users`` collection.

Tracks every user who interacts with the bot. Phase 1+ will extend this
with preferences, history, ban status, and statistics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import motor.motor_asyncio as motor

from app.database.repositories.base import BaseRepository


class UserRepository(BaseRepository):
    """CRUD operations for Telegram user documents."""

    def __init__(self, db: motor.AsyncIOMotorDatabase) -> None:
        super().__init__(db, "users")

    # ── Domain methods ────────────────────────────────────────────────────────

    async def get_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a user document by Telegram user_id."""
        return await self.find_one({"user_id": user_id})

    async def register_or_update(
        self,
        user_id: int,
        first_name: str,
        username: Optional[str] = None,
        last_name: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> bool:
        """
        Create a new user document or refresh mutable fields.

        Returns True if the user is brand new, False if they already existed.
        """
        data = {
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "language_code": language_code,
            "last_seen": datetime.now(tz=timezone.utc),
            # Fields managed only on first insert (via $setOnInsert in base.upsert):
            # created_at, is_banned, is_sudo
        }
        is_new = await self.upsert({"user_id": user_id}, data)
        if is_new:
            # Set fields that must only be written once
            await self._collection.update_one(
                {"user_id": user_id},
                {
                    "$setOnInsert": {
                        "is_banned": False,
                        "is_sudo": False,
                        "songs_requested": 0,
                    }
                },
            )
        return is_new

    async def ban(self, user_id: int, reason: str = "") -> bool:
        """Ban a user. Returns True if the user existed."""
        modified = await self.update_one(
            {"user_id": user_id},
            {"$set": {"is_banned": True, "ban_reason": reason}},
        )
        return modified > 0

    async def unban(self, user_id: int) -> bool:
        """Unban a user. Returns True if the user existed."""
        modified = await self.update_one(
            {"user_id": user_id},
            {"$set": {"is_banned": False, "ban_reason": ""}},
        )
        return modified > 0

    async def is_banned(self, user_id: int) -> bool:
        """Return True if the user is currently banned."""
        doc = await self.find_one({"user_id": user_id, "is_banned": True})
        return doc is not None

    async def total_count(self) -> int:
        """Return the total number of registered users."""
        return await self.count()

    async def get_all_ids(self) -> List[int]:
        """Return a list of all user IDs (for broadcast, etc.)."""
        cursor = self._collection.find({}, {"user_id": 1, "_id": 0})
        docs = await cursor.to_list(length=None)
        return [d["user_id"] for d in docs]
