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
Called when StreamResolver fails.  Passes ytdlp_parameters to MediaStream.
ntgcalls 2.2.5 may silently ignore --cookies but it is better than failing.

Stage log: [FFMPEG]
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from app.infrastructure.logger import logger
from app.shared.constants import COOKIES_SECRETS_DIR, COOKIES_TMP_DIR
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

        Stage log: [FFMPEG] primary
        """
        logger.info("[FFMPEG] primary — direct CDN URL  url={}...", direct_url[:70])
        return MediaStream(
            direct_url,
            AUDIO_QUALITY,
            video_flags=IGNORE_VIDEO,
        )

    @staticmethod
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        FALLBACK PATH — Build a MediaStream from a YouTube watch URL.

        Called when StreamResolver fails or times out.
        Passes ytdlp_parameters to MediaStream; ntgcalls 2.2.5 may or may
        not honour them (known issue: --cookies is silently ignored), but
        it is better than aborting playback entirely.

        Stage log: [FFMPEG] fallback
        """
        abs_cookies = _resolve_cookies(cookies_path)
        extractor_args = "--extractor-args youtube:player_client=android,web"

        if abs_cookies:
            ytdlp_params = f"--cookies {abs_cookies} {extractor_args}"
            logger.warning(
                "[FFMPEG] fallback — YouTube page URL + ytdlp_parameters  "
                "cookies={}  url={}",
                abs_cookies,
                webpage_url[:60],
            )
        else:
            ytdlp_params = extractor_args
            logger.warning(
                "[FFMPEG] fallback — YouTube page URL, no cookies  url={}",
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
