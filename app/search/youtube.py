"""
app.search.youtube
~~~~~~~~~~~~~~~~~~
YouTube search via yt-dlp — metadata only (no stream URL resolution).

Responsibility
--------------
Search for a query string.
Return a SearchResult with permanent webpage_url + metadata.
Nothing more.

Stream URL resolution lives in search.resolver (separate responsibility).

Why extract_flat?
-----------------
extract_flat="in_playlist" fetches only metadata — no format resolution,
no audio CDN requests. It is fast (~1-2 s) and does not require cookies
for most public videos. The CDN URL is resolved separately by StreamResolver
just before playback starts.

Why a thread executor?
----------------------
yt-dlp is a synchronous library. Running it on the event loop would block
all Telegram updates. The executor keeps the event loop free.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import yt_dlp
import yt_dlp.utils

from app.infrastructure.logger import logger
from app.search.models import SearchResult
from app.shared.constants import (
    COOKIES_SECRETS_DIR,
    COOKIES_TMP_DIR,
    SEARCH_TIMEOUT_SEC,
    YT_EXECUTOR_NAME,
    YT_EXECUTOR_WORKERS,
)

# ── Thread executor (module-level — one executor for all searches) ─────────────
_EXECUTOR = ThreadPoolExecutor(
    max_workers=YT_EXECUTOR_WORKERS,
    thread_name_prefix=YT_EXECUTOR_NAME,
)

# ── yt-dlp options for metadata-only search ───────────────────────────────────
_SEARCH_OPTS: Dict = {
    "quiet":          True,
    "no_warnings":    True,
    "noplaylist":     True,
    "extract_flat":   "in_playlist",   # metadata only — zero CDN requests
    "default_search": "ytsearch",
    "skip_download":  True,
    "geo_bypass":     True,
    "socket_timeout": 20,
    "retries":        2,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}


class YouTubeSearch:
    """
    Searches YouTube and returns the first matching SearchResult.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape cookies.txt file.
        Enables access to age-restricted / region-locked content.
        Copied to /tmp at init to allow yt-dlp to write its .lock file
        (Render Secret Files are read-only).
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        self._cookies_path: Optional[str] = _resolve_cookies(cookies_path)

    # ── Public API ────────────────────────────────────────────────────────────

    async def search(self, query: str) -> Optional[SearchResult]:
        """
        Search YouTube and return the first result.

        Returns None when the query produces no results or on timeout.
        Callers should raise NoResultsError on None.

        Stage log: [SEARCH]
        """
        logger.info("[SEARCH] query='{}'", query)
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(_EXECUTOR, self._sync_search, query),
                timeout=SEARCH_TIMEOUT_SEC,
            )
            if result:
                logger.info(
                    "[SEARCH] OK  title='{}'  url='{}'",
                    result.title, result.webpage_url,
                )
            else:
                logger.warning("[SEARCH] No results for '{}'", query)
            return result
        except asyncio.TimeoutError:
            logger.error("[SEARCH] Timed out for '{}'", query)
            return None

    @staticmethod
    def shutdown() -> None:
        """Drain the thread executor on application shutdown."""
        _EXECUTOR.shutdown(wait=False)
        logger.debug("YouTubeSearch executor shut down")

    # ── Private sync (runs in executor) ───────────────────────────────────────

    def _sync_search(self, query: str) -> Optional[SearchResult]:
        opts = dict(_SEARCH_OPTS)
        if self._cookies_path:
            opts["cookiefile"] = self._cookies_path

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                return None

            entries: List = info.get("entries") or []
            if not entries:
                return None

            entry = entries[0]
            video_id: str = entry.get("id") or ""
            webpage_url: str = (
                entry.get("webpage_url")
                or entry.get("url")
                or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
            )
            if not webpage_url:
                logger.warning("[SEARCH] No URL in entry for '{}'", query)
                return None

            raw_duration = entry.get("duration")
            duration = int(raw_duration) if raw_duration else 0

            thumbnail: Optional[str] = entry.get("thumbnail")
            if thumbnail is None:
                thumbs: List = entry.get("thumbnails") or []
                if thumbs:
                    thumbnail = thumbs[-1].get("url")

            return SearchResult(
                title=entry.get("title") or "Unknown Title",
                duration=duration,
                webpage_url=webpage_url,
                uploader=(
                    entry.get("uploader")
                    or entry.get("channel")
                    or entry.get("uploader_id")
                    or "Unknown"
                ),
                thumbnail=thumbnail,
            )

        except yt_dlp.utils.DownloadError as exc:
            logger.error("[SEARCH] DownloadError for '{}': {}", query, exc)
            return None
        except Exception as exc:
            logger.error("[SEARCH] Unexpected error for '{}': {}", query, exc)
            return None


# ── Private helpers ────────────────────────────────────────────────────────────

def _resolve_cookies(cookies_path: Optional[str]) -> Optional[str]:
    """
    Resolve cookies_path to a writable /tmp copy.

    Render Secret Files are mounted read-only at /etc/secrets.
    yt-dlp writes a .lock file next to cookies.txt and crashes on EROFS
    if the directory is read-only.  Copy to /tmp once at startup.
    """
    if not cookies_path:
        return None

    filename = os.path.basename(cookies_path)
    candidates = [
        f"{COOKIES_SECRETS_DIR}/{filename}",
        cookies_path,
        os.path.join(os.getcwd(), cookies_path),
    ]

    found: Optional[str] = None
    for candidate in candidates:
        if os.path.isfile(candidate):
            found = candidate
            break

    if not found:
        logger.warning(
            "[SEARCH] cookies.txt not found (checked: {}) — public videos only",
            ", ".join(candidates),
        )
        return None

    tmp_path = f"{COOKIES_TMP_DIR}/{filename}"
    try:
        import shutil
        shutil.copy2(found, tmp_path)
        logger.info(
            "[SEARCH] cookies.txt copied to '{}' (source: '{}')",
            tmp_path, found,
        )
        return tmp_path
    except Exception as copy_err:
        logger.warning(
            "[SEARCH] Could not copy cookies to /tmp ({}), using '{}' directly",
            copy_err, found,
        )
        return found
