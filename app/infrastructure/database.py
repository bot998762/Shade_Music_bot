"""
app.infrastructure.database
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Motor (async MongoDB) connection lifecycle and domain repositories.

Design
------
DatabaseManager owns the connection.  Repositories receive the database
handle via the manager's properties — never imported as a global.

Repository pattern
------------------
BaseRepository provides generic CRUD.
Domain repositories (UserRepository, ChatRepository) add domain methods.
Future repositories (TrackHistoryRepository, PlaylistRepository) follow
the same pattern without touching DatabaseManager.

Keeping repositories here (rather than a separate repositories/ sub-package)
matches the architecture spec and prevents unnecessary nesting while still
keeping all database concerns in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import motor.motor_asyncio as motor
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

from app.infrastructure.logger import logger


# ══════════════════════════════════════════════════════════════════════════════
# Base Repository
# ══════════════════════════════════════════════════════════════════════════════

class BaseRepository:
    """
    Generic MongoDB collection wrapper.

    Parameters
    ----------
    db:
        AsyncIOMotorDatabase provided by DatabaseManager.
    collection_name:
        Name of the MongoDB collection this repository manages.
    """

    def __init__(
        self,
        db: motor.AsyncIOMotorDatabase,
        collection_name: str,
    ) -> None:
        self._collection: motor.AsyncIOMotorCollection = db[collection_name]

    # ── Reads ─────────────────────────────────────────────────────────────────

    async def find_one(self, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return the first document matching *query*, or None."""
        return await self._collection.find_one(query, {"_id": 0})

    async def find_many(
        self,
        query: Dict[str, Any],
        limit: int = 100,
        sort_by: Optional[str] = None,
        ascending: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return up to *limit* documents matching *query*."""
        direction = 1 if ascending else -1
        cursor = self._collection.find(query, {"_id": 0})
        if sort_by:
            cursor = cursor.sort(sort_by, direction)
        cursor = cursor.limit(limit)
        return await cursor.to_list(length=limit)

    async def count(self, query: Optional[Dict[str, Any]] = None) -> int:
        """Return the number of documents matching *query*."""
        return await self._collection.count_documents(query or {})

    async def exists(self, query: Dict[str, Any]) -> bool:
        """Return True if at least one document matches *query*."""
        doc = await self._collection.find_one(query, {"_id": 1})
        return doc is not None

    # ── Writes ────────────────────────────────────────────────────────────────

    async def insert_one(self, document: Dict[str, Any]) -> str:
        """Insert *document* and return the inserted ``_id`` as a string."""
        document.setdefault("created_at", _utcnow())
        document.setdefault("updated_at", _utcnow())
        result = await self._collection.insert_one(document)
        return str(result.inserted_id)

    async def update_one(
        self,
        query: Dict[str, Any],
        update: Dict[str, Any],
        upsert: bool = False,
    ) -> int:
        """Apply *update* to the first document matching *query*."""
        if "$set" in update:
            update["$set"]["updated_at"] = _utcnow()
        else:
            update.setdefault("$set", {})["updated_at"] = _utcnow()

        result = await self._collection.update_one(query, update, upsert=upsert)
        return result.modified_count

    async def delete_one(self, query: Dict[str, Any]) -> int:
        """Delete the first document matching *query*. Returns deleted count."""
        result = await self._collection.delete_one(query)
        return result.deleted_count

    async def delete_many(self, query: Dict[str, Any]) -> int:
        """Delete all documents matching *query*. Returns deleted count."""
        result = await self._collection.delete_many(query)
        return result.deleted_count

    async def upsert(self, query: Dict[str, Any], data: Dict[str, Any]) -> bool:
        """
        Insert or update a single document.

        Returns True if a new document was created, False if updated.
        """
        data_set = {k: v for k, v in data.items()}
        data_set["updated_at"] = _utcnow()

        result = await self._collection.update_one(
            query,
            {
                "$set": data_set,
                "$setOnInsert": {"created_at": _utcnow()},
            },
            upsert=True,
        )
        return result.upserted_id is not None


# ══════════════════════════════════════════════════════════════════════════════
# User Repository
# ══════════════════════════════════════════════════════════════════════════════

class UserRepository(BaseRepository):
    """CRUD operations for Telegram user documents."""

    def __init__(self, db: motor.AsyncIOMotorDatabase) -> None:
        super().__init__(db, "users")

    async def get_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
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
        Create or refresh a user document.

        Returns True if the user is brand new.
        """
        data = {
            "user_id":       user_id,
            "first_name":    first_name,
            "last_name":     last_name,
            "username":      username,
            "language_code": language_code,
            "last_seen":     _utcnow(),
        }
        is_new = await self.upsert({"user_id": user_id}, data)
        if is_new:
            await self._collection.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {
                    "is_banned":       False,
                    "is_sudo":         False,
                    "songs_requested": 0,
                }},
            )
        return is_new

    async def ban(self, user_id: int, reason: str = "") -> bool:
        modified = await self.update_one(
            {"user_id": user_id},
            {"$set": {"is_banned": True, "ban_reason": reason}},
        )
        return modified > 0

    async def unban(self, user_id: int) -> bool:
        modified = await self.update_one(
            {"user_id": user_id},
            {"$set": {"is_banned": False, "ban_reason": ""}},
        )
        return modified > 0

    async def is_banned(self, user_id: int) -> bool:
        doc = await self.find_one({"user_id": user_id, "is_banned": True})
        return doc is not None

    async def total_count(self) -> int:
        return await self.count()

    async def get_all_ids(self) -> List[int]:
        cursor = self._collection.find({}, {"user_id": 1, "_id": 0})
        docs = await cursor.to_list(length=None)
        return [d["user_id"] for d in docs]


