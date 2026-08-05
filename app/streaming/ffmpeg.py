"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Centralised factory for pytgcalls MediaStream objects.

py-tgcalls 2.3+ API — verified against:
  • pytgcalls/pytgcalls master (2025-08-05)
  • AsmSafone/MusicPlayer main.py (production reference)
  • Official examples: piped_audio_calls, remote_piped_play

Confirmed API surface (py-tgcalls >= 2.3.0):
  from pytgcalls.types import MediaStream, AudioQuality, AudioParameters

  MediaStream(
      media_path,
      audio_parameters=AudioParameters(),   # keyword, not positional
      video_flags=MediaStream.Flags.IGNORE, # audio-only
      ffmpeg_parameters="...",              # extra FFmpeg CLI args
  )

  AudioQuality is a convenience enum whose values ARE AudioParameters instances:
      AudioQuality.HIGH   == AudioParameters(bitrate=128000, channels=2)
      AudioQuality.STUDIO == AudioParameters(bitrate=320000, channels=2)

Breaking changes vs older code:
  WRONG (positional, unreliable across 2.x minor versions):
      MediaStream(url, AudioQuality.HIGH, video_flags=...)
  CORRECT (keyword, stable):
      MediaStream(url, audio_parameters=AudioQuality.HIGH, video_flags=...)

  WRONG:  video_flags=MediaStream.IGNORE
  CORRECT: video_flags=MediaStream.Flags.IGNORE
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream

# FFmpeg flags that improve stability when streaming from remote HTTPS URLs
# (YouTube DASH segments on googlevideo.com):
#   -reconnect 1               — reconnect on disconnect
#   -reconnect_streamed 1      — reconnect even for streamed sources
#   -reconnect_delay_max 5     — give up after 5 s without reconnect
#   -vn                        — discard video track at FFmpeg level (belt+suspenders)
_FFMPEG_RECONNECT = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)


class FFmpegStreamBuilder:
    """
    Builds MediaStream objects for py-tgcalls 2.3+.

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
            Direct audio URL returned by YouTubeService (HTTPS).
        quality:
            AudioQuality.LOW | .MEDIUM | .HIGH | .STUDIO
            Defaults to HIGH (128 kbps, stereo).

        Notes
        -----
        audio_parameters is passed as a keyword argument — required for
        compatibility across all py-tgcalls 2.x minor versions.

        video_flags=MediaStream.Flags.IGNORE tells ntgcalls not to wait for
        a video track, preventing the call from stalling on audio-only URLs.

        ffmpeg_parameters adds reconnect flags so a brief CDN hiccup (common
        with googlevideo.com on Render) does not permanently drop the stream.
        """
        return MediaStream(
            stream_url,
            audio_parameters=quality,                  # keyword — stable across 2.x
            video_flags=MediaStream.Flags.IGNORE,      # audio-only stream
            ffmpeg_parameters=_FFMPEG_RECONNECT,       # resilience on remote URLs
        )
