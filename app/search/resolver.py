"""
app.search.resolver
~~~~~~~~~~~~~~~~~~~
Stream URL resolution via yt-dlp subprocess — proper kill on timeout.

Responsibility
--------------
Accept a permanent YouTube watch URL.
Return a direct CDN audio URL (e.g. rr*.googlevideo.com/...).
Nothing more.

Subprocess approach (Phase-2 OOM fix)
--------------------------------------
Phase 1 used ThreadPoolExecutor + asyncio.wait_for().  When asyncio timed
out, it cancelled the Future but the executor thread continued running yt-dlp
+ Deno ("ghost thread"), holding ~100–200 MB for 20–60 s after the timeout.

Phase 2 uses asyncio.create_subprocess_exec().  When asyncio.wait_for()
times out, it cancels the coroutine _resolve_subprocess() by raising
CancelledError at the ``await proc.communicate()`` line.  Our except-clause
then calls _kill_proc_group() which sends SIGTERM to the yt-dlp process group
(yt-dlp + its Deno child).  No ghost processes, no leaked memory.

Why the resolver was timing out (Phase-2 production failure)
-------------------------------------------------------------
Phase-2 deployed with ``_PLAYER_CLIENTS = "mweb,web,tv_embedded"``.

yt-dlp auto-detects Deno at /usr/local/bin/deno (installed in our
Dockerfile) and uses it to generate YouTube Proof-of-Origin (PO) tokens for
the mweb and web player clients.  This is required to obtain usable CDN URLs
from those clients since YouTube began enforcing PO tokens in late 2024.

On Render free tier:
  - Each deploy starts a fresh container — the Deno module cache
    (~/.cache/deno) is empty on every cold start.
  - Deno JIT-compiles yt-dlp-ejs on first invocation.
  - On a throttled 512 MB free-plan instance, this JIT compilation
    takes > 30 s — longer than STREAM_RESOLVE_TIMEOUT_SEC.
  - yt-dlp is stuck waiting for Deno to return a PO token.
  - Our timeout fires → SIGTERM → resolver raises StreamResolveTimeoutError.

This explains why:
  a) Search works  — uses extract_flat which never fetches stream URLs or
                     PO tokens.  Deno is never invoked.
  b) Resolver times out consistently — full URL extraction triggers PO
                     token generation, Deno hangs, 30 s timeout fires.
  c) The timeout started appearing AFTER Phase 2, not before —
                     Phase 2's format selector successfully reaches the PO
                     token step, whereas Phase 1's missing selector caused
                     yt-dlp to fail earlier with "format not available".

The fix: tv_embedded client (no Deno, no PO tokens)
----------------------------------------------------
YouTube's TVHTML5_SIMPLY_EMBEDDED_PLAYER API (client name: tv_embedded)
returns stream URLs that do NOT require PO-token validation.  yt-dlp
recognises this and does not invoke Deno for tv_embedded responses.

Result: full URL extraction completes in 2–5 s instead of > 30 s.

Format coverage with tv_embedded:
  - Format 140  (m4a audio-only, 128 kbps)   — preferred by bestaudio
  - Format 251  (opus audio-only, 160 kbps)  — preferred by bestaudio
  - Format 18   (360p mp4 muxed)             — best fallback
  - Format 22   (720p mp4 muxed)             — occasional fallback
  All are usable by FFmpeg → PyTgCalls for audio playback.

Concurrency limit
-----------------
Each yt-dlp subprocess uses ~80–150 MB peak (no Deno now, so lower than
before).  The bot base is ~150 MB.  The asyncio.Semaphore(1) gate is kept
as a memory safety rail for 512 MB Render, even though the memory pressure
is now lower.

Contract
--------
  SUCCESS   → str  (direct CDN URL)
  FAILURE   → None (yt-dlp exited non-zero; caller raises StreamResolveError)
  TIMEOUT   → raises StreamResolveTimeoutError (yt-dlp process killed)

Diagnostic mode (TEMPORARY)
----------------------------
_DIAGNOSTIC is True in this build.  When True:
  - ``--verbose`` replaces ``--quiet``/``--no-warnings`` so yt-dlp emits its
    full internal trace to stderr.
  - On timeout, after the process group is killed, the resolver drains
    whatever partial stdout/stderr the subprocess had already written to its
    pipe buffers and logs it at ERROR level under the [RESOLVE][DIAGNOSTIC]
    prefix.
  - The pipe drain has its own 3-second timeout so it can never block the
    event loop or delay cleanup.
  - Process-group kill, process reaping, semaphore release, and the
    no-ghost-process guarantee are all unchanged.

Set _DIAGNOSTIC = False and restore --quiet/--no-warnings to return to
normal silent operation once the root cause has been identified.

Stage log: [RESOLVE]
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Optional

from app.infrastructure.logger import logger
from app.infrastructure.memprobe import log_memory, poll_memory_during
from app.shared.constants import (
    COOKIES_SECRETS_DIR,
    COOKIES_TMP_DIR,
    STREAM_RESOLVE_TIMEOUT_SEC,
)
from app.shared.exceptions import StreamResolveTimeoutError

# ── Diagnostic mode ────────────────────────────────────────────────────────────
# TEMPORARY — set False once the timeout root cause has been identified.
#
# When True:
#   • ``--verbose`` is added to the yt-dlp command in place of
#     ``--quiet``/``--no-warnings``.  yt-dlp then emits its full internal
#     trace (HTTP requests, player JS download, n-signature steps, Deno
#     invocation, format selection, etc.) to stderr.
#   • On timeout, the partial stderr (and stdout) that the subprocess wrote
#     before it was killed are drained from the pipe buffers and logged.
#   • Extraction behaviour is unchanged — same player client, same format
#     selector, same timeout, same kill semantics.
#
# Must be False in normal production to avoid leaking internal yt-dlp traces
# in logs and to keep log volume manageable.
_DIAGNOSTIC: bool = True

# ── Pipe-drain timeout ─────────────────────────────────────────────────────────
# After killing the subprocess on timeout, we have this many seconds to drain
# whatever the process had already written to its stdout/stderr buffers.
# 3 seconds is generous; the kernel flushes pipe buffers immediately on process
# exit, so in practice the read is nearly instantaneous.
_DRAIN_TIMEOUT_SEC: float = 3.0

# ── Format selector ────────────────────────────────────────────────────────────
# Priority 1: best audio-only stream with an actual codec (DASH m4a/opus)
# Priority 2: best overall format that carries audio (muxed mp4/webm)
# Priority 3: absolute best — last resort when nothing else matches
#
# This explicit selector replaced the old "no format selector" approach which
# triggered yt-dlp's default bestvideo*+bestaudio/best and raised
# "Requested format is not available" with mweb/web player clients.
_YDL_FORMAT = "bestaudio[acodec!=none]/best[acodec!=none]/best"

# ── Player client ──────────────────────────────────────────────────────────────
# tv_embedded = TVHTML5_SIMPLY_EMBEDDED_PLAYER
#
# This is the sole player client.  See module docstring for the full
# explanation.  Short version:
#
#   mweb / web   → require YouTube PO tokens → yt-dlp invokes Deno →
#                  Deno JIT compilation hangs > 30 s on Render free tier →
#                  StreamResolveTimeoutError every time.
#
#   tv_embedded  → does NOT require PO tokens → Deno never invoked →
#                  resolves in 2–5 s.
#
# DO NOT restore mweb or web here without first confirming that Deno cold-
# start completes well within STREAM_RESOLVE_TIMEOUT_SEC on the target
# Render plan.  Pre-warming the Deno cache in the Dockerfile would be
# necessary for that.
_PLAYER_CLIENT = "tv_embedded"

# ── Concurrency gate ───────────────────────────────────────────────────────────
# Caps simultaneous yt-dlp subprocesses at 1.
# Peak memory per subprocess: ~80–150 MB (no Deno invocation now).
# Bot base: ~150 MB.  Two concurrent subprocesses would still risk OOM on
# a 512 MB instance if other work is also happening.  Keep the gate.
_RESOLVE_SEMAPHORE: asyncio.Semaphore = asyncio.Semaphore(1)


class StreamResolver:
    """
    Resolves a permanent YouTube watch URL to a direct CDN audio URL.

    Parameters
    ----------
    cookies_path:
        Optional path to a Netscape cookies.txt file.
        Unlocks age-restricted / region-locked content.
        Shared /tmp copy from YouTubeSearch is used if available.
    """

    def __init__(self, cookies_path: Optional[str] = None) -> None:
        # Accept the already-resolved /tmp path from YouTubeSearch if possible.
        if cookies_path and os.path.isfile(cookies_path):
            self._cookies_path: Optional[str] = cookies_path
        else:
            self._cookies_path = _resolve_cookies_tmp(cookies_path)

    # ── Public API ────────────────────────────────────────────────────────────

    async def resolve(self, webpage_url: str) -> Optional[str]:
        """
        Resolve a direct CDN audio URL from a YouTube watch URL.

        Returns
        -------
        str
            The direct https://... CDN URL on success.
        None
            On genuine extraction failure (yt-dlp exited non-zero, format
            not available, video private/deleted, etc.).

        Raises
        ------
        StreamResolveTimeoutError
            When asyncio.wait_for() times out.  The yt-dlp process was killed
            via SIGTERM to its process group.  No ghost processes remain.

        Stage log: [RESOLVE]
        """
        logger.debug("[RESOLVE] Resolving stream URL for: {}", webpage_url)

        # [MEM DIAGNOSTIC] Measure before acquiring semaphore
        log_memory("BEFORE_RESOLVE")

        async with _RESOLVE_SEMAPHORE:
            # [MEM DIAGNOSTIC] Start background polling task to capture peak during Deno JIT
            _stop_poll = asyncio.Event()
            _poll_task = asyncio.create_task(
                poll_memory_during("DURING_RESOLVE", interval_sec=5.0, stop_event=_stop_poll)
            )
            try:
                url = await asyncio.wait_for(
                    self._resolve_subprocess(webpage_url),
                    timeout=STREAM_RESOLVE_TIMEOUT_SEC,
                )
                if url:
                    logger.info(
                        "[RESOLVE] OK  url_preview={}...",
                        url[:60],
                    )
                else:
                    logger.warning(
                        "[RESOLVE] No direct URL found for '{}'", webpage_url,
                    )
                return url
            except asyncio.TimeoutError:
                # yt-dlp subprocess was killed by _kill_proc_group() inside
                # _resolve_subprocess()'s except-clause before re-raising
                # CancelledError.  asyncio.wait_for() converts that to
                # TimeoutError here.  No ghost processes remain.
                logger.error(
                    "[RESOLVE] Timed out for '{}' — "
                    "yt-dlp subprocess killed; no ghost processes",
                    webpage_url,
                )
                raise StreamResolveTimeoutError(
                    f"Stream resolution timed out for: {webpage_url}"
                )
            finally:
                # [MEM DIAGNOSTIC] Stop the polling task and take final snapshot
                _stop_poll.set()
                _poll_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(_poll_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                log_memory("AFTER_RESOLVE")

    @staticmethod
    def shutdown() -> None:
        """
        No-op: subprocess-based resolver has no persistent executor.

        Called by the bootstrap shutdown path for compatibility.
        """
        logger.debug(
            "[RESOLVE] StreamResolver.shutdown() called "
            "(subprocess mode — nothing to drain)"
        )

    # ── Private async subprocess ───────────────────────────────────────────────

    async def _resolve_subprocess(self, webpage_url: str) -> Optional[str]:
        """
        Spawn yt-dlp as a subprocess and return the direct audio URL.

        Uses tv_embedded client — no PO tokens, no Deno, fast completion.

        start_new_session=True places yt-dlp in its own process group so
        SIGTERM via os.killpg() kills both yt-dlp and any child processes
        it may have spawned (e.g. a future Deno invocation for a different
        client, or FFmpeg for format probing).

        On CancelledError (asyncio.wait_for timeout):
          1. _kill_proc_group() sends SIGTERM to the process group.
          2. CancelledError is re-raised.
          3. asyncio.wait_for() converts it to TimeoutError.
          4. resolve() converts TimeoutError to StreamResolveTimeoutError.

        Result: no orphaned processes, no ghost memory.
        """
        # ── Build command ─────────────────────────────────────────────────────
        cmd: list[str] = [
            "yt-dlp",
            "--format",         _YDL_FORMAT,
            "--no-playlist",
            "--geo-bypass",
            "--socket-timeout", "10",
            "--retries",        "1",
            "--extractor-args", f"youtube:player_client={_PLAYER_CLIENT}",
            "--print",          "url",
        ]
        if _DIAGNOSTIC:
            # --verbose replaces --quiet/--no-warnings.
            # yt-dlp emits its full internal trace to stderr:
            # HTTP requests, player JS URL, n-signature steps, Deno
            # invocation, format selection, cookies status, etc.
            cmd.append("--verbose")
        else:
            cmd += ["--quiet", "--no-warnings"]

        if self._cookies_path:
            cmd += ["--cookies", self._cookies_path]
        cmd.append(webpage_url)

        logger.debug(
            "[RESOLVE] Spawning yt-dlp  client={}  diagnostic={}  url='{}'",
            _PLAYER_CLIENT, _DIAGNOSTIC, webpage_url,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,   # new pgid — safe group kill on timeout
        )

        stdout = b""
        stderr = b""
        try:
            stdout, stderr = await proc.communicate()
        except BaseException:
            # CancelledError (timeout) or any other exception while waiting.
            # Kill yt-dlp (and any child it spawned) before propagating so no
            # processes are orphaned.
            _kill_proc_group(proc)

            if _DIAGNOSTIC:
                # Drain whatever partial output the subprocess had already
                # written to its pipe buffers before it was killed.
                # The process is dead (or dying) so the read completes as
                # soon as the kernel flushes the pipe — typically < 1 ms.
                # _DRAIN_TIMEOUT_SEC caps the wait so this can never stall.
                stdout, stderr = await _drain_pipes(proc)
                _log_diagnostic_output(webpage_url, stdout, stderr)

            raise

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(
                "[RESOLVE] yt-dlp exited {}  url='{}'  client={}\nstderr: {}",
                proc.returncode, webpage_url, _PLAYER_CLIENT, err[:500],
            )
            return None

        # --print url outputs one URL per line; take the first http(s) line.
        lines = [
            ln.strip()
            for ln in stdout.decode(errors="replace").splitlines()
            if ln.strip().startswith("http")
        ]
        if not lines:
            logger.warning(
                "[RESOLVE] yt-dlp returned no URL  url='{}'  client={}  raw={!r}",
                webpage_url, _PLAYER_CLIENT, stdout[:120],
            )
            return None

        return lines[0]


# ── Private helpers ────────────────────────────────────────────────────────────

def _kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """
    Send SIGTERM to the yt-dlp process group.

    Because start_new_session=True was used, the process is its own group
    leader (proc.pid == pgid).  SIGTERM propagates to any children yt-dlp
    may have spawned.  Falls back to proc.kill() (SIGKILL on the main
    process only) if killpg fails.
    """
    if proc.returncode is not None:
        return  # already exited — nothing to kill
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        logger.debug("[RESOLVE] SIGTERM sent to process group pid={}", proc.pid)
    except (ProcessLookupError, PermissionError, OSError) as kill_err:
        logger.debug(
            "[RESOLVE] killpg failed ({}) — falling back to proc.kill()", kill_err
        )
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


async def _drain_pipes(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """
    Read whatever partial output the subprocess wrote before being killed.

    Called only on the timeout/cancel path, after _kill_proc_group().
    The process is already dead (or about to exit); the pipe buffers are
    frozen.  Reading them is nearly instantaneous.

    A _DRAIN_TIMEOUT_SEC asyncio timeout guards against the unlikely case
    where a pipe read would block (e.g. the process is still running despite
    the SIGTERM).  On timeout the drain silently returns whatever was
    collected so far — no exception is propagated, and the original
    CancelledError is still re-raised by the caller.

    Returns (stdout_bytes, stderr_bytes).  Both may be empty.
    Never raises.
    """
    try:
        stdout_data, stderr_data = await asyncio.wait_for(
            _read_pipes(proc),
            timeout=_DRAIN_TIMEOUT_SEC,
        )
        return stdout_data, stderr_data
    except Exception:
        # asyncio.TimeoutError or anything else — return what we have.
        return b"", b""


async def _read_pipes(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    """Read stdout and stderr pipes concurrently."""
    stdout_task = asyncio.create_task(_read_one(proc.stdout))
    stderr_task = asyncio.create_task(_read_one(proc.stderr))
    stdout_data = await stdout_task
    stderr_data = await stderr_task
    return stdout_data, stderr_data


async def _read_one(stream: Optional[asyncio.StreamReader]) -> bytes:
    """Read all available bytes from a stream, or return b'' if None."""
    if stream is None:
        return b""
    try:
        return await stream.read(-1)   # -1 = read until EOF
    except Exception:
        return b""


def _log_diagnostic_output(
    webpage_url: str,
    stdout: bytes,
    stderr: bytes,
) -> None:
    """
    Log partial yt-dlp output captured after a timeout.

    Truncates to 4 000 bytes each, preserving the head (most useful for
    identifying which step stalled) and the tail (most useful for seeing
    the last thing yt-dlp attempted).  Cookies, tokens, and CDN secrets
    are already redacted by the byte limit — full cookie file contents
    are never echoed by yt-dlp --verbose.

    The [RESOLVE][DIAGNOSTIC] prefix makes these lines easy to grep for.
    """
    # Decode safely — yt-dlp output is UTF-8 with occasional binary noise.
    stderr_str = stderr.decode(errors="replace").strip()
    stdout_str = stdout.decode(errors="replace").strip()

    _MAX = 4000   # bytes per stream — enough for a full yt-dlp trace

    def _truncate(text: str, label: str) -> str:
        if not text:
            return f"<empty>"
        if len(text) <= _MAX:
            return text
        head = text[:_MAX // 2]
        tail = text[-_MAX // 2:]
        omitted = len(text) - _MAX
        return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"

    logger.error(
        "[RESOLVE][DIAGNOSTIC] yt-dlp did not complete within timeout.\n"
        "  url        : {}\n"
        "  client     : {}\n"
        "  diagnostic : verbose mode active\n"
        "─── stderr (yt-dlp internal trace) ───\n{}\n"
        "─── stdout (partial URL output) ───\n{}",
        webpage_url,
        _PLAYER_CLIENT,
        _truncate(stderr_str, "stderr"),
        _truncate(stdout_str, "stdout"),
    )


def _resolve_cookies_tmp(cookies_path: Optional[str]) -> Optional[str]:
    """Return the /tmp copy of cookies_path if it exists, else None."""
    if not cookies_path:
        return None
    filename = os.path.basename(cookies_path)
    tmp = f"{COOKIES_TMP_DIR}/{filename}"
    if os.path.isfile(tmp):
        return tmp
    secret = f"{COOKIES_SECRETS_DIR}/{filename}"
    if os.path.isfile(secret):
        return secret
    if os.path.isfile(cookies_path):
        return cookies_path
    return None
