"""
app.search.resolver
~~~~~~~~~~~~~~~~~~~
Stream URL resolution via yt-dlp — full extraction, no searching.

Responsibility
--------------
Accept a permanent YouTube watch URL.
Return a direct CDN audio URL (e.g. rr*.googlevideo.com/...).
Nothing more.

Why this is separate from search/youtube.py
-------------------------------------------
Search (metadata-only, fast) and stream resolution (full extraction, slower)
are different operations with different options, timeouts, and failure modes.
Separating them keeps each module small and single-purpose, and avoids
triggering full CDN resolution at search time (CDN URLs expire in ~6 h).

Player client strategy
----------------------
``mweb`` (YouTube Mobile Web) is the primary client.

Since yt-dlp 2025.11.12, YouTube enforces Proof-of-Origin (PO) tokens
for all Innertube clients when requests originate from data-centre IP
ranges (Render, VPS, CI).  This now includes ``tv_embedded``
(TVHTML5_SIMPLY_EMBEDDED_PLAYER), which was previously exempt but is
no longer reliable from server IPs as of mid-2026.

``mweb`` and ``web`` are the correct primary clients because:
  - yt-dlp automatically uses Deno + yt-dlp-ejs to generate PO tokens
    for these clients when Deno is on PATH (which it is — installed to
    /usr/local/bin/deno in our Dockerfile).
  - No explicit PO-token configuration is needed; yt-dlp handles it.
  - ``mweb`` (mobile web) typically faces less aggressive bot detection
    from server IPs than the desktop ``web`` client.
  - ``tv_embedded`` is retained as a tertiary fallback for edge cases
    where the embed API still responds correctly.

``ios`` and ``android`` are removed: both require PO tokens on server
IPs and yt-dlp does not auto-generate PO tokens for native app clients,
so they always fail from Render.

Format note: ``mweb`` returns muxed or DASH streams depending on the
video.  Priority 1 (audio-only) handles DASH; Priority 2 (muxed with
audio) handles muxed.  FFmpeg/ntgcalls extracts the audio track either
way.

Why Python-layer resolution beats MediaStream(ytdlp_parameters=...)
-------------------------------------------------------------------
ntgcalls 2.2.5 accepts ytdlp_parameters as a string but does NOT forward
--cookies to its internal yt-dlp invocation.  Cookies applied as a Python
dict option (cookiefile="...") in this module DO work correctly.
Resolving the CDN URL here hands ntgcalls a direct https://... URL that
requires zero authentication — yt-dlp runs zero times inside ntgcalls.

Stage log: [RESOLVE]
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import yt_dlp
import yt_dlp.utils

from app.infrastructure.logger import logger
from app.shared.constants import (
    COOKIES_SECRETS_DIR,
    COOKIES_TMP_DIR,
    STREAM_RESOLVE_TIMEOUT_SEC,
    YT_EXECUTOR_NAME,
    YT_EXECUTOR_WORKERS,
)

# ── Thread executor ────────────────────────────────────────────────────────────
# Separate executor from search so a slow resolution doesn't block searches.
_RESOLVE_EXECUTOR = ThreadPoolExecutor(
    max_workers=YT_EXECUTOR_WORKERS,
    thread_name_prefix=f"{YT_EXECUTOR_NAME}_resolve",
)

# ── yt-dlp options for full extraction ────────────────────────────────────────
# mweb is first: YouTube Mobile Web client.  When Deno is on PATH and
# yt-dlp-ejs is installed (both are true in our Docker image), yt-dlp
# automatically generates a PO token via Deno for this client.  This is
# the correct bypass for Render's data-centre IP range as of yt-dlp 2026.x.
#
# web is second: standard desktop client.  Same Deno/PO-token generation
# path as mweb; returns DASH audio-only streams (Priority 1 in _sync_resolve).
#
# tv_embedded is third: the embedded-player endpoint.  Previously exempt from
# PO tokens but now blocked from many data-centre IPs.  Retained as a
# last-resort fallback for edge cases where it still responds correctly.
#
# ios and android are REMOVED: they require PO tokens but yt-dlp does not
# auto-generate PO tokens for native app clients — they always fail on Render.
#
# NO format selector — we fetch ALL formats and manually select the best
# audio stream, so we are never bound to a format ID that may disappear.
_STREAM_OPTS: Dict = {
    "quiet":         True,
    "no_warnings":   True,
    "noplaylist":    True,
    "skip_download": True,
    "geo_bypass":    True,
    "socket_timeout": 25,
    "retries":       3,
    # format intentionally omitted — manual selection in _sync_resolve()
    "extractor_args": {
        "youtube": {
            "player_client": ["mweb", "web", "tv_embedded"],
        }
    },
}


class StreamResolver:
    """
    Resolves a permanent YouTube watch URL to a direct CDN audio URL.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape cookies.txt file.
        Unlocks age-restricted / region-locked content.
        Shared /tmp copy from YouTubeSearch is used if available.
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        # Accept the already-resolved /tmp path from YouTubeSearch if possible.
        if cookies_path and os.path.isfile(cookies_path):
            self._cookies_path: Optional[str] = cookies_path
        else:
            self._cookies_path = _resolve_cookies_tmp(cookies_path)

    # ── Public API ────────────────────────────────────────────────────────────

    async def resolve(self, webpage_url: str) -> Optional[str]:
        """
        Resolve a direct CDN audio URL from a YouTube watch URL.

        Returns the direct https://... CDN URL on success.
        Returns None on failure — caller falls back to FFmpeg direct extraction.

        Stage log: [RESOLVE]
        """
        logger.debug("[RESOLVE] Resolving stream URL for: {}", webpage_url)
        loop = asyncio.get_running_loop()
        try:
            url = await asyncio.wait_for(
                loop.run_in_executor(
                    _RESOLVE_EXECUTOR,
                    self._sync_resolve,
                    webpage_url,
                ),
                timeout=STREAM_RESOLVE_TIMEOUT_SEC,
            )
            if url:
                logger.info(
                    "[RESOLVE] OK  url_preview={}...",
                    url[:60],
                )
            else:
                logger.warning(
                    "[RESOLVE] No direct URL found for '{}'", webpage_url,
                )
            return url
        except asyncio.TimeoutError:
            logger.error("[RESOLVE] Timed out for '{}'", webpage_url)
            return None

    @staticmethod
    def shutdown() -> None:
        """Drain the executor on application shutdown."""
        _RESOLVE_EXECUTOR.shutdown(wait=False)
        logger.debug("StreamResolver executor shut down")

    # ── Private sync (runs in executor) ───────────────────────────────────────

    def _sync_resolve(self, webpage_url: str) -> Optional[str]:
        """
        Full yt-dlp extraction using mweb as the primary client.

        mweb (YouTube Mobile Web) triggers yt-dlp's automatic PO-token
        generation via Deno + yt-dlp-ejs, which is the correct bypass for
        Render's data-centre IP range.  web is the secondary client using
        the same PO-token path.  tv_embedded is a last-resort fallback.

        URL selection priority:
          1. Best audio-only format (vcodec == none) sorted by bitrate
          2. Best muxed format that has audio (tv_embedded returns these)
          3. info["url"] when yt-dlp returns a single-format response
        """
        opts = dict(_STREAM_OPTS)
        if self._cookies_path:
            opts["cookiefile"] = self._cookies_path

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(webpage_url, download=False)

            if not info:
                return None

            formats: List[Dict] = info.get("formats") or []

            # Priority 1: audio-only streams
            audio_only = [
                f for f in formats
                if f.get("url")
                and f.get("acodec") not in (None, "none")
                and f.get("vcodec") in (None, "none")
            ]
            if audio_only:
                best = sorted(
                    audio_only,
                    key=lambda f: float(f.get("tbr") or f.get("abr") or 0),
                    reverse=True,
                )
                logger.debug(
                    "[RESOLVE] Selected audio-only  acodec={}  abr={}kbps",
                    best[0].get("acodec"),
                    best[0].get("abr") or best[0].get("tbr"),
                )
                return best[0]["url"]

            # Priority 2: muxed streams with audio
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
                logger.debug(
                    "[RESOLVE] Selected muxed  ext={}  tbr={}kbps",
                    best_mux[0].get("ext"),
                    best_mux[0].get("tbr"),
                )
                return best_mux[0]["url"]

            # Priority 3: single-format response
            return info.get("url")

        except yt_dlp.utils.DownloadError as exc:
            logger.error("[RESOLVE] DownloadError for '{}': {}", webpage_url, exc)
            return None
        except Exception as exc:
            logger.error("[RESOLVE] Unexpected error for '{}': {}", webpage_url, exc)
            return None


# ── Private helpers ────────────────────────────────────────────────────────────

def _resolve_cookies_tmp(cookies_path: Optional[str]) -> Optional[str]:
    """Return the /tmp copy of cookies_path if it exists, else None."""
    if not cookies_path:
        return None
    filename = os.path.basename(cookies_path)
    tmp = f"{COOKIES_TMP_DIR}/{filename}"
    if os.path.isfile(tmp):
        return tmp
    secret = f"{COOKIES_SECRETS_DIR}/{filename}"
    if os.path.isfile(secret):
        return secret
    if os.path.isfile(cookies_path):
        return cookies_path
    return None
