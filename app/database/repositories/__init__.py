"""Domain repositories built on BaseRepository."""
from app.database.repositories.users import UserRepository
from app.database.repositories.chats import ChatRepository

__all__ = ["UserRepository", "ChatRepository"]
