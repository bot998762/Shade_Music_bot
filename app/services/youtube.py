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
* All yt-dlp errors are caught and re-raised as ``None``; callers never
  see an unhandled exception.
* Format resolution uses a cascading fallback chain — the bot never dies
  because one container (m4a / webm) is unavailable for a given video.

Format-fallback strategy
------------------------
Priority   Selector                                    Notes
--------   -----------------------------------------   --------------------------
1          bestaudio[ext=m4a]/bestaudio[ext=webm]/     Ideal: pure audio, small
           bestaudio/best
2          bestaudio/best                               Any audio, no container
3          best                                         Combined a/v as last resort
MANUAL     (no selector — scan formats[])               Absolute last resort
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

# Single bounded executor — avoids spawning unlimited threads on Render.
_YT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdlp")

# ── Format fallback chain ─────────────────────────────────────────────────────
# Each entry is tried in sequence for stream-URL resolution.  We stop at the
# first success.  Using three stages means every video on YouTube will resolve
# to *some* playable URL regardless of which renditions the uploader has enabled.

_FORMAT_CHAIN: List[str] = [
    # Stage 1 — pure audio, prefer m4a (AAC, natively compatible) then webm (Opus)
    "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    # Stage 2 — any bestaudio regardless of container
    "bestaudio/best",
    # Stage 3 — absolute fallback: any format (muxed a+v is fine; FFmpeg extracts audio)
    "best",
]

# Lower-cased substrings present in yt-dlp errors that mean "format not available".
# These are safe to retry with the next format selector.
_FORMAT_ERR_PHRASES = (
    "requested format is not available",
    "no video formats found",
    "format is not available",
    "no formats",
)

# ── HTTP headers ──────────────────────────────────────────────────────────────
# Mimic a modern browser so YouTube does not serve bot-detection challenges.
_HTTP_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
}

# ── Base yt-dlp options applied to every call ─────────────────────────────────
_BASE_OPTS: Dict = {
    # Silence yt-dlp's own stdout/stderr — we handle all output via loguru.
    "quiet": True,
    "no_warnings": True,
    # Safety / reliability
    "noplaylist": True,          # never accidentally pull a whole playlist
    "extract_flat": False,       # always resolve full metadata
    "geo_bypass": True,          # attempt to bypass geo-restrictions automatically
    "nocheckcertificate": True,  # avoids SSL issues on some VPS / Render environments
    # Network resilience
    "source_address": "0.0.0.0",  # bind to IPv4; avoids IPv6 timeouts on Render
    "socket_timeout": 30,          # per-socket timeout in seconds
    "retries": 5,                  # retry failed HTTP requests up to 5 times
    "fragment_retries": 5,         # retry DASH / HLS segment failures
    # Browser impersonation
    "http_headers": _HTTP_HEADERS,
}

# ── Search-specific options ───────────────────────────────────────────────────
# We only need metadata during search — no format selector is needed because
# we never play back the result directly.  Adding a format selector here would
# cause yt-dlp to validate format availability and *fail* with the same
# "Requested format is not available" error before we even try to stream.
_SEARCH_OPTS: Dict = {
    **_BASE_OPTS,
    "default_search": "ytsearch",
    "skip_download": True,
}

