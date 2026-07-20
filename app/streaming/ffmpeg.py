"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Centralised factory for pytgcalls ``MediaStream`` objects.

All pytgcalls / FFmpeg configuration lives here so future phases can add
quality presets, video support, or custom ffmpeg_parameters in one place
without touching handlers or the engine.

pytgcalls (0.10+) uses ntgcalls internally for audio processing.
FFmpeg must be installed on the host for format conversion fallbacks.
"""

from __future__ import annotations

from pytgcalls.types import MediaStream, AudioParameters, AudioQuality


class AudioQualityPreset:
    """Named quality presets mapping to pytgcalls ``AudioQuality`` values."""
    LOW    = AudioQuality.LOW     # 48 kbps
    MEDIUM = AudioQuality.MEDIUM  # 128 kbps (default)
    HIGH   = AudioQuality.HIGH    # 256 kbps
    STUDIO = AudioQuality.STUDIO  # 320 kbps


class FFmpegStreamBuilder:
    """
    Builds ``MediaStream`` objects for pytgcalls.

    Usage
    -----
    stream = FFmpegStreamBuilder.build(url)
    stream = FFmpegStreamBuilder.build(url, quality=AudioQualityPreset.HIGH)
    """

    @staticmethod
    def build(
        stream_url: str,
        quality: AudioQuality = AudioQuality.MEDIUM,
    ) -> MediaStream:
        """
        Create a ``MediaStream`` ready to pass to pytgcalls.

        Parameters
        ----------
        stream_url:
            Direct audio URL returned by :class:`~app.services.youtube.YouTubeService`.
        quality:
            One of the ``AudioQualityPreset`` constants.  Defaults to MEDIUM
            (128 kbps) which balances quality against bandwidth on Render's
            free-tier network.
        """
        return MediaStream(
            stream_url,
            audio_parameters=AudioParameters(quality),
        )
