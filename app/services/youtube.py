"""
app.services.youtube
~~~~~~~~~~~~~~~~~~~~
YouTube search and audio-stream extraction via yt-dlp.

Design rules
------------
* yt-dlp is synchronous — every call runs in a thread-pool executor so
  the asyncio event loop is never blocked.
* We store only the permanent ``webpage_url`` from search results.
  A fresh, short-lived ``stream_url`` is fetched immediately before
  each playback attempt; it is never cached.
* cookies.txt is used when present; the bot works without it (public
  videos only).  A missing cookies file never causes a crash.
* All yt-dlp errors are caught and logged; callers receive ``None`` rather
  than an unhandled exception.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import yt_dlp

from app.core.logger import logger
from app.player.models import Track

# Single executor for all yt-dlp work — avoids spawning unlimited threads
_YT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdlp")

# ── yt-dlp option templates ───────────────────────────────────────────────────

_BASE_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "source_address": "0.0.0.0",   # bind to IPv4 — avoids IPv6 issues on some hosts
}

_SEARCH_OPTS: dict = {
    **_BASE_OPTS,
    # Prefer opus inside webm; fall back to any audio-only format
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "default_search": "ytsearch",
    # We only need metadata, not a downloaded file
    "skip_download": True,
}

_STREAM_OPTS: dict = {
    **_BASE_OPTS,
    "format": "bestaudio[ext=webm]/bestaudio/best",
    "skip_download": True,
}


class YouTubeService:
    """
    Wraps yt-dlp to provide async search and stream-URL extraction.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape-format cookies.txt file.  When the
        file exists it is used automatically.  When missing or ``None``
        the service operates without cookies (public videos only).
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        self._cookies_path: Optional[str] = None

        if cookies_path and os.path.isfile(cookies_path):
            self._cookies_path = cookies_path
            logger.info("YouTube: cookies.txt loaded from '{}'", cookies_path)
        elif cookies_path:
            logger.warning(
                "YouTube: cookies_path='{}' not found — continuing without cookies.",
                cookies_path,
            )

    # ── Public async API ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        """
        Search YouTube for *query* and return the first result as a Track.

        Returns ``None`` when no results are found or yt-dlp raises an error.
        The returned Track contains a permanent ``webpage_url`` but no
        stream URL — call :meth:`get_stream_url` immediately before playback.
        """
        logger.info("YouTube search: '{}'", query)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _YT_EXECUTOR,
            self._sync_search,
            query,
            requested_by_id,
            requested_by_name,
        )

    async def get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Resolve *webpage_url* to a direct audio stream URL.

        Always call this immediately before starting playback — the URL
        returned by YouTube expires quickly and must never be stored.
        Returns ``None`` on failure.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _YT_EXECUTOR,
            self._sync_get_stream_url,
            webpage_url,
        )

    # ── Private sync workers (run inside ThreadPoolExecutor) ──────────────────

    def _build_opts(self, base: dict) -> dict:
        """Merge base options with cookies if available."""
        opts = dict(base)
        if self._cookies_path:
            opts["cookiefile"] = self._cookies_path
        return opts

    def _sync_search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        opts = self._build_opts(_SEARCH_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                logger.warning("YouTube search returned no info for '{}'", query)
                return None

            entries = info.get("entries")
            if not entries:
                logger.warning("YouTube search returned no entries for '{}'", query)
                return None

            entry = entries[0]

            return Track(
                title=entry.get("title") or "Unknown Title",
                duration=int(entry.get("duration") or 0),
                webpage_url=entry.get("webpage_url") or entry.get("url", ""),
                uploader=entry.get("uploader") or entry.get("channel") or "Unknown",
                thumbnail=entry.get("thumbnail"),
                requested_by_id=requested_by_id,
                requested_by_name=requested_by_name,
            )

        except yt_dlp.utils.DownloadError as exc:
            logger.error("yt-dlp DownloadError during search '{}': {}", query, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error during YouTube search '{}': {}", query, exc)
            return None

    def _sync_get_stream_url(self, webpage_url: str) -> Optional[str]:
        opts = self._build_opts(_STREAM_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)

            if not info:
                logger.error("yt-dlp returned no info for '{}'", webpage_url)
                return None

            # Direct URL on the root info dict (most common case)
            if "url" in info:
                return info["url"]

            # Fall back to iterating formats — pick the best audio-only entry
            formats: list = info.get("formats") or []
            audio_formats = [
                f for f in formats
                if f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none")
                and f.get("url")
            ]
            if audio_formats:
                # Sort by bitrate descending and take the best
                audio_formats.sort(key=lambda f: f.get("abr") or 0, reverse=True)
                return audio_formats[0]["url"]

            # Last resort: any format with a URL
            for fmt in reversed(formats):
                if fmt.get("url"):
                    return fmt["url"]

            logger.error("No usable URL found in yt-dlp output for '{}'", webpage_url)
            return None

        except yt_dlp.utils.DownloadError as exc:
            logger.error("yt-dlp DownloadError resolving '{}': {}", webpage_url, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error resolving stream URL '{}': {}", webpage_url, exc)
            return None
