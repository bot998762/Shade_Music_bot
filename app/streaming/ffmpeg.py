"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for pytgcalls MediaStream objects — py-tgcalls==2.3.3.

Responsibility
--------------
Build a MediaStream from either:
  a) A direct CDN audio URL  (primary path)
  b) A YouTube watch URL     (fallback path — ntgcalls runs yt-dlp internally)

No searching.  No stream URL resolution.  No voice chat joining.

Primary path (build_from_url)
------------------------------
StreamResolver resolved the CDN URL in the Python yt-dlp layer where
cookies are correctly applied.  ntgcalls receives a direct https://... URL
and passes it straight to FFmpeg — zero yt-dlp processing inside ntgcalls.

Fallback path (build_from_youtube)
-----------------------------------
Called only when StreamResolver fails (e.g. age-restricted or geo-blocked
video where even mweb/web cannot obtain a URL without authentication).
Passes ytdlp_parameters to MediaStream so ntgcalls runs yt-dlp internally
with the same mweb,web,tv_embedded client priority as the primary path.
For age-gated or geo-blocked videos, valid cookies from a logged-in Google
account are also required.

FFmpeg input flags (ffmpeg_parameters)
---------------------------------------
Both paths pass FFMPEG_INPUT_FLAGS as ffmpeg_parameters.  ntgcalls 2.2.5
prepends these before the "-i <url>" argument in its internal ffmpeg call:

    ffmpeg {ffmpeg_parameters} -i <url> -f s16le -ac 2 -ar 48000 pipe:1

The flags add HTTP reconnect handling and a larger network read buffer:

reconnect / reconnect_streamed / reconnect_delay_max
    googlevideo.com CDN connections can drop at DASH segment boundaries.
    Without reconnect flags, FFmpeg exits on the first drop; ntgcalls
    fires StreamAudioEnded; the queue advances unexpectedly — the user
    hears a sudden cut or track skip.  These flags tell FFmpeg to retry
    the HTTP connection in-place with a ≤5 s back-off.

buffer_size 8192k
    Raises the avformat network read buffer from the default 32 KB to
    8 MB, giving ~40 s of headroom at 160 kbps (the typical opus/webm
    audio-only bitrate from mweb).  On Render's shared network a momentary
    CDN slowdown can drain the 32 KB default in under 2 s, causing the
    brief stutter-without-disconnect symptom.  The larger buffer fills
    opportunistically and does not increase start latency.

Stage log: [FFMPEG]
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from app.infrastructure.logger import logger
from app.shared.constants import (
    COOKIES_SECRETS_DIR,
    COOKIES_TMP_DIR,
    FFMPEG_INPUT_FLAGS,
)
from app.streaming.media import AUDIO_QUALITY, IGNORE_VIDEO, MediaStream


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects.

    All methods are static — no state, no side effects.
    """

    @staticmethod
    def build_from_url(direct_url: str) -> MediaStream:
        """
        PRIMARY PATH — Build a MediaStream from a pre-resolved direct CDN URL.

        The URL is a direct audio stream (e.g. rr*.googlevideo.com/...).
        ntgcalls passes it straight to FFmpeg with zero yt-dlp invocation.
        No cookies or extractor-args needed here — auth already happened in
        StreamResolver which produced the direct URL.

        FFMPEG_INPUT_FLAGS are added as ffmpeg_parameters to handle CDN
        reconnects and buffer the network read to absorb transient slowdowns.

        Stage log: [FFMPEG] primary
        """
        logger.info("[FFMPEG] primary — direct CDN URL  url={}...", direct_url[:70])
        return MediaStream(
            direct_url,
            AUDIO_QUALITY,
            video_flags=IGNORE_VIDEO,
            ffmpeg_parameters=FFMPEG_INPUT_FLAGS,
        )

    @staticmethod
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        FALLBACK PATH — Build a MediaStream from a YouTube watch URL.

        Reached only when StreamResolver failed to resolve a direct CDN URL.
        Uses the same mweb→web→tv_embedded client priority as StreamResolver
        so ntgcalls' internal yt-dlp also benefits from Deno PO-token
        generation.  For age-gated or geo-blocked videos, valid cookies
        are still required; without them those videos will also fail.

        FFMPEG_INPUT_FLAGS are added as ffmpeg_parameters alongside
        ytdlp_parameters.  ntgcalls 2.2.5 accepts both independently.

        Stage log: [FFMPEG] fallback
        """
        abs_cookies = _resolve_cookies(cookies_path)
        # Matches StreamResolver client priority: mweb/web trigger Deno PO-token
        # generation automatically; tv_embedded is a last-resort fallback.
        # ios and android are omitted — they require PO tokens that yt-dlp
        # cannot auto-generate for native app clients on server IPs.
        extractor_args = "--extractor-args youtube:player_client=mweb,web,tv_embedded"

        if abs_cookies:
            ytdlp_params = f"--cookies {abs_cookies} {extractor_args}"
            logger.warning(
                "[FFMPEG] fallback — YouTube page URL  "
                "clients=mweb,web,tv_embedded  cookies={}  url={}",
                abs_cookies,
                webpage_url[:60],
            )
        else:
            ytdlp_params = extractor_args
            logger.warning(
                "[FFMPEG] fallback — YouTube page URL  "
                "clients=mweb,web,tv_embedded  no cookies  url={}",
                webpage_url[:60],
            )

        return MediaStream(
            webpage_url,
            AUDIO_QUALITY,
            video_flags=IGNORE_VIDEO,
            ytdlp_parameters=ytdlp_params,
            ffmpeg_parameters=FFMPEG_INPUT_FLAGS,
        )


# ── Private helpers ────────────────────────────────────────────────────────────

def _resolve_cookies(cookies_path: Optional[str]) -> Optional[str]:
    """
    Return a writable absolute path for the cookies file.

    Check order:
      1. /tmp/<basename>          — copied by YouTubeSearch at startup
      2. /etc/secrets/<basename>  — Render Secret Files (copy to /tmp first)
      3. Absolute path as-is
      4. Relative to cwd
    """
    if not cookies_path:
        return None

    filename = os.path.basename(cookies_path)

    tmp = f"{COOKIES_TMP_DIR}/{filename}"
    if os.path.isfile(tmp):
        return tmp

    secret = f"{COOKIES_SECRETS_DIR}/{filename}"
    if os.path.isfile(secret):
        try:
            shutil.copy2(secret, tmp)
            logger.info("[FFMPEG] Cookies: copied {} → {}", secret, tmp)
            return tmp
        except Exception as exc:
            logger.warning(
                "[FFMPEG] Cookies: copy failed ({}), using {} directly",
                exc, secret,
            )
            return secret

    if os.path.isabs(cookies_path) and os.path.isfile(cookies_path):
        return cookies_path

    abs_path = os.path.join(os.getcwd(), cookies_path)
    if os.path.isfile(abs_path):
        return abs_path

    logger.warning(
        "[FFMPEG] Cookies not found — checked /tmp/{}, /etc/secrets/{}, {} — no auth",
        filename, filename, cookies_path,
    )
    return None
