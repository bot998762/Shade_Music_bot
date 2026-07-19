"""
app.database.repositories.chats
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the ``chats`` collection.

Stores per-group settings, the active voice-chat state, and configuration
that Phase 1 (streaming) and Phase 2 (playlists) will populate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import motor.motor_asyncio as motor

from app.database.repositories.base import BaseRepository


class ChatRepository(BaseRepository):
    """CRUD operations for Telegram chat (group/channel) documents."""

    def __init__(self, db: motor.AsyncIOMotorDatabase) -> None:
        super().__init__(db, "chats")

    # ── Domain methods ────────────────────────────────────────────────────────

    async def get_by_id(self, chat_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a chat document by Telegram chat_id."""
        return await self.find_one({"chat_id": chat_id})

    async def register_or_update(
        self,
        chat_id: int,
        title: str,
        chat_type: str,
        username: Optional[str] = None,
    ) -> bool:
        """
        Create a new chat document or refresh mutable fields.

        Returns True if the chat is brand new.
        """
        data = {
            "chat_id": chat_id,
            "title": title,
            "type": chat_type,
            "username": username,
        }
        is_new = await self.upsert({"chat_id": chat_id}, data)
        if is_new:
            # Initialise defaults only on first registration
            await self._collection.update_one(
                {"chat_id": chat_id},
                {
                    "$setOnInsert": {
                        "is_active": True,
                        "is_banned": False,
                        "volume": 100,
                        "language": "en",
                        # Phase 1: voice_chat_active, current_track, queue
                        # Phase 2: playlist_id, loop_mode
                    }
                },
            )
        return is_new

    async def set_active(self, chat_id: int, active: bool) -> bool:
        """Enable or disable the bot in a chat."""
        modified = await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_active": active}},
        )
        return modified > 0

    async def ban(self, chat_id: int, reason: str = "") -> bool:
        """Ban a chat from using the bot."""
        modified = await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_banned": True, "ban_reason": reason}},
        )
        return modified > 0

    async def unban(self, chat_id: int) -> bool:
        modified = await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_banned": False, "ban_reason": ""}},
        )
        return modified > 0

    async def is_banned(self, chat_id: int) -> bool:
        doc = await self.find_one({"chat_id": chat_id, "is_banned": True})
        return doc is not None

    async def set_volume(self, chat_id: int, volume: int) -> None:
        """Persist the per-chat volume preference (0-200)."""
        await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"volume": max(0, min(200, volume))}},
            upsert=True,
        )

    async def get_volume(self, chat_id: int, default: int = 100) -> int:
        """Return the stored volume for a chat, falling back to *default*."""
        doc = await self.find_one({"chat_id": chat_id})
        return int(doc.get("volume", default)) if doc else default

    async def total_count(self) -> int:
        """Return the total number of registered chats."""
        return await self.count()

    async def get_active_ids(self) -> List[int]:
        """Return chat IDs where the bot is active and not banned."""
        cursor = self._collection.find(
            {"is_active": True, "is_banned": False},
            {"chat_id": 1, "_id": 0},
        )
        docs = await cursor.to_list(length=None)
        return [d["chat_id"] for d in docs]
