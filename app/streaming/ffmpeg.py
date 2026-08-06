"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3 confirmed API.

Render Secret Files location
-----------------------------
Render stores Secret Files at /etc/secrets/<filename>.
The app root also gets a symlink but /etc/secrets is authoritative.

Our cookies.txt is at: /etc/secrets/cookies.txt

We resolve in this order:
  1. /etc/secrets/<basename>   ← Render Secret Files (primary)
  2. Absolute path if provided
  3. Relative to /app (project root)
  4. Relative to cwd

MediaStream ytdlp_parameters confirmed from official pytgcalls example:
  MediaStream(url, AudioQuality.HIGH, ..., ytdlp_parameters='--proxy URL')
We use: ytdlp_parameters='--cookies /etc/secrets/cookies.txt'
"""

from __future__ import annotations

import os
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream

from app.core.logger import logger

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _resolve_cookies(cookies_path: Optional[str]) -> Optional[str]:
    """
    Resolve cookies_path to an absolute path that actually exists.

    Priority:
      1. /etc/secrets/<basename>  — Render Secret Files (always checked first)
      2. Already-absolute path
      3. Relative to /app
      4. Relative to cwd
    """
    if not cookies_path:
        return None

    filename = os.path.basename(cookies_path)

    # 1. Render Secret Files — primary location
    render_path = f"/etc/secrets/{filename}"
    if os.path.isfile(render_path):
        logger.debug("Cookies: Render Secret Files  path={}", render_path)
        return render_path

    # 2. Already absolute
    if os.path.isabs(cookies_path) and os.path.isfile(cookies_path):
        logger.debug("Cookies: absolute path  path={}", cookies_path)
        return cookies_path

    # 3. Relative to project root
    abs_path = os.path.join(_PROJECT_ROOT, cookies_path)
    if os.path.isfile(abs_path):
        logger.debug("Cookies: project root  path={}", abs_path)
        return abs_path

    # 4. Relative to cwd
    cwd_path = os.path.join(os.getcwd(), cookies_path)
    if os.path.isfile(cwd_path):
        logger.debug("Cookies: cwd  path={}", cwd_path)
        return cwd_path

    logger.warning(
        "cookies.txt not found — checked: {} | {} | {} — streaming without auth",
        render_path, abs_path, cookies_path,
    )
    return None


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with py-tgcalls==2.3.3.
    Always use build_from_youtube() for YouTube URLs.
    """

    @staticmethod
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        Build a MediaStream from a YouTube watch URL.
        Resolves cookies to absolute path and passes via ytdlp_parameters.
        """
        abs_cookies = _resolve_cookies(cookies_path)

        if abs_cookies:
            logger.debug(
                "MediaStream: YouTube + cookies  url={}  cookies={}",
                webpage_url[:60], abs_cookies,
            )
            return MediaStream(
                webpage_url,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                ytdlp_parameters=f"--cookies {abs_cookies}",
            )
        else:
            logger.warning(
                "MediaStream: NO cookies — will fail on server IPs  url={}",
                webpage_url[:60],
            )
            return MediaStream(
                webpage_url,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
            )

    @staticmethod
    def build_from_url(
        stream_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """Pre-resolved direct URL — cookies not needed."""
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
