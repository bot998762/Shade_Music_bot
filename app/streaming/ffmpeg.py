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
and passes it to FFmpeg with reconnect flags so transient CDN TCP drops
do not abort the stream.

FFmpeg reconnect flags
----------------------
YouTube CDN (googlevideo.com) URLs are valid for ~6 hours but the underlying
TCP connection can be interrupted on Render's infrastructure (NAT rebind,
CDN load-balancer failover).  Without reconnect flags, a single TCP hiccup
causes FFmpeg to EOF immediately → ntgcalls fires StreamEnded prematurely →
the song appears to end after a few seconds or mid-track.

The three flags used:
  -reconnect 1              Enable reconnect on disconnect
  -reconnect_streamed 1     Also reconnect for streamed (non-seekable) sources
  -reconnect_delay_max 5    Cap reconnect back-off at 5 s (avoids long hangs)

These are passed via MediaStream(ffmpeg_parameters=...) which py-tgcalls 2.x
forwards to the ntgcalls FFmpeg invocation as input options (before -i).

Fallback path (build_from_youtube)
-----------------------------------
Called only when StreamResolver fails (e.g. age-restricted or geo-blocked
video where even mweb/web cannot obtain a URL without authentication).
Passes ytdlp_parameters to MediaStream so ntgcalls runs yt-dlp internally
with the same mweb,web,tv_embedded client priority as the primary path.
For age-gated or geo-blocked videos, valid cookies from a logged-in Google
account are also required.

Stage log: [FFMPEG]
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from app.infrastructure.logger import logger
from app.shared.constants import COOKIES_SECRETS_DIR, COOKIES_TMP_DIR
from app.streaming.media import AUDIO_QUALITY, IGNORE_VIDEO, MediaStream

# ── FFmpeg reconnect flags for direct CDN URLs ────────────────────────────────
# Applied to the primary path only.
# These are positional input options prepended before -i <url> by FFmpeg.
# Stable across FFmpeg 4.x and 5.x (the versions available in Debian/Ubuntu apt).
_RECONNECT_FLAGS = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)


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
        ntgcalls passes it to FFmpeg with reconnect flags so transient
        CDN TCP disconnects do not abort the stream mid-track.

        No cookies or extractor-args needed here — auth already happened in
        StreamResolver which produced the direct URL.

        Stage log: [FFMPEG] primary
        """
        logger.info("[FFMPEG] primary — direct CDN URL  url={}...", direct_url[:70])
        return MediaStream(
            direct_url,
            AUDIO_QUALITY,
            video_flags=IGNORE_VIDEO,
            ffmpeg_parameters=_RECONNECT_FLAGS,
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
