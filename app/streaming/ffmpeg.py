"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Centralised factory for pytgcalls MediaStream objects.

py-tgcalls 2.x API — verified against:
  • Official example: pytgcalls/pytgcalls/example/piped_audio_calls/example_piped_audio.py
  • Official example: pytgcalls/pytgcalls/example/remote_piped_play/example_remote_piped.py

Confirmed API surface (py-tgcalls >= 2.0.0):
  from pytgcalls.types import MediaStream, AudioQuality

  MediaStream(path)                                      # a/v, default quality
  MediaStream(path, AudioQuality.HIGH, VideoQuality.X)   # positional args
  MediaStream(path, video_flags=MediaStream.Flags.IGNORE) # audio-only

BREAKING CHANGE vs older drafts:
  WRONG:  video_flags=MediaStream.IGNORE
  RIGHT:  video_flags=MediaStream.Flags.IGNORE
  Source: piped_audio_calls/example_piped_audio.py (master branch, 2025-08-05)
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream


class FFmpegStreamBuilder:
    """
    Builds MediaStream objects for py-tgcalls 2.x.

    Usage
    -----
    stream = FFmpegStreamBuilder.build(url)
    stream = FFmpegStreamBuilder.build(url, quality=AudioQuality.MEDIUM)
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

        Notes
        -----
        AudioQuality is passed as the second positional argument (matching
        the official py-tgcalls examples).

        video_flags=MediaStream.Flags.IGNORE tells py-tgcalls that no video
        track will ever arrive, so it does not hold the call open waiting for
        one.  Using the old MediaStream.IGNORE (without .Flags) raises
        AttributeError at runtime.
        """
        return MediaStream(
            stream_url,
            quality,                       # positional — AudioQuality enum
            video_flags=MediaStream.Flags.IGNORE,  # audio-only stream
        )
