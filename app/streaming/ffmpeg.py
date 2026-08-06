"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3.

Cookies strategy on Render
----------------------------
Render Secret Files: /etc/secrets/cookies.txt (read-only)
yt-dlp needs write access alongside cookies for lock files.

YouTubeService.__init__ already copies:
    /etc/secrets/cookies.txt  →  /tmp/cookies.txt  (writable)

So _resolve_cookies() checks /tmp/ first — it will always find it there
after YouTubeService starts. Falls back through other locations for
non-Render environments (local dev, Docker without secrets, etc).

MediaStream ytdlp_parameters confirmed from official pytgcalls example:
    MediaStream(url, AudioQuality.HIGH, ..., ytdlp_parameters='--proxy URL')
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

    Check order:
      1. /tmp/<basename>          — already copied here by YouTubeService
      2. /etc/secrets/<basename>  — Render Secret Files (read-only, copy to /tmp)
      3. Absolute path as-is
      4. Relative to project root
    """
    if not cookies_path:
        return None

    filename = os.path.basename(cookies_path)
    tmp_path = f"/tmp/{filename}"

    # 1. Already in /tmp (YouTubeService copies it there at startup)
    if os.path.isfile(tmp_path):
        logger.debug("Cookies: using /tmp copy  path={}", tmp_path)
        return tmp_path

    # 2. Render Secret Files — copy to /tmp so yt-dlp can write lock file
    render_path = f"/etc/secrets/{filename}"
    if os.path.isfile(render_path):
        try:
            shutil.copy2(render_path, tmp_path)
            logger.info("Cookies: copied {} → {}", render_path, tmp_path)
            return tmp_path
        except Exception as exc:
            logger.warning("Cookies: copy failed ({}), using {} directly", exc, render_path)
            return render_path

    # 3. Absolute path
    if os.path.isabs(cookies_path) and os.path.isfile(cookies_path):
        return cookies_path

    # 4. Relative to project root
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
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        Build a MediaStream from a YouTube watch URL with cookies auth.
        """
        abs_cookies = _resolve_cookies(cookies_path)

        # --extractor-args "youtube:player_client=android,web"
        #   android client does NOT require PO Token on datacenter IPs.
        #   This is the primary bypass for Render/AWS/GCP server IPs.
        # --cookies bypasses bot-detection for age-gated / region-locked videos.
        # Combined, these two flags handle ~99% of YouTube videos from server IPs.
        extractor_args = '--extractor-args "youtube:player_client=android,web"'

        if abs_cookies:
            ytdlp_params = f"--cookies {abs_cookies} {extractor_args}"
            logger.info(
                "MediaStream: YouTube + cookies + android_client  cookies={}  url={}",
                abs_cookies, webpage_url[:60],
            )
        else:
            ytdlp_params = extractor_args
            logger.warning(
                "MediaStream: YouTube + android_client (no cookies)  url={}",
                webpage_url[:60],
            )

        return MediaStream(
            webpage_url,
            AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
            ytdlp_parameters=ytdlp_params,
        )

    @staticmethod
    def build_from_url(
        stream_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """Pre-resolved direct URL — no yt-dlp auth needed."""
        return MediaStream(
            stream_url,
            AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
        )

    @staticmethod
    def build(
        stream_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """Alias for build_from_url() — backward compatibility."""
        return FFmpegStreamBuilder.build_from_url(stream_url, cookies_path)
