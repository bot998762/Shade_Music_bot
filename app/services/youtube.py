"""
app.services.youtube
~~~~~~~~~~~~~~~~~~~~
YouTube search + stream URL extraction via yt-dlp — Python layer only.

Two-phase design
-----------------
Phase 1 — Search  (search())
    extract_flat="in_playlist" — metadata only, fast, no format resolution.
    Returns Track(webpage_url, title, duration, ...).

Phase 2 — Stream URL  (get_stream_url())
    Full format extraction with cookies + android client in the Python layer.
    Returns a direct CDN audio URL (expires in ~6 h — resolved just before play).
    This is the KEY FIX: stream URL is resolved here in Python (where cookies
    are correctly applied as a Python dict option) instead of inside ntgcalls'
    C++ yt-dlp invocation (which ignores ytdlp_parameters in 2.2.5).

Why Python extraction beats ytdlp_parameters on MediaStream
-------------------------------------------------------------
ntgcalls 2.2.5 accepts ytdlp_parameters as a string but does NOT forward
--cookies or --extractor-args to its internal yt-dlp call.  Every attempt
to pass credentials via MediaStream(ytdlp_parameters=...) results in:

    ERROR: [youtube] ...: Sign in to confirm you're not a bot.

By resolving the direct CDN URL here (Python yt-dlp, cookiefile dict option,
android player_client), we hand ntgcalls a direct https://... audio URL that
requires no further authentication — yt-dlp runs zero times inside ntgcalls.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import yt_dlp
import yt_dlp.utils

from app.core.logger import logger
from app.player.models import Track

_YT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdlp_search")
_SEARCH_TIMEOUT_SEC = 30
_STREAM_TIMEOUT_SEC = 45   # full format extraction takes longer than flat search

# ── Search opts (metadata only) ───────────────────────────────────────────────
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
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}

# ── Stream-URL opts (full extraction) ─────────────────────────────────────────
# No extract_flat — we need actual format data, not just metadata.
# NO format selector — android client returns different format IDs than web
#   client. "bestaudio/best" fails with "Requested format is not available"
#   because yt-dlp can't find a matching format in android's response.
#   Instead, we fetch ALL formats and pick the best audio-only stream manually
#   in _sync_get_stream_url() using the formats list.
# android player_client bypasses PO Token requirement on datacenter IPs.
# cookiefile is added dynamically in _build_opts() if cookies are available.
_STREAM_OPTS: Dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "geo_bypass": True,
    "socket_timeout": 25,
    "retries": 3,
    # format intentionally omitted — manual selection in _sync_get_stream_url
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}


class YouTubeService:
    """
    YouTube search + stream URL resolution.

    search()         — fast metadata fetch (extract_flat), returns Track.
    get_stream_url() — full extraction with cookies + android client,
                       returns a direct CDN audio URL for FFmpeg.
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        self._cookies_path: Optional[str] = None
        if cookies_path:
            filename = os.path.basename(cookies_path)
            candidates = [
                f"/etc/secrets/{filename}",
                cookies_path,
                os.path.join(os.getcwd(), cookies_path),
            ]
            found: Optional[str] = None
            for candidate in candidates:
                if os.path.isfile(candidate):
                    found = candidate
                    break

            if found:
                # /etc/secrets is read-only on Render — yt-dlp writes a .lock
                # file next to cookies.txt and crashes with EROFS on read-only FS.
                # Copy to /tmp (writable) once at startup.
                tmp_path = f"/tmp/{filename}"
                try:
                    import shutil
                    shutil.copy2(found, tmp_path)
                    self._cookies_path = tmp_path
                    logger.info(
                        "YouTube: cookies.txt copied to '{}' (source: '{}')",
                        tmp_path, found,
                    )
                except Exception as copy_err:
                    self._cookies_path = found
                    logger.warning(
                        "YouTube: could not copy cookies to /tmp ({}), using '{}' directly",
                        copy_err, found,
                    )
            else:
                logger.warning(
                    "YouTube: cookies.txt not found (checked: {}) — no auth",
                    ", ".join(candidates),
                )

    @staticmethod
    def shutdown() -> None:
        _YT_EXECUTOR.shutdown(wait=False)
        logger.debug("YouTubeService executor shut down")

    # ── Public: search ────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        """
        Search YouTube and return the first result as a Track.

        Uses extract_flat — metadata only, no audio CDN URL.
        The webpage_url in the returned Track is a permanent YouTube page URL.
        Call get_stream_url(track.webpage_url) to resolve the direct CDN URL
        before passing to FFmpegStreamBuilder.
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

    # ── Public: stream URL ────────────────────────────────────────────────────

    async def get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Resolve a direct CDN audio URL from a YouTube watch URL.

        Uses full yt-dlp extraction (NOT extract_flat) with:
          - cookiefile applied as a Python dict option (guaranteed to work)
          - android player_client (bypasses PO Token on datacenter IPs)

        Returns the direct https://... audio URL on success, None on failure.
        The caller (MusicEngine._resolve_stream) falls back to
        FFmpegStreamBuilder.build_from_youtube() if this returns None.

        NOTE: The returned CDN URL expires in ~6 hours. Always call this
        just before playback starts, never at queue-add time.
        """
        logger.debug("Resolving stream URL for: {}", webpage_url)
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    _YT_EXECUTOR,
                    self._sync_get_stream_url,
                    webpage_url,
                ),
                timeout=_STREAM_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("get_stream_url timed out: '{}'", webpage_url)
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

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

    def _sync_get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Synchronous stream URL extraction — runs in the thread executor.

        Extracts the best audio CDN URL using Python yt-dlp with cookies
        and android player_client.  This is the layer where cookie auth
        actually works — unlike ytdlp_parameters on MediaStream which
        ntgcalls 2.2.5 silently ignores.

        URL selection priority:
          1. info["url"]  — set when yt-dlp selects a single best format
          2. Best audio-only format from info["formats"] list
          3. Best format with any audio from info["formats"] list
        """
        opts = self._build_opts(_STREAM_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)

            if not info:
                logger.warning("get_stream_url: no info returned for '{}'", webpage_url)
                return None

            formats: List[Dict] = info.get("formats") or []

            # Case 1: no format selector used → pick best audio-only by bitrate
            # Priority 1 — audio-only streams (vcodec is none/null)
            audio_only = [
                f for f in formats
                if f.get("url")
                and f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none")
            ]
            if audio_only:
                # Sort by audio bitrate descending; tbr is total bitrate (= abr for audio-only)
                best = sorted(
                    audio_only,
                    key=lambda f: float(f.get("tbr") or f.get("abr") or 0),
                    reverse=True,
                )
                direct_url: Optional[str] = best[0]["url"]
                logger.debug(
                    "Format selected: audio-only acodec={} abr={}kbps",
                    best[0].get("acodec"), best[0].get("abr") or best[0].get("tbr"),
                )
            else:
                # Priority 2 — muxed streams (any format that has audio)
                with_audio = [
                    f for f in formats
                    if f.get("url")
                    and f.get("acodec") not in (None, "none")
                ]
                if with_audio:
                    best_mux = sorted(
                        with_audio,
                        key=lambda f: float(f.get("tbr") or 0),
                        reverse=True,
                    )
                    direct_url = best_mux[0]["url"]
                    logger.debug(
                        "Format selected: muxed ext={} tbr={}kbps",
                        best_mux[0].get("ext"), best_mux[0].get("tbr"),
                    )
                else:
                    # Priority 3 — single-format response (info["url"] set directly)
                    direct_url = info.get("url")

            if direct_url:
                logger.info(
                    "Stream URL resolved: {} → {}...",
                    webpage_url[-20:], direct_url[:60],
                )
                return direct_url

            logger.warning("get_stream_url: could not find direct URL for '{}'", webpage_url)
            return None

        except yt_dlp.utils.DownloadError as exc:
            logger.error("get_stream_url DownloadError '{}': {}", webpage_url, exc)
            return None
        except Exception as exc:
            logger.error("get_stream_url unexpected error '{}': {}", webpage_url, exc)
            return None
