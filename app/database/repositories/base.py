"""
app.database.repositories.base
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Generic base repository providing common CRUD operations.

All domain repositories inherit from BaseRepository and call
``super().__init__(db, "collection_name")`` so every collection gets
find/insert/update/delete for free.

The base layer purposefully returns plain dicts rather than ORM objects
so each phase can decide its own serialisation strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import motor.motor_asyncio as motor


class BaseRepository:
    """
    Generic MongoDB collection wrapper.

    Parameters
    ----------
    db:
        An AsyncIOMotorDatabase instance provided by DatabaseManager.
    collection_name:
        The name of the MongoDB collection this repository manages.
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
        """Return the number of documents matching *query* (or total count)."""
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
        """
        Apply *update* to the first document matching *query*.

        Returns the number of documents modified (0 or 1).
        Automatically sets ``updated_at`` in the ``$set`` operator.
        """
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

    # ── Convenience ───────────────────────────────────────────────────────────

    async def upsert(
        self,
        query: Dict[str, Any],
        data: Dict[str, Any],
    ) -> bool:
        """
        Insert or update a single document.

        Returns True if a new document was created, False if an existing
        document was updated.
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


def _utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)
