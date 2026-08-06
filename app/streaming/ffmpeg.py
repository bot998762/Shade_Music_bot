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
    Resolve cookies_path to a WRITABLE absolute path for yt-dlp.

    /etc/secrets is read-only on Render — yt-dlp tries to write a lock file
    next to cookies.txt and raises [Errno 30] Read-only file system.
    We always copy to /tmp/<filename> which is writable.

    Lookup order for source file:
      1. /etc/secrets/<basename>  — Render Secret Files
      2. Already-absolute path
      3. Relative to project root
      4. Relative to cwd
    """
    if not cookies_path:
        return None

    import shutil
    filename = os.path.basename(cookies_path)
    tmp_path = f"/tmp/{filename}"

    # Find the source file
    candidates = [
        f"/etc/secrets/{filename}",
        cookies_path if os.path.isabs(cookies_path) else "",
        os.path.join(_PROJECT_ROOT, cookies_path),
        os.path.join(os.getcwd(), cookies_path),
    ]
    source: Optional[str] = None
    for c in candidates:
        if c and os.path.isfile(c):
            source = c
            break

    if not source:
        logger.warning(
            "cookies.txt not found — checked: {} — streaming without auth",
            ", ".join(c for c in candidates if c),
        )
        return None

    # Copy to /tmp so yt-dlp can write its lock file alongside it
    try:
        shutil.copy2(source, tmp_path)
        logger.debug("Cookies: copied {} → {}", source, tmp_path)
        return tmp_path
    except Exception as exc:
        # If copy fails (e.g. /tmp full), fall back to source — may still work
        logger.warning("Cookies: copy to /tmp failed ({}), using {} directly", exc, source)
        return source


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
