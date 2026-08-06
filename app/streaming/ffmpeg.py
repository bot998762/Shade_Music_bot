"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3 confirmed API.

Why cookies are required on Render
------------------------------------
YouTube blocks yt-dlp requests from datacenter IPs with:
"Sign in to confirm you're not a bot."

MediaStream accepts ytdlp_parameters (confirmed from official example):
    MediaStream(url, AudioQuality.HIGH, ..., ytdlp_parameters='--proxy URL')

We pass: ytdlp_parameters='--cookies /absolute/path/to/cookies.txt'

CRITICAL: Must use ABSOLUTE path. Render working dir is /app.
Relative path 'cookies.txt' fails because MediaStream's internal yt-dlp
may run from a different working directory than the app.
"""

from __future__ import annotations

import os
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream

from app.core.logger import logger

# Absolute path resolution — resolve once at import time relative to this file.
# This file is at: /app/app/streaming/ffmpeg.py
# Project root is: /app
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _resolve_cookies(cookies_path: Optional[str]) -> Optional[str]:
    """
    Resolve cookies_path to an absolute path.

    Tries in order:
    1. As-is (already absolute, e.g. /app/cookies.txt)
    2. Relative to project root (/app/cookies.txt for 'cookies.txt')
    3. Returns None if file does not exist at either location
    """
    if not cookies_path:
        return None

    # Already absolute and exists
    if os.path.isabs(cookies_path) and os.path.isfile(cookies_path):
        return cookies_path

    # Relative — resolve from project root
    abs_path = os.path.join(_PROJECT_ROOT, cookies_path)
    if os.path.isfile(abs_path):
        return abs_path

    # Try current working directory as last resort
    cwd_path = os.path.join(os.getcwd(), cookies_path)
    if os.path.isfile(cwd_path):
        return cwd_path

    logger.warning(
        "cookies.txt not found at '{}' or '{}' — streaming without cookies",
        cookies_path, abs_path,
    )
    return None


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with py-tgcalls==2.3.3.

    Always use build_from_youtube() for YouTube URLs.
    Pass cookies_path so the internal yt-dlp authenticates with YouTube.
    """

    @staticmethod
    def build_from_youtube(
        webpage_url: str,
        cookies_path: Optional[str] = None,
    ) -> MediaStream:
        """
        Build a MediaStream from a YouTube watch URL.

        Parameters
        ----------
        webpage_url:
            YouTube watch URL: https://www.youtube.com/watch?v=...
        cookies_path:
            Path to cookies.txt (relative or absolute).
            Resolved to absolute path internally — required on server IPs.
        """
        abs_cookies = _resolve_cookies(cookies_path)

        if abs_cookies:
            ytdlp_params = f"--cookies {abs_cookies}"
            logger.debug(
                "MediaStream: YouTube + cookies  url={}  cookies={}",
                webpage_url[:60], abs_cookies,
            )
            return MediaStream(
                webpage_url,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                ytdlp_parameters=ytdlp_params,
            )
        else:
            logger.warning(
                "MediaStream: YouTube WITHOUT cookies — likely to fail on server IPs  url={}",
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
        logger.debug("MediaStream: direct URL  url={}...", stream_url[:60])
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
