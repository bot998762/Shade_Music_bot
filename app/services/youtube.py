"""
app.services.youtube
~~~~~~~~~~~~~~~~~~~~
YouTube search via yt-dlp — metadata only.

Stream URL strategy (py-tgcalls==2.3.3)
----------------------------------------
py-tgcalls 2.3.x includes its own yt-dlp integration. MediaStream accepts
YouTube watch URLs directly and handles extraction internally. Therefore:

  1. YouTubeService.search() fetches metadata only (title, duration, etc.)
     using extract_flat="in_playlist" — fast, no format resolution.

  2. The webpage_url stored on Track is passed directly to MediaStream
     via FFmpegStreamBuilder.build_from_youtube(). No pre-resolved CDN
     URL is needed.

  3. get_stream_url() is kept as a no-op passthrough for backward
     compatibility with any callers that still invoke it. It simply
     returns the webpage_url unchanged so MediaStream receives the
     YouTube URL.

Why server IPs fail with the web player client
----------------------------------------------
YouTube requires a Proof-of-Origin (PO) Token for the "web" player client
on datacenter IPs. py-tgcalls' internal yt-dlp uses the android/iOS
clients which bypass this requirement — which is why passing the URL
directly to MediaStream works reliably on Render, while manual yt-dlp
with the "web" client does not.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import yt_dlp
import yt_dlp.utils

from app.core.logger import logger
from app.player.models import Track

_YT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdlp_search")
_SEARCH_TIMEOUT_SEC = 30

_SEARCH_OPTS: Dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "extract_flat": "in_playlist",   # metadata only — no format resolution
    "default_search": "ytsearch",
    "skip_download": True,
    "geo_bypass": True,
    "socket_timeout": 20,
    "retries": 2,
}


class YouTubeService:
    """
    YouTube search — returns Track metadata.

    Stream URL resolution is handled by py-tgcalls internally when
    MediaStream receives the webpage_url directly.
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        self._cookies_path: Optional[str] = None
        if cookies_path:
            import os
            if os.path.isfile(cookies_path):
                self._cookies_path = cookies_path
                logger.info("YouTube: cookies.txt loaded from '{}'", cookies_path)
            else:
                logger.warning(
                    "YouTube: cookies_path='{}' not found — continuing without cookies.",
                    cookies_path,
                )

    @staticmethod
    def shutdown() -> None:
        _YT_EXECUTOR.shutdown(wait=False)
        logger.debug("YouTubeService executor shut down")

    async def search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        """
        Search YouTube and return the first result as a Track.

        Uses extract_flat mode — no format resolution, no yt-dlp extraction
        of the audio stream. Fast and reliable from server IPs.

        Returns None on failure.
        """
        logger.info("YouTube search: '{}'", query)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    _YT_EXECUTOR,
                    self._sync_search,
                    query,
                    requested_by_id,
                    requested_by_name,
                ),
                timeout=_SEARCH_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("YouTube search timed out for: '{}'", query)
            return None

    async def get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Passthrough — returns webpage_url unchanged.

        py-tgcalls handles stream URL resolution internally when
        MediaStream receives the YouTube watch URL directly.
        Keeping this method avoids breaking any callers that still
        invoke it; the returned value (the YouTube URL itself) is
        passed to FFmpegStreamBuilder.build_from_youtube() which
        gives it directly to MediaStream.
        """
        return webpage_url

    def _build_opts(self, base: Dict) -> Dict:
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
        """Synchronous search — runs in the thread executor."""
        opts = self._build_opts(_SEARCH_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                logger.warning("Search: no info for '{}'", query)
                return None

            entries = info.get("entries") or []
            if not entries:
                logger.warning("Search: no entries for '{}'", query)
                return None

            entry = entries[0]
            video_id: str = entry.get("id") or ""
            webpage_url: str = (
                entry.get("webpage_url")
                or entry.get("url")
                or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
            )
            if not webpage_url:
                logger.warning("Search: no URL for '{}'", query)
                return None

            raw_duration = entry.get("duration")
            duration = int(raw_duration) if raw_duration else 0

            thumbnail: Optional[str] = entry.get("thumbnail")
            if thumbnail is None:
                thumbs: List = entry.get("thumbnails") or []
                if thumbs:
                    thumbnail = thumbs[-1].get("url")

            track = Track(
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
                requested_by_id=requested_by_id,
                requested_by_name=requested_by_name,
            )
            logger.info(
                "Search OK: '{}' → '{}' ({}s) url='{}'",
                query, track.title, track.duration, webpage_url,
            )
            return track

        except yt_dlp.utils.DownloadError as exc:
            logger.error("DownloadError during search '{}': {}", query, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error during search '{}': {}", query, exc)
            return None
