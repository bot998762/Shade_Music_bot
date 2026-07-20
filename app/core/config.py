"""
app.core.config
~~~~~~~~~~~~~~~
Centralised settings loaded from environment variables.

All required fields raise a clear startup error if missing.
Optional fields carry sensible defaults so the bot can run
with minimal configuration on Render's free tier.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings resolved from environment variables.

    Pydantic will raise a descriptive ValidationError at import time if
    any required variable is absent, preventing a partially-started bot.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",           # ignore unknown env vars (e.g. Render injects PORT)
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    api_id: int = Field(..., description="Telegram API ID from my.telegram.org")
    api_hash: str = Field(..., description="Telegram API Hash from my.telegram.org")
    bot_token: str = Field(..., description="Bot token from @BotFather")

    # ── Authorisation ─────────────────────────────────────────────────────────
    owner_id: int = Field(..., description="Telegram user ID of the bot owner")
    sudo_users: List[int] = Field(
        default_factory=list,
        description="Additional admin user IDs (comma-separated string accepted)",
    )

    # ── MongoDB ───────────────────────────────────────────────────────────────
    mongo_uri: str = Field(..., description="MongoDB Atlas connection URI")
    mongo_db_name: str = Field(default="shademusicbot", description="MongoDB database name")

    # ── Web Server ────────────────────────────────────────────────────────────
    app_host: str = Field(default="0.0.0.0", description="Health server bind host")
    # Render injects $PORT; fall back to APP_PORT, then 8080
    app_port: int = Field(default=8080, description="Health server bind port")

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = Field(default="INFO", description="Logging level")

    # ── Bot Behaviour ─────────────────────────────────────────────────────────
    max_queue_size: int = Field(default=50, description="Max queued songs per chat")
    default_volume: int = Field(default=100, description="Default playback volume (0-200)")

    # ── Phase 1: Voice Chat ────────────────────────────────────────────────────
    # Optional Pyrogram string session for a user assistant account.
    # When set, the assistant joins voice chats (recommended for production).
    # When absent, the bot itself joins (requires voice-chat admin permission).
    assistant_session: Optional[str] = Field(
        default=None,
        description="Pyrogram string session for the VC assistant (optional)",
    )

    # Path to a Netscape-format cookies.txt file for yt-dlp.
    # The bot works without it; cookies unlock age-restricted / region-locked videos.
    cookies_path: str = Field(
        default="cookies.txt",
        description="Path to yt-dlp cookies.txt (optional; ignored if file absent)",
    )

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def all_admins(self) -> List[int]:
        """Merged list of owner + sudo users for permission checks."""
        return list({self.owner_id, *self.sudo_users})

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("log_level", mode="before")
    @classmethod
    def normalise_log_level(cls, v: str) -> str:
        level = v.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {allowed}, got '{v}'")
        return level

    @field_validator("sudo_users", mode="before")
    @classmethod
    def parse_sudo_users(cls, v: object) -> List[int]:
        """Accept a comma-separated string or a list."""
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, list):
            return [int(uid) for uid in v if uid]
        return []

    @field_validator("default_volume", mode="before")
    @classmethod
    def clamp_volume(cls, v: int) -> int:
        if not (0 <= int(v) <= 200):
            raise ValueError("DEFAULT_VOLUME must be between 0 and 200")
        return int(v)

    @model_validator(mode="after")
    def resolve_render_port(self) -> "Settings":
        """Render injects PORT at runtime; prefer it over APP_PORT."""
        render_port = os.environ.get("PORT")
        if render_port:
            object.__setattr__(self, "app_port", int(render_port))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return a cached Settings instance.

    Calling this at module level gives a clear error on startup if any
    required variable is missing, rather than a cryptic AttributeError later.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:
        # Print a human-readable message before Python's traceback
        print(
            "\n[FATAL] Configuration error — check your environment variables.\n"
            f"        {exc}\n",
            file=sys.stderr,
        )
        sys.exit(1)
