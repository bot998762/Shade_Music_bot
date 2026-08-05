"""
app.services.youtube
~~~~~~~~~~~~~~~~~~~~
YouTube search and audio-stream extraction via yt-dlp.

Why servers fail at YouTube extraction (root cause analysis)
-------------------------------------------------------------
YouTube has, since ~2024, required a Proof-of-Origin (PO) Token for the
``web`` player client on requests originating from datacenter/server IPs
(Render, AWS, GCP, etc.).  Without a PO Token, the ``web`` client returns
an empty format list for every video — every selector, including "best" and
"bestaudio", raises "Requested format is not available".

The ``android_vr`` player client does NOT require a PO Token and exposes
the full DASH format list (both audio-only and combined streams).  Placing
it first in the client priority list bypasses the PO Token requirement
entirely for the vast majority of videos.

The ``ios`` client is a secondary fallback that also works without PO Tokens.
The ``web`` client remains last in the list so that cookies.txt, if provided,
can be used as a PO Token workaround for edge cases (age-restricted, etc.).

Extraction strategy (two-stage, never crashes on format failure)
-----------------------------------------------------------------
Stage 1 — Dynamic extraction (primary, preferred)
    ``extract_info(process=False)`` runs the extractor (including nsig
    decryption for signed YouTube URLs) but skips ``process_ie_result()``,
    the function that validates format availability.  We receive the raw
    formats list and pick the best audio track ourselves via ``_pick_audio_url()``,
    which never crashes — it just returns None if no format is usable.

Stage 2 — Selector fallback (secondary)
    If Stage 1 yields nothing (rare; some extractors don't populate
    ``formats[]`` without processing), try ``bestaudio/best`` then ``best``
    as explicit format selectors.  With ``android_vr`` in the player client
    list, these succeed reliably from server IPs.

Live stream detection
---------------------
Live streams set ``duration=None`` or ``duration=0`` and set ``is_live=True``
in the info dict.  We detect this and set Track.duration = 0 so the UI
can display "LIVE" instead of a nonsensical duration.

Playlists
---------
We always set ``noplaylist=True`` during search and stream resolution so a
playlist URL does not accidentally expand to thousands of entries.  Callers
that want playlist support should call ``search()`` once per video URL.

Cookies
-------
``cookiefile`` is added to yt-dlp options whenever a Netscape-format
cookies.txt is available at the configured path.  This gives access to:
  • Age-restricted videos
  • Region-locked videos (when the cookie account has the right region)
  • Videos behind login walls (rare on YouTube but possible)
  • Signature-protected or PO-Token-requiring videos when the cookie
    carries a valid PO Token credential.
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

# Bounded executor — yt-dlp is I/O-bound (network) + CPU-bound (JS decryption).
# 5 threads allows concurrent search + stream resolution without overwhelming
# a Render Starter instance (1 vCPU shared).
_YT_EXECUTOR = ThreadPoolExecutor(max_workers=5, thread_name_prefix="ytdlp")

# Timeout per yt-dlp operation.  A genuinely stalled call (DNS timeout,
# CDN hiccup) is abandoned after this many seconds and treated as a
# resolution failure.  The executor thread is released.
_YTDLP_TIMEOUT_SEC = 60

# yt-dlp format-availability error substrings.  Used in Stage 2 to decide
# whether a different selector might succeed.
_FORMAT_ERR_PHRASES = (
    "requested format is not available",
    "no video formats found",
    "format is not available",
    "no formats",
    "no suitable formats",
)

# HTTP headers that mimic a real browser.  Reduces bot-detection false positives.
_HTTP_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Sec-Fetch-Mode": "navigate",
}

# ── Base options (applied to every yt-dlp call) ───────────────────────────────
#
# player_client priority:
#   1. android_vr — no PO Token required, full DASH format list on server IPs
#   2. ios        — no PO Token required, good fallback
#   3. web        — requires PO Token on server IPs; useful only with cookies.txt
_BASE_OPTS: Dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "http_headers": _HTTP_HEADERS,
    "extractor_args": {
        "youtube": {
            "player_client": ["android_vr", "ios", "web"],
        }
    },
}

# ── Search options ────────────────────────────────────────────────────────────
# extract_flat="in_playlist" → metadata only, zero format resolution.
# This prevents yt-dlp from failing on unusual codecs (iamf, AV1-only)
# during the search step where we only need title/duration/url.
_SEARCH_OPTS: Dict = {
    **_BASE_OPTS,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch",
    "skip_download": True,
}

# ── Stream-resolution base options ────────────────────────────────────────────
# No "format" key here — Stage 1 bypasses selection; Stage 2 injects it.
_STREAM_BASE_OPTS: Dict = {
    **_BASE_OPTS,
    "skip_download": True,
}


# ── Module-level format-selection helpers ─────────────────────────────────────

def _pick_audio_url(formats: List[Dict]) -> Optional[str]:
    """
    Choose the best playable audio stream URL from a raw yt-dlp formats list.

    Never hard-codes a container (m4a, webm, opus) or codec — works on
    whatever the extractor actually returns.  This makes it robust against
    YouTube experimenting with new codec rollouts.

    Selection priority
    ------------------
    1. Audio-only streams (vcodec == "none"), highest bitrate first.
       These are ideal: FFmpeg only has to decode audio, no video demux.
    2. Combined audio+video streams, lowest resolution first.
       We want the smallest video track to minimise bandwidth; FFmpeg will
       extract only the audio channel via the -vn flag in ffmpeg_parameters.
    3. Any HTTPS URL as absolute last resort.

    Skips manifests (m3u8, mpd) and non-HTTP protocols (rtmp, mms) by default
    because they require special FFmpeg demuxer setup; HLS (m3u8) is allowed
    as a last resort since FFmpeg handles it natively.
    """
    if not formats:
        return None

    def _is_direct(f: Dict) -> bool:
        url = f.get("url", "")
        proto = f.get("protocol", "https")
        return url.startswith("http") and proto not in (
            "m3u8_native", "rtmp", "rtmpe", "mms", "rtsp",
        )

    # Prefer direct HTTP sources; fall back to m3u8 (HLS) as last resort.
    direct = [f for f in formats if _is_direct(f)]
    if not direct:
        # Allow m3u8 as fallback — FFmpeg handles HLS natively.
        direct = [f for f in formats if f.get("url", "").startswith("http")]
    if not direct:
        return None

    # ── Priority 1: audio-only ────────────────────────────────────────────
    audio_only = [
        f for f in direct
        if f.get("vcodec") in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    if audio_only:
        best = max(
            audio_only,
            key=lambda f: float(f.get("abr") or f.get("tbr") or 0),
        )
        logger.debug(
            "pick: audio-only  ext={}  abr={}kbps  acodec={}",
            best.get("ext", "?"),
            best.get("abr", "?"),
            best.get("acodec", "?"),
        )
        return best["url"]

    # ── Priority 2: combined a+v with audio track ─────────────────────────
    has_audio = [
        f for f in direct
        if f.get("acodec") not in (None, "none")
    ]
    if has_audio:
        best = min(
            has_audio,
            key=lambda f: (
                f.get("height") or 9999,
                -(float(f.get("abr") or 0)),
            ),
        )
        logger.debug(
            "pick: combined a+v  ext={}  height={}",
            best.get("ext", "?"),
            best.get("height", "?"),
        )
        return best["url"]

    # ── Priority 3: any HTTPS URL ─────────────────────────────────────────
    for fmt in reversed(direct):
        if fmt.get("url"):
            logger.debug("pick: last-resort  ext={}", fmt.get("ext", "?"))
            return fmt["url"]

    return None


def _pick_url_from_info(info: Optional[Dict]) -> Optional[str]:
    """
    Extract a playable URL from a fully-processed yt-dlp info dict.

    When yt-dlp ran format selection, the chosen URL is in ``info["url"]``.
    Falls back to scanning ``info["formats"]`` when that key is absent.
    """
    if not info:
        return None
    if url := info.get("url"):
        return url
    return _pick_audio_url(info.get("formats") or [])


# ── Service class ─────────────────────────────────────────────────────────────

class YouTubeService:
    """
    Async YouTube search and audio-stream extraction backed by yt-dlp.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape-format cookies.txt.  The service runs
        without it for all public videos; cookies unlock age-gated / region-
        locked / login-required content.
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

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @staticmethod
    def shutdown() -> None:
        """
        Gracefully shut down the shared thread executor.

        Call from ApplicationLifecycle.stop() so in-flight yt-dlp threads
        are released cleanly on process exit rather than being abandoned.
        """
        _YT_EXECUTOR.shutdown(wait=False)
        logger.debug("YouTubeService executor shut down")

    # ── Public async API ──────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> Optional[Track]:
        """
        Search YouTube for *query* and return the first result as a Track.

        Returns None on any failure.  The Track carries only metadata and a
        permanent ``webpage_url``; the stream URL is resolved separately, just
        before playback, because streaming URLs expire quickly.
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
                timeout=_YTDLP_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(
                "YouTube search timed out after {}s for query: '{}'",
                _YTDLP_TIMEOUT_SEC, query,
            )
            return None

    async def get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Resolve *webpage_url* to a direct, playable audio stream URL.

        Always call this immediately before starting/changing a stream —
        the returned URL is pre-signed and typically expires in ~6 hours.
        Returns None on any failure.
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    _YT_EXECUTOR,
                    self._sync_get_stream_url,
                    webpage_url,
                ),
                timeout=_YTDLP_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Stream URL resolution timed out after {}s for: '{}'",
                _YTDLP_TIMEOUT_SEC, webpage_url,
            )
            return None

    # ── Private sync workers (run in thread pool) ─────────────────────────────

    def _build_opts(self, base: Dict) -> Dict:
        """Merge *base* options with cookies when available."""
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
        """
        Synchronous YouTube search — runs in the thread executor.

        Uses extract_flat="in_playlist" so yt-dlp only fetches metadata
        and does not attempt format resolution during search.  This prevents
        failures on videos with unusual codecs (iamf, AV1-only, etc.) that
        would cause "Requested format is not available" at search time.
        """
        opts = self._build_opts(_SEARCH_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # ytsearch1: returns exactly 1 result
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                logger.warning("Search: no info returned for '{}'", query)
                return None

            entries = info.get("entries")
            if not entries:
                logger.warning("Search: no entries returned for '{}'", query)
                return None

            entry = entries[0]

            # With extract_flat="in_playlist", webpage_url may be absent.
            # "url" holds the watch URL in flat mode.
            video_id: str = entry.get("id") or ""
            webpage_url: str = (
                entry.get("webpage_url")
                or entry.get("url")
                or (
                    f"https://www.youtube.com/watch?v={video_id}"
                    if video_id else ""
                )
            )
            if not webpage_url:
                logger.warning(
                    "Search: could not determine webpage_url for '{}'", query
                )
                return None

            # Duration — None for live streams.
            raw_duration = entry.get("duration")
            duration = int(raw_duration) if raw_duration else 0

            # Thumbnail — scalar field or last item in thumbnails list.
            thumbnail: Optional[str] = entry.get("thumbnail")
            if thumbnail is None:
                thumbs: list = entry.get("thumbnails") or []
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
                "Search OK: '{}' → title='{}' duration={}s live={} url='{}'",
                query, track.title, track.duration, track.is_live, webpage_url,
            )
            return track

        except yt_dlp.utils.DownloadError as exc:
            logger.error("DownloadError during search '{}': {}", query, exc)
            return None
        except yt_dlp.utils.ExtractorError as exc:
            logger.error("ExtractorError during search '{}': {}", query, exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error during search '{}': {}", query, exc)
            return None

    def _sync_get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Two-stage stream URL resolution — runs in the thread executor.

        Stage 1 — Dynamic extraction (primary)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        ``extract_info(process=False)`` runs the extractor including nsig
        URL decryption but skips ``process_ie_result()``.  This means we
        receive the complete raw format list without any format-availability
        validation, so it cannot raise "Requested format is not available".
        We then select the best audio track via ``_pick_audio_url()``.

        Stage 2 — Selector fallback (secondary)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        If Stage 1 returns nothing (rare: some extractors do not populate
        ``formats[]`` without processing), try ``bestaudio/best`` then
        ``best`` as explicit format selectors.  With ``android_vr`` first in
        the player_client list, these succeed from server IPs.

        This two-stage approach means:
          • We never crash on "Requested format is not available".
          • We automatically try alternatives when one approach fails.
          • We never rely on a specific codec or container being present.
        """
        logger.debug("Resolving stream URL for '{}'", webpage_url)

        # ── Stage 1: dynamic format inspection ────────────────────────────
        url = self._dynamic_extract(webpage_url)
        if url:
            return url

        logger.debug(
            "Stage 1 returned nothing for '{}' — trying selector fallback",
            webpage_url,
        )

        # ── Stage 2: explicit selector fallback ───────────────────────────
        for fmt_selector in ("bestaudio/best", "best"):
            opts = self._build_opts({**_STREAM_BASE_OPTS, "format": fmt_selector})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(webpage_url, download=False)
                url = _pick_url_from_info(info)
                if url:
                    logger.info(
                        "Stage 2 OK via selector='{}' url_chars={} for '{}'",
                        fmt_selector, len(url), webpage_url,
                    )
                    return url

            except yt_dlp.utils.DownloadError as exc:
                exc_str = str(exc).lower()
                if any(p in exc_str for p in _FORMAT_ERR_PHRASES):
                    logger.warning(
                        "Selector '{}' unavailable for '{}' — {}",
                        fmt_selector, webpage_url, exc,
                    )
                    continue  # try next selector
                # Non-format errors (geo-block, age-gate, private, DRM)
                # will not be fixed by a different selector.
                logger.error(
                    "DownloadError (non-format) for '{}': {}", webpage_url, exc
                )
                return None

            except yt_dlp.utils.ExtractorError as exc:
                logger.error("ExtractorError for '{}': {}", webpage_url, exc)
                return None

            except Exception as exc:
                logger.error("Unexpected error for '{}': {}", webpage_url, exc)
                return None

        logger.error(
            "All extraction strategies exhausted for '{}'. "
            "Video may be DRM-protected, age-gated, or region-locked.",
            webpage_url,
        )
        return None

    def _dynamic_extract(self, webpage_url: str) -> Optional[str]:
        """
        Fetch the complete raw format list via extract_info(process=False)
        and pick the best audio URL from it.

        ``process=False`` skips ``process_ie_result()``, which is the function
        that validates format availability and raises "not available".  The
        extractor itself (including nsig decryption for signed YouTube URLs)
        still runs, so all stream URLs in formats[] are fully decrypted and
        ready to use.

        Returns None if no suitable URL is found (never raises on format
        grounds — any exception here is treated as a non-fatal Stage 1 miss).
        """
        opts = self._build_opts(_STREAM_BASE_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(webpage_url, download=False, process=False)

            if not raw:
                logger.debug("Dynamic: no raw info for '{}'", webpage_url)
                return None

            # Handle single-level redirect entries (e.g. YouTube Shorts).
            entry: Dict = raw
            if raw.get("_type") in ("url", "url_transparent") and raw.get("url"):
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        inner = ydl.extract_info(
                            raw["url"], download=False, process=False
                        )
                    if inner:
                        entry = inner
                except Exception:
                    pass  # use the original entry — inner resolution is best-effort

            formats: List[Dict] = entry.get("formats") or []

            # Compact format summary for diagnostics.
            if formats:
                sample = [
                    f"{f.get('ext','?')}/"
                    f"{'audio' if f.get('vcodec') in (None,'none') else 'video'}/"
                    f"{f.get('abr') or f.get('tbr') or '?'}kbps"
                    for f in formats[:8]
                ]
                logger.debug(
                    "Dynamic: {} formats for '{}' — sample: {}",
                    len(formats), webpage_url, ", ".join(sample),
                )

            if not formats:
                # Some extractors put a direct URL on the root entry itself.
                if (direct := entry.get("url", "")).startswith("http"):
                    logger.info(
                        "Dynamic: direct root URL (no formats[]) for '{}'",
                        webpage_url,
                    )
                    return direct
                logger.debug(
                    "Dynamic: empty formats[] and no root url for '{}'",
                    webpage_url,
                )
                return None

            url = _pick_audio_url(formats)
            if url:
                logger.info(
                    "Dynamic OK: {} formats scanned → url_chars={} for '{}'",
                    len(formats), len(url), webpage_url,
                )
            else:
                logger.warning(
                    "Dynamic: {} formats found but none yielded a usable URL for '{}'",
                    len(formats), webpage_url,
                )
            return url

        except Exception as exc:
            # process=False can still raise on network errors, deleted videos, etc.
            # Treat as non-fatal — Stage 2 will attempt the selector approach.
            logger.warning(
                "Dynamic extract exception for '{}': {}", webpage_url, exc
            )
            return None
