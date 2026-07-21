"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Centralised factory for pytgcalls MediaStream objects.

py-tgcalls 2.x API notes
-------------------------
* Install package name : py-tgcalls  (NOT the old pytgcalls on PyPI)
* Import name          : from pytgcalls.types import MediaStream, AudioQuality
* Audio quality        : pass AudioQuality.<LEVEL> directly as audio_parameters
                         There is no AudioParameters wrapper class in 2.x.
* Audio-only streams   : must set video_flags=MediaStream.IGNORE so
                         pytgcalls does not wait for a video track.
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream


class FFmpegStreamBuilder:
    """
    Builds MediaStream objects for py-tgcalls 2.x.

    Usage
    -----
    stream = FFmpegStreamBuilder.build(url)
    stream = FFmpegStreamBuilder.build(url, quality=AudioQuality.HIGH)
    """

    @staticmethod
    def build(
        stream_url: str,
        quality: AudioQuality = AudioQuality.HIGH,
    ) -> MediaStream:
        """
        Create a MediaStream ready to pass to PyTgCalls.play() or
        PyTgCalls.change_stream().

        Parameters
        ----------
        stream_url:
            Direct audio URL returned by YouTubeService.
        quality:
            AudioQuality.LOW | .MEDIUM | .HIGH | .STUDIO
            Defaults to HIGH for good quality without excessive bandwidth.
        """
        return MediaStream(
            stream_url,
            audio_parameters=quality,        # AudioQuality passed directly (no wrapper)
            video_flags=MediaStream.IGNORE,  # audio-only: do not wait for video track
        )