# ══════════════════════════════════════════════════════════════════════════════
# Chat Repository
# ══════════════════════════════════════════════════════════════════════════════

class ChatRepository(BaseRepository):
    """CRUD operations for Telegram chat (group/channel) documents."""

    def __init__(self, db: motor.AsyncIOMotorDatabase) -> None:
        super().__init__(db, "chats")

    async def get_by_id(self, chat_id: int) -> Optional[Dict[str, Any]]:
        return await self.find_one({"chat_id": chat_id})

    async def register_or_update(
        self,
        chat_id: int,
        title: str,
        chat_type: str,
        username: Optional[str] = None,
    ) -> bool:
        """
        Create or refresh a chat document.

        Returns True if the chat is brand new.
        """
        data = {
            "chat_id":  chat_id,
            "title":    title,
            "type":     chat_type,
            "username": username,
        }
        is_new = await self.upsert({"chat_id": chat_id}, data)
        if is_new:
            await self._collection.update_one(
                {"chat_id": chat_id},
                {"$setOnInsert": {
                    "is_active": True,
                    "is_banned": False,
                    "volume":    100,
                    "language":  "en",
                    # Phase 1+: voice_chat_active, current_track, queue
                    # Phase 2+: playlist_id, loop_mode
                }},
            )
        return is_new

    async def set_active(self, chat_id: int, active: bool) -> bool:
        modified = await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_active": active}},
        )
        return modified > 0

    async def ban(self, chat_id: int, reason: str = "") -> bool:
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
        await self.update_one(
            {"chat_id": chat_id},
            {"$set": {"volume": max(0, min(200, volume))}},
            upsert=True,
        )

    async def get_volume(self, chat_id: int, default: int = 100) -> int:
        doc = await self.find_one({"chat_id": chat_id})
        return int(doc.get("volume", default)) if doc else default

    async def total_count(self) -> int:
        return await self.count()

    async def get_active_ids(self) -> List[int]:
        cursor = self._collection.find(
            {"is_active": True, "is_banned": False},
            {"chat_id": 1, "_id": 0},
        )
        docs = await cursor.to_list(length=None)
        return [d["chat_id"] for d in docs]


# ══════════════════════════════════════════════════════════════════════════════
# Database Manager
# ══════════════════════════════════════════════════════════════════════════════

class DatabaseManager:
    """
    Wraps a Motor AsyncIOMotorClient and exposes typed repositories.

    Attributes
    ----------
    client:
        Raw Motor client. Exposed for health checks.
    db:
        Selected database handle.
    users:
        UserRepository instance.
    chats:
        ChatRepository instance.
    """

    def __init__(self, mongo_uri: str, db_name: str) -> None:
        self._uri:    str = mongo_uri
        self._name:   str = db_name
        self.client:  Optional[motor.AsyncIOMotorClient]   = None
        self.db:      Optional[motor.AsyncIOMotorDatabase] = None
        self._users:  Optional[UserRepository]             = None
        self._chats:  Optional[ChatRepository]             = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the Motor connection pool."""
        self.client = motor.AsyncIOMotorClient(
            self._uri,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=10_000,
            socketTimeoutMS=30_000,
            maxPoolSize=10,
            minPoolSize=1,
            retryWrites=True,
            retryReads=True,
        )
        self.db     = self.client[self._name]
        self._users = UserRepository(self.db)
        self._chats = ChatRepository(self.db)
        logger.debug("Motor client created for database='{}'", self._name)

    async def disconnect(self) -> None:
        """Close the Motor connection pool gracefully."""
        if self.client is not None:
            self.client.close()
            self.client = None
            self.db     = None
            self._users = None
            self._chats = None

    async def ping(self) -> None:
        """
        Verify the server is reachable.

        Raises RuntimeError on failure so startup aborts with a clear message.
        """
        if self.db is None:
            raise RuntimeError("DatabaseManager.connect() must be called first.")
        try:
            await self.db.command("ping")
        except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
            raise RuntimeError(
                f"MongoDB ping failed — check MONGO_URI and network access: {exc}"
            ) from exc

    async def ensure_indexes(self) -> None:
        """
        Create all collection indexes.

        Idempotent — safe to call on every restart.
        Future repositories add their indexes here for easy discovery.
        """
        if self.db is None:
            raise RuntimeError("Database not connected.")

        await self.db.users.create_index("user_id", unique=True)
        await self.db.users.create_index("username")
        await self.db.chats.create_index("chat_id", unique=True)
        # Future phases add indexes here (track_history, playlists, etc.)
        logger.debug("Database indexes verified ✓")

    # ── Repository accessors ──────────────────────────────────────────────────

    @property
    def users(self) -> UserRepository:
        if self._users is None:
            raise RuntimeError("Database not connected — call connect() first.")
        return self._users

    @property
    def chats(self) -> ChatRepository:
        if self._chats is None:
            raise RuntimeError("Database not connected — call connect() first.")
        return self._chats

    # ── Health helper ─────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self.client is not None


# ── Private helpers ────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
