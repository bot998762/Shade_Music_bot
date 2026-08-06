"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3.

Primary path (post-fix)
------------------------
MusicEngine._resolve_stream() calls YouTubeService.get_stream_url() first.
That returns a direct CDN audio URL resolved in the Python yt-dlp layer
(cookies + android client applied as Python dict options — guaranteed to work).

build_from_url() receives that direct URL and creates a MediaStream that
requires zero yt-dlp processing inside ntgcalls — the URL is already a
direct https://rr*.googlevideo.com/... link.  No cookies, no extractor-args
needed at the MediaStream level.

Fallback path
-------------
If get_stream_url() fails (timeout, rate-limit, etc.) MusicEngine falls back
to build_from_youtube(webpage_url, cookies_path).  This passes ytdlp_parameters
to MediaStream which ntgcalls 2.2.5 may or may not honour, but it's better
than giving up entirely.

Cookies strategy on Render
---------------------------
Render Secret Files: /etc/secrets/cookies.txt (read-only mount)
yt-dlp needs write access for .lock files → copy to /tmp at startup.
YouTubeService.__init__ handles the copy; _resolve_cookies() here is the
fallback guard for build_from_youtube() only.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream

from app.core.logger import logger

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _resolve_cookies(cookies_path: Optional[str]) -> Optional[str]:
    """
    Resolve cookies_path to a writable absolute path for yt-dlp.
    Used only by the fallback build_from_youtube() path.

    Check order:
      1. /tmp/<basename>          — copied here by YouTubeService at startup
      2. /etc/secrets/<basename>  — Render Secret Files (read-only, copy to /tmp)
      3. Absolute path as-is
      4. Relative to project root
    """
    if not cookies_path:
        return None

    filename = os.path.basename(cookies_path)
    tmp_path = f"/tmp/{filename}"

    if os.path.isfile(tmp_path):
        logger.debug("Cookies: using /tmp copy  path={}", tmp_path)
        return tmp_path

    render_path = f"/etc/secrets/{filename}"
    if os.path.isfile(render_path):
        try:
            shutil.copy2(render_path, tmp_path)
            logger.info("Cookies: copied {} → {}", render_path, tmp_path)
            return tmp_path
        except Exception as exc:
            logger.warning("Cookies: copy failed ({}), using {} directly", exc, render_path)
            return render_path

    if os.path.isabs(cookies_path) and os.path.isfile(cookies_path):
        return cookies_path

    abs_path = os.path.join(_PROJECT_ROOT, cookies_path)
    if os.path.isfile(abs_path):
        return abs_path

    logger.warning(
        "Cookies: not found — checked /tmp/{}, /etc/secrets/{}, {} — no auth",
        filename, filename, cookies_path,
    )
    return None


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with py-tgcalls==2.3.3.
    """

    @staticmethod
    def build_from_url(
        direct_url: str,
        cookies_path: Optional[str] = None,  # unused, kept for API compat
    ) -> MediaStream:
        """
        PRIMARY PATH — Build a MediaStream from a pre-resolved direct CDN URL.

        The URL is already a direct audio stream (e.g. googlevideo.com CDN).
        ntgcalls passes it straight to FFmpeg with no yt-dlp invocation,
        so no cookies or extractor-args are needed at this level.

        cookies_path is accepted but ignored — authentication already happened
        in YouTubeService.get_stream_url() which produced the direct URL.
        """
        logger.info(
            "MediaStream: direct CDN URL  url={}...",
            direct_url[:70],
        )
        return MediaStream(
            direct_url,
            AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )

    @staticmethod
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        FALLBACK PATH — Build a MediaStream from a YouTube watch URL.

        Called when YouTubeService.get_stream_url() fails or times out.
        Passes ytdlp_parameters to MediaStream; ntgcalls 2.2.5 may or may
        not apply them (known bug: --cookies is silently ignored).
        Better than failing entirely.
        """
        abs_cookies = _resolve_cookies(cookies_path)

        # Note: double-quoted value survives shlex.split() correctly.
        # If ntgcalls fixes ytdlp_parameters handling in a future version,
        # these args will authenticate successfully.
        extractor_args = '--extractor-args youtube:player_client=android,web'

        if abs_cookies:
            ytdlp_params = f"--cookies {abs_cookies} {extractor_args}"
            logger.warning(
                "MediaStream: fallback — YouTube page URL + ytdlp_parameters  "
                "cookies={}  url={}",
                abs_cookies, webpage_url[:60],
            )
        else:
            ytdlp_params = extractor_args
            logger.warning(
                "MediaStream: fallback — YouTube page URL, no cookies  url={}",
                webpage_url[:60],
            )

        return MediaStream(
            webpage_url,
            AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
            ytdlp_parameters=ytdlp_params,
        )

    @staticmethod
    def build(
        stream_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """Alias for build_from_url() — backward compatibility."""
        return FFmpegStreamBuilder.build_from_url(stream_url, cookies_path)
