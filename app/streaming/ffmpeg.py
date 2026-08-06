"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3 confirmed API.

Why cookies are required on Render
------------------------------------
YouTube blocks yt-dlp requests from datacenter IPs (Render, AWS, GCP)
with: "Sign in to confirm you're not a bot."

py-tgcalls MediaStream has its own internal yt-dlp. We pass cookies to it
via the ytdlp_parameters keyword argument:

    MediaStream(
        url,
        AudioQuality.HIGH,
        video_flags=MediaStream.Flags.IGNORE,
        ytdlp_parameters='--cookies /path/to/cookies.txt',
    )

Confirmed MediaStream signature (py-tgcalls==2.3.3, positional args):
    arg 1: url (str)
    arg 2: AudioQuality  (positional)
    kwarg: video_flags=MediaStream.Flags.IGNORE  (audio-only)
    kwarg: ytdlp_parameters=str  (extra yt-dlp CLI flags)
"""

from __future__ import annotations

import os
from typing import Optional

from pytgcalls.types import AudioQuality, MediaStream

from app.core.logger import logger


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with py-tgcalls==2.3.3.

    Always use build_from_youtube() — the library handles yt-dlp internally.
    Pass cookies_path so the internal yt-dlp can authenticate with YouTube.
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
            Path to Netscape-format cookies.txt.
            Required on Render/server IPs to bypass bot detection.
            If None or file missing, streams without cookies (may fail).
        """
        ytdlp_params = ""

        if cookies_path and os.path.isfile(cookies_path):
            ytdlp_params = f"--cookies {cookies_path}"
            logger.debug(
                "MediaStream (YouTube + cookies): {} cookies={}",
                webpage_url[:60], cookies_path,
            )
        else:
            logger.warning(
                "MediaStream (YouTube, NO cookies): {} — "
                "may fail on server IPs without cookies.txt",
                webpage_url[:60],
            )

        if ytdlp_params:
            return MediaStream(
                webpage_url,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                ytdlp_parameters=ytdlp_params,
            )
        else:
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
        """
        Build a MediaStream from a pre-resolved direct HTTPS audio URL.
        cookies_path unused here — direct URLs don't need yt-dlp auth.
        """
        logger.debug("MediaStream (direct URL): {}...", stream_url[:60])
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
