"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Factory for MediaStream objects — py-tgcalls==2.3.3 confirmed API.

Confirmed from official pytgcalls/pytgcalls examples (master branch):
----------------------------------------------------------------------

Audio-only file/URL:
    MediaStream(url, video_flags=MediaStream.Flags.IGNORE)

Audio + video quality (positional args — NOT keyword):
    MediaStream(url, AudioQuality.HIGH, VideoQuality.HD_720p)

YouTube URL with yt-dlp (library handles extraction internally):
    MediaStream(
        'https://www.youtube.com/watch?v=...',
        AudioQuality.HIGH,
        VideoQuality.HD_720p,
        ytdlp_parameters='--proxy URL',
    )

Audio-only with quality (what we use for music bots):
    MediaStream(url, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE)

WRONG (caused silent no-audio failure):
    MediaStream(url, audio_parameters=AudioQuality.HIGH, ...)  ← does NOT exist
    MediaStream(url, ffmpeg_parameters='...')                  ← does NOT exist

AudioQuality enum values (positional arg 2):
    AudioQuality.LOW    — 48 kbps
    AudioQuality.MEDIUM — 96 kbps
    AudioQuality.HIGH   — 128 kbps  ← we use this
    AudioQuality.STUDIO — 320 kbps

video_flags=MediaStream.Flags.IGNORE tells ntgcalls not to wait for a
video track. Required for audio-only URLs — without it, the stream can
hang silently waiting for a video stream that never arrives.

Two modes
---------
build_from_url(stream_url):
    For pre-resolved direct HTTPS audio URLs from yt-dlp.
    Uses video_flags=IGNORE since the URL is already audio-only.

build_from_youtube(webpage_url):
    For YouTube watch URLs (youtube.com/watch?v=...).
    Lets the library's built-in yt-dlp handle extraction.
    This is the simpler, more reliable path for YouTube.
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream

from app.core.logger import logger


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with py-tgcalls==2.3.3.

    Use build_from_youtube() for YouTube URLs — the library handles
    yt-dlp extraction internally with its own format selection.

    Use build_from_url() for pre-resolved direct HTTPS audio stream URLs
    (e.g. from a manual yt-dlp call, or non-YouTube sources).
    """

    @staticmethod
    def build_from_youtube(webpage_url: str) -> MediaStream:
        """
        Build a MediaStream from a YouTube watch URL.

        py-tgcalls 2.3.x has yt-dlp built in and handles the full
        extraction pipeline internally. Pass the YouTube URL directly —
        no pre-resolution needed.

        Parameters
        ----------
        webpage_url:
            A YouTube watch URL: https://www.youtube.com/watch?v=...
            Also accepts Shorts, playlists (first entry), and most
            YouTube URL formats that yt-dlp supports.
        """
        logger.debug("MediaStream (YouTube): {}", webpage_url)
        return MediaStream(
            webpage_url,
            AudioQuality.HIGH,          # positional arg 2 — 128 kbps stereo
            video_flags=MediaStream.Flags.IGNORE,  # audio-only; no video wait
        )

    @staticmethod
    def build_from_url(stream_url: str) -> MediaStream:
        """
        Build a MediaStream from a pre-resolved direct HTTPS audio URL.

        Use this when you have already run yt-dlp and have a direct
        CDN URL (googlevideo.com, etc.). The URL must be playable by
        FFmpeg directly — no JavaScript decryption needed.

        Parameters
        ----------
        stream_url:
            Direct HTTPS URL to an audio stream. Must not require auth
            headers or JS-level decryption.
        """
        logger.debug("MediaStream (direct URL): {}...{}", stream_url[:40], stream_url[-10:])
        return MediaStream(
            stream_url,
            AudioQuality.HIGH,          # positional arg 2 — 128 kbps stereo
            video_flags=MediaStream.Flags.IGNORE,  # audio-only; no video wait
        )

    @staticmethod
    def build(stream_url: str) -> MediaStream:
        """
        Alias for build_from_url() — kept for backward compatibility
        with existing engine.py call sites.
        """
        return FFmpegStreamBuilder.build_from_url(stream_url)
