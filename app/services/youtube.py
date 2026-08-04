"""
app.services.youtube
~~~~~~~~~~~~~~~~~~~~
YouTube search and audio-stream extraction via yt-dlp.

Root cause of "Requested format is not available" on Render
------------------------------------------------------------
yt-dlp's default player client is ``web``.  Since ~2024, YouTube requires a
*Proof-of-Origin (PO) Token* for web-client requests that originate from
server/datacenter IPs (Render, AWS, GCP, etc.).  Without that token the
format list returned by YouTube is empty — every selector, including "best",
raises "Requested format is not available".  Switching to the ``android_vr``
client bypasses the PO Token requirement entirely.

This is configured in ``_BASE_OPTS`` via::

    "extractor_args": {
        "youtube": {"player_client": ["android_vr", "ios", "web"]}
    }

Stream-URL extraction strategy
--------------------------------
Stage 1 — Dynamic (primary)
    Call ``extract_info(..., process=False)`` to obtain the raw format list
    with all streaming URLs already decrypted by the extractor.  ``process=False``
    skips ``process_ie_result()`` — the step that runs format *selection* and
    raises the "not available" error — so it can never fail on format grounds.
    We then pick the best audio track ourselves via ``_pick_audio_url()``.

Stage 2 — Selector fallback
    If Stage 1 returns nothing (edge case: some extractors don't populate
    ``formats[]`` in unprocessed mode), try ``bestaudio/best`` then ``best``
    as explicit selectors.  With ``android_vr`` in ``_BASE_OPTS`` these now
    resolve reliably even without cookies.

Search
------
``extract_flat="in_playlist"`` prevents yt-dlp from resolving formats during
search (we only need metadata).  Without this, yt-dlp validates every result's
format list and fails for videos with unusual codecs (iamf, AV1-only, etc.).
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

# Bounded executor — 3 threads is enough; yt-dlp calls are I/O-bound.
_YT_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdlp")

# Substrings that identify a yt-dlp format-availability error.
# We use these only in Stage 2 to decide whether to try the next selector.
_FORMAT_ERR_PHRASES = (
    "requested format is not available",
    "no video formats found",
    "format is not available",
    "no formats",
)

# ── HTTP headers ──────────────────────────────────────────────────────────────
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

# ── Base options (applied to every yt-dlp call) ───────────────────────────────
#
# player_client priority:
#   android_vr — no PO Token required, full DASH format list, most reliable
#   ios        — no PO Token required, good fallback
#   web        — requires PO Token on server IPs; last resort (works with cookies)
_BASE_OPTS: Dict = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "socket_timeout": 15,
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
# Overrides _BASE_OPTS which has no extract_flat key (defaults to False).
_SEARCH_OPTS: Dict = {
    **_BASE_OPTS,
    "extract_flat": "in_playlist",
    "default_search": "ytsearch",
    "skip_download": True,
}

# ── Stream-resolution base options ────────────────────────────────────────────
# No "format" key — Stage 1 bypasses selection entirely; Stage 2 injects it
# per-attempt so we never re-use the same selector.
_STREAM_BASE_OPTS: Dict = {
    **_BASE_OPTS,
    "skip_download": True,
}


# ── Module-level helpers (no class state) ─────────────────────────────────────

def _pick_audio_url(formats: List[Dict]) -> Optional[str]:
    """
    Choose the best audio stream URL from a raw yt-dlp formats list.

    Never hard-codes a container name or codec — works on whatever the
    extractor actually returns.

    Priority
    --------
    1. Audio-only streams (vcodec = none), highest bitrate first.
    2. Combined a+v streams with audio, lowest resolution first
       (minimises unnecessary video data sent to FFmpeg).
    3. Any HTTP/HTTPS URL as absolute last resort.

    Skips manifests (m3u8, mpd) and non-HTTP protocols (rtmp, mms)
    because they require special demuxer setup; FFmpeg can handle a plain
    HTTPS DASH segment URL directly.
    """
    if not formats:
        return None

    def _is_direct(f: Dict) -> bool:
        url = f.get("url", "")
        proto = f.get("protocol", "https")
        return url.startswith("http") and proto not in (
            "m3u8", "m3u8_native", "rtmp", "rtmpe", "mms",
        )

    # Allow m3u8 only as a last resort (FFmpeg handles HLS natively)
    direct = [f for f in formats if _is_direct(f)]
    if not direct:
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
            "pick: audio-only ext={} abr={}kbps acodec={}",
            best.get("ext", "?"),
            best.get("abr", "?"),
            best.get("acodec", "?"),
        )
        return best["url"]

    # ── Priority 2: combined a+v with an audio track ──────────────────────
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
            "pick: combined a+v ext={} height={}",
            best.get("ext", "?"),
            best.get("height", "?"),
        )
        return best["url"]

    # ── Priority 3: any format with a URL ─────────────────────────────────
    for fmt in reversed(direct):
        if fmt.get("url"):
            logger.debug("pick: last-resort ext={}", fmt.get("ext", "?"))
            return fmt["url"]

    return None


def _pick_url_from_info(info: Optional[Dict]) -> Optional[str]:
    """
    Extract a playable URL from a fully-processed yt-dlp info dict.

    When yt-dlp ran format selection it puts the chosen URL directly in
    ``info["url"]``.  Falls back to scanning ``info["formats"]`` when that
    key is absent.
    """
    if not info:
        return None
    if url := info.get("url"):
        return url
    return _pick_audio_url(info.get("formats") or [])


# ── Service class ─────────────────────────────────────────────────────────────

class YouTubeService:
    """
    Async YouTube search and stream-URL extraction backed by yt-dlp.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape-format cookies.txt.  Used automatically
        when the file exists; the service works without it for public videos.
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

        Returns ``None`` on failure.  The Track carries only metadata and a
        permanent ``webpage_url``; stream URL is resolved separately, just
        before playback.
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

        Always call this immediately before playback — the URL expires quickly.
        Returns ``None`` on failure.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _YT_EXECUTOR,
            self._sync_get_stream_url,
            webpage_url,
        )

    # ── Private sync workers ──────────────────────────────────────────────────

    def _build_opts(self, base: Dict) -> Dict:
        """Merge *base* options with cookies when available."""
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
        opts = self._build_opts(_SEARCH_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            if not info:
                logger.warning("Search: no info for '{}'", query)
                return None

            entries = info.get("entries")
            if not entries:
                logger.warning("Search: no entries for '{}'", query)
                return None

            entry = entries[0]

            # With extract_flat="in_playlist", webpage_url is absent.
            # "url" holds the watch URL; fall back to constructing from "id".
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
                logger.warning("Search: could not determine webpage_url for '{}'", query)
                return None

            # Thumbnail: scalar field or last item in thumbnails list
            thumbnail: Optional[str] = entry.get("thumbnail")
            if thumbnail is None:
                thumbs: list = entry.get("thumbnails") or []
                if thumbs:
                    thumbnail = thumbs[-1].get("url")

            track = Track(
                title=entry.get("title") or "Unknown Title",
                duration=int(entry.get("duration") or 0),
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
                "Search OK: '{}' → title='{}' duration={}s url='{}'",
                query, track.title, track.duration, webpage_url,
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

    # ── Stream URL resolution ─────────────────────────────────────────────────

    def _sync_get_stream_url(self, webpage_url: str) -> Optional[str]:
        """
        Two-stage stream URL resolution.

        Stage 1 — Dynamic (primary)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~
        ``extract_info(process=False)`` runs the YouTube extractor (including
        nsig URL decryption) but skips ``process_ie_result()`` — the function
        that validates format availability and raises "not available".  We
        receive the full raw format list and pick the best audio track with
        ``_pick_audio_url()``, which inspects actual format properties (vcodec,
        acodec, abr) without assuming any specific container or codec exists.

        Stage 2 — Selector fallback
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~
        If Stage 1 yields nothing (rare: some extractors don't populate
        ``formats[]`` without processing), try ``bestaudio/best`` then ``best``
        as explicit selectors.  With ``player_client=["android_vr"]`` in
        ``_BASE_OPTS``, these selectors now resolve successfully from server IPs.
        Unlike the old 3-selector chain, each selector here is genuinely
        different and only tried once.
        """
        logger.debug("Resolving stream URL for '{}'", webpage_url)

        # ── Stage 1: dynamic format inspection ────────────────────────────
        url = self._dynamic_extract(webpage_url)
        if url:
            return url

        # ── Stage 2: explicit selector fallback ───────────────────────────
        for fmt in ("bestaudio/best", "best"):
            opts = self._build_opts({**_STREAM_BASE_OPTS, "format": fmt})
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(webpage_url, download=False)
                url = _pick_url_from_info(info)
                if url:
                    logger.info(
                        "Stream OK via selector='{}' url_chars={} for '{}'",
                        fmt, len(url), webpage_url,
                    )
                    return url

            except yt_dlp.utils.DownloadError as exc:
                if any(p in str(exc).lower() for p in _FORMAT_ERR_PHRASES):
                    logger.warning(
                        "Selector '{}' unavailable for '{}' — {}",
                        fmt, webpage_url, exc,
                    )
                    continue  # try next selector
                # Non-format errors (geo-block, age-gate, private video)
                # won't be fixed by a different selector.
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
        Fetch the complete raw format list and pick the best audio URL.

        Uses ``extract_info(process=False)`` which runs the extractor
        (including signature/nsig decryption) but skips format selection.
        The returned ``formats[]`` list contains decrypted streaming URLs for
        every rendition YouTube exposes via the ``android_vr`` client — DASH
        audio tracks, combined mp4, etc. — without any filtering.

        We then score and select from those formats dynamically, never
        assuming that m4a, webm, or any other specific container is present.
        """
        opts = self._build_opts(_STREAM_BASE_OPTS)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                raw = ydl.extract_info(webpage_url, download=False, process=False)

            if not raw:
                logger.debug("Dynamic: no raw info for '{}'", webpage_url)
                return None

            # Resolve single-level redirect entries (url / url_transparent).
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
                    pass  # use the original entry

            formats: List[Dict] = entry.get("formats") or []

            # Log a compact summary so format issues are diagnosable in Render logs.
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
                # Some extractors put a direct URL on the root entry.
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
            # process=False can still raise if the extractor itself fails
            # (network error, video deleted, etc.).  Treat as non-fatal here;
            # Stage 2 will attempt the selector approach.
            logger.warning(
                "Dynamic extract exception for '{}': {}", webpage_url, exc
            )
            return None
