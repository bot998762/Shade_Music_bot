"""
app.streaming.ffmpeg
~~~~~~~~~~~~~~~~~~~~
Centralised factory for pytgcalls MediaStream objects.

pytgcalls 0.9.x API reference
------------------------------
    from pytgcalls.types import MediaStream, AudioQuality, AudioParameters

    MediaStream(
        media_path,                             # direct URL or file path
        audio_parameters=AudioQuality.HIGH,     # keyword arg — stable across minor versions
        video_flags=MediaStream.Flags.IGNORE,   # audio-only; do not wait for a video track
        ffmpeg_parameters="...",                # extra FFmpeg CLI flags (prepended to input)
    )

    AudioQuality convenience enum (values are AudioParameters instances):
        AudioQuality.LOW    — 48 kbps stereo
        AudioQuality.MEDIUM — 96 kbps stereo
        AudioQuality.HIGH   — 128 kbps stereo  (production default)
        AudioQuality.STUDIO — 320 kbps stereo

FFmpeg reconnect flags
----------------------
YouTube's googlevideo.com CDN occasionally drops connections mid-stream,
especially on server/datacenter IPs (Render, AWS, etc.).  The reconnect
flags instruct FFmpeg to re-attempt the HTTP connection automatically:

    -reconnect 1              — reconnect on disconnect
    -reconnect_streamed 1     — reconnect even for live/streamed sources
    -reconnect_delay_max 5    — give up after 5 s if reconnect fails

These flags are safe for all HTTPS URLs and are ignored silently by FFmpeg
when the source is a local file.
"""

from __future__ import annotations

from pytgcalls.types import AudioQuality, MediaStream

# FFmpeg flags injected before the input URL for CDN resilience.
_FFMPEG_RECONNECT = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)


class FFmpegStreamBuilder:
    """
    Factory for MediaStream objects compatible with pytgcalls 0.9.x.

    Usage
    -----
    stream = FFmpegStreamBuilder.build(url)
    stream = FFmpegStreamBuilder.build(url, quality=AudioQuality.STUDIO)
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
            Must be a pre-signed URL that FFmpeg can open without auth headers.
        quality:
            AudioQuality.LOW | .MEDIUM | .HIGH | .STUDIO
            Defaults to HIGH (128 kbps stereo) — best balance of quality vs
            bandwidth on Render Starter.

        Notes on keyword arguments
        --------------------------
        ``audio_parameters`` is passed as a keyword argument, not positional.
        Positional argument order changed across pytgcalls minor versions;
        keyword arguments are stable.

        ``video_flags=MediaStream.Flags.IGNORE`` tells ntgcalls not to wait
        for a video stream, preventing calls from stalling on audio-only URLs.

        ``ffmpeg_parameters`` inserts reconnect flags before the input so
        brief CDN hiccups do not permanently terminate the stream.
        """
        return MediaStream(
            stream_url,
            audio_parameters=quality,              # keyword — stable across 0.9.x
            video_flags=MediaStream.Flags.IGNORE,  # audio-only; no video wait
            ffmpeg_parameters=_FFMPEG_RECONNECT,   # CDN resilience for HTTPS URLs
        )