# ── Stream-resolution base options ────────────────────────────────────────────
# The "format" key is injected per-attempt inside _sync_get_stream_url so that
# different stages of the fallback chain can use different selectors.
_STREAM_BASE_OPTS: Dict = {
    **_BASE_OPTS,
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

        Uses a cascading format-selector chain so playback always succeeds
        even when a specific container (m4a / webm) is unavailable for a
        given video.

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

    def _build_opts(self, base: Dict) -> Dict:
        """Merge *base* options with cookies path when available."""
        opts = dict(base)
        if self._cookies_path:
            opts["cookiefile"] = self._cookies_path
        return opts

    # ── Search ────────────────────────────────────────────────────────────────

    def _sync_search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        """
        Synchronous YouTube search (runs in executor).

        Uses ``ytsearch1:`` prefix so yt-dlp executes a YouTube search and
        returns the first result.  No format selector is specified — we only
        need metadata, not a playback URL.
        """
        opts = self._build_opts(_SEARCH_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                logger.warning("YouTube search: no info returned for '{}'", query)
                return None

            entries = info.get("entries")
            if not entries:
                logger.warning("YouTube search: no entries for '{}'", query)
                return None

            entry = entries[0]
            track = Track(
                title=entry.get("title") or "Unknown Title",
                duration=int(entry.get("duration") or 0),
                webpage_url=entry.get("webpage_url") or entry.get("url", ""),
                uploader=(
                    entry.get("uploader")
                    or entry.get("channel")
                    or "Unknown"
                ),
                thumbnail=entry.get("thumbnail"),
                requested_by_id=requested_by_id,
                requested_by_name=requested_by_name,
            )
            logger.info(
                "YouTube search OK: query='{}' → title='{}' duration={}s",
                query,
                track.title,
                track.duration,
            )
            return track

        except yt_dlp.utils.DownloadError as exc:
            logger.error(
                "yt-dlp DownloadError during search '{}': {}", query, exc
            )
            return None
        except yt_dlp.utils.ExtractorError as exc:
            logger.error(
                "yt-dlp ExtractorError during search '{}': {}", query, exc
            )
            return None
        except Exception as exc:
            logger.error(
                "yt-dlp unexpected error during search '{}': {}", query, exc
            )
            return None

    # ── Stream URL resolution ─────────────────────────────────────────────────

    def _sync_get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Synchronous stream-URL resolution (runs in executor).

        Tries each format selector in ``_FORMAT_CHAIN`` in order.  On a
        format-availability error it logs a warning and moves to the next
        selector.  After all selectors are exhausted it falls back to
        ``_manual_format_scan`` which queries all formats and picks the
        best audio track manually.

        Non-format errors (geo-block, age-gate, private video, network
        error) abort immediately and return ``None``.
        """
        last_format_error: Optional[Exception] = None

        for attempt, fmt in enumerate(_FORMAT_CHAIN, start=1):
            opts = self._build_opts({**_STREAM_BASE_OPTS, "format": fmt})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(webpage_url, download=False)

                url = _pick_url_from_info(info)
                if url:
                    logger.info(
                        "yt-dlp stream resolved — "
                        "attempt={}/{} format='{}' url_chars={}",
                        attempt,
                        len(_FORMAT_CHAIN),
                        fmt,
                        len(url),
                    )
                    return url

                # extract_info returned something but no URL could be found.
                logger.warning(
                    "yt-dlp attempt={} format='{}' produced no URL for '{}', "
                    "trying next fallback",
                    attempt,
                    fmt,
                    webpage_url,
                )

            except yt_dlp.utils.DownloadError as exc:
                err_lower = str(exc).lower()
                if any(phrase in err_lower for phrase in _FORMAT_ERR_PHRASES):
                    logger.warning(
                        "yt-dlp attempt={}/{} format='{}' not available for '{}' "
                        "→ trying next fallback",
                        attempt,
                        len(_FORMAT_CHAIN),
                        fmt,
                        webpage_url,
                    )
                    last_format_error = exc
                    continue  # move to next format selector

                # Non-format error: geo-block, age-gate, private, network, etc.
                # These won't be fixed by trying a different format selector.
                logger.error(
                    "yt-dlp DownloadError (non-format) for '{}': {}",
                    webpage_url,
                    exc,
                )
                return None

            except yt_dlp.utils.ExtractorError as exc:
                logger.error(
                    "yt-dlp ExtractorError for '{}': {}", webpage_url, exc
                )
                return None

            except Exception as exc:
                logger.error(
                    "yt-dlp unexpected error for '{}': {}", webpage_url, exc
                )
                return None

        # ── All format selectors exhausted ────────────────────────────────────
        logger.warning(
            "All {} format selectors failed for '{}' (last error: {}), "
            "falling back to manual format scan",
            len(_FORMAT_CHAIN),
            webpage_url,
            last_format_error,
        )
        url = self._manual_format_scan(webpage_url)
        if url:
            return url

        logger.error(
            "yt-dlp: could not resolve any playable URL for '{}' "
            "after {} attempts + manual scan",
            webpage_url,
            len(_FORMAT_CHAIN),
        )
        return None

    def _manual_format_scan(self, webpage_url: str) -> Optional[str]:
        """
        Last-resort format resolution.

        Fetches the full format list without a selector and manually picks
        the best audio-only stream by bitrate, falling back to any format
        that has a URL.
        """
        # No "format" key → yt-dlp returns all available formats.
        opts = self._build_opts({**_STREAM_BASE_OPTS})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)

            if not info:
                return None

            # Some extractors put the URL directly on the top-level dict.
            if direct_url := info.get("url"):
                logger.info(
                    "Manual scan: direct URL found on info root for '{}'",
                    webpage_url,
                )
                return direct_url

            formats: List[Dict] = info.get("formats") or []
            if not formats:
                return None

            # Priority 1: audio-only streams (vcodec=none), best bitrate first.
            audio_only = [
                f for f in formats
                if f.get("vcodec") in (None, "none")
                and f.get("acodec") not in (None, "none")
                and f.get("url")
            ]
            if audio_only:
                best = max(
                    audio_only,
                    key=lambda f: float(f.get("abr") or f.get("tbr") or 0),
                )
                logger.info(
                    "Manual scan: selected audio-only ext={} abr={}kbps for '{}'",
                    best.get("ext", "?"),
                    best.get("abr", "?"),
                    webpage_url,
                )
                return best["url"]

            # Priority 2: combined a+v (FFmpeg will extract the audio track).
            for fmt in reversed(formats):
                if fmt.get("url"):
                    logger.info(
                        "Manual scan: fallback to combined ext={} for '{}' "
                        "(FFmpeg will extract audio)",
                        fmt.get("ext", "?"),
                        webpage_url,
                    )
                    return fmt["url"]

        except Exception as exc:
            logger.error(
                "yt-dlp manual format scan failed for '{}': {}",
                webpage_url,
                exc,
            )

        return None


# ── Module-level helper — no class state needed ───────────────────────────────

def _pick_url_from_info(info: Optional[Dict]) -> Optional[str]:
    """
    Extract the best audio URL from a yt-dlp info dict.

    When yt-dlp uses a format selector it pre-selects a format and puts its
    URL directly on ``info["url"]``.  If that key is absent (e.g. for
    multi-format responses) we fall back to scanning ``info["formats"]``.
    """
    if not info:
        return None

    # Fast path — yt-dlp already selected and resolved a format.
    if url := info.get("url"):
        return url

    # Slow path — scan formats and pick the best audio-only entry.
    formats: List[Dict] = info.get("formats") or []

    audio_only = [
        f for f in formats
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("url")
    ]
    if audio_only:
        best = max(
            audio_only,
            key=lambda f: float(f.get("abr") or f.get("tbr") or 0),
        )
        return best["url"]

    # Absolute last resort: any format with a URL.
    for fmt in reversed(formats):
        if fmt.get("url"):
            return fmt["url"]

    return None
