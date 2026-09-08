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

Format selection fix
--------------------
The previous implementation omitted the format selector and relied on manual
parsing of info["formats"].  yt-dlp's internal default selector
(bestvideo*+bestaudio/best) raised "Requested format is not available" when
the mweb/web/tv_embedded player clients return audio-only or restricted-format
lists (confirmed in production logs).

``--format "bestaudio[acodec!=none]/best[acodec!=none]/best"`` tells yt-dlp to
select the best audio-only stream with an actual codec (DASH audio), falling
back to the best muxed format with audio, then any best format.  Combined with
``--print url``, yt-dlp outputs just the direct CDN URL — no JSON parsing.

Concurrency limit
-----------------
Each yt-dlp + Deno subprocess uses ~100–200 MB peak.  The bot base is ~150 MB.
Two concurrent subprocesses would exceed Render's 512 MB limit.  A module-level
asyncio.Semaphore(1) serialises concurrent resolve() calls — the second chat
waits rather than causing OOM.

Player client strategy
----------------------
mweb (YouTube Mobile Web) is primary.  yt-dlp automatically generates PO
tokens for mweb via Deno + yt-dlp-ejs when Deno is on PATH (installed in our
Dockerfile).  web is secondary (same PO-token path).  tv_embedded is a last-
resort fallback for edge cases.

iOS and Android are excluded: yt-dlp cannot auto-generate PO tokens for native
app clients from server IPs.

Contract
--------
  SUCCESS   → str  (direct CDN URL)
  FAILURE   → None (yt-dlp exited non-zero; caller raises StreamResolveError)
  TIMEOUT   → raises StreamResolveTimeoutError (yt-dlp process killed)

Stage log: [RESOLVE]
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Optional

from app.infrastructure.logger import logger
from app.shared.constants import (
    COOKIES_SECRETS_DIR,
    COOKIES_TMP_DIR,
    STREAM_RESOLVE_TIMEOUT_SEC,
)
from app.shared.exceptions import StreamResolveTimeoutError

# ── Format selector ────────────────────────────────────────────────────────────
# Priority 1: best audio-only stream with an actual codec (DASH opus/m4a)
# Priority 2: best overall format that carries audio (muxed mp4/webm)
# Priority 3: absolute best — last resort when nothing else matches
#
# This replaces the old "no format selector" approach that triggered yt-dlp's
# default bestvideo*+bestaudio/best selector, which raised
# "Requested format is not available" with mweb/web/tv_embedded clients.
_YDL_FORMAT = "bestaudio[acodec!=none]/best[acodec!=none]/best"

# ── Player clients ─────────────────────────────────────────────────────────────
_PLAYER_CLIENTS = "mweb,web,tv_embedded"

# ── Concurrency gate ───────────────────────────────────────────────────────────
# Limits simultaneous yt-dlp subprocesses to 1.
# Each subprocess (yt-dlp + Deno) peaks at ~100–200 MB.
# With the bot base at ~150 MB, two concurrent subprocesses exceed 512 MB.
# Callers wait inside resolve(); the second chat is not dropped, only delayed.
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

        async with _RESOLVE_SEMAPHORE:
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

        start_new_session=True places yt-dlp in its own process group so
        SIGTERM via os.killpg() kills both yt-dlp and its Deno child.

        On CancelledError (asyncio.wait_for timeout):
          1. _kill_proc_group() sends SIGTERM to the process group.
          2. CancelledError is re-raised.
          3. asyncio.wait_for() converts it to TimeoutError.
          4. resolve() converts TimeoutError to StreamResolveTimeoutError.

        Result: no orphaned yt-dlp or Deno processes, no ghost memory.
        """
        cmd: list[str] = [
            "yt-dlp",
            "--format",          _YDL_FORMAT,
            "--no-playlist",
            "--geo-bypass",
            "--socket-timeout",  "10",
            "--retries",         "1",
            "--extractor-args",  f"youtube:player_client={_PLAYER_CLIENTS}",
            "--quiet",
            "--no-warnings",
            "--print",           "url",
        ]
        if self._cookies_path:
            cmd += ["--cookies", self._cookies_path]
        cmd.append(webpage_url)

        logger.debug("[RESOLVE] Spawning yt-dlp  url='{}'", webpage_url)

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
            # Kill yt-dlp + Deno before propagating so no processes are orphaned.
            _kill_proc_group(proc)
            raise

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.error(
                "[RESOLVE] yt-dlp exited {}  url='{}'\nstderr: {}",
                proc.returncode, webpage_url, err[:500],
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
                "[RESOLVE] yt-dlp returned no URL  url='{}'  raw={!r}",
                webpage_url, stdout[:120],
            )
            return None

        return lines[0]


# ── Private helpers ────────────────────────────────────────────────────────────

def _kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """
    Send SIGTERM to the yt-dlp process group.

    Because start_new_session=True was used, the process is its own group
    leader (proc.pid == pgid).  SIGTERM propagates to Deno, which yt-dlp
    spawned as a child.  Falls back to proc.kill() (SIGKILL on the main
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
