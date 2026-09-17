"""
app.infrastructure.memprobe
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
TEMPORARY diagnostic module — memory measurement for OOM root-cause analysis.

Measures RSS (Resident Set Size) of:
  • The Python bot process itself        (/proc/self/status)
  • Every relevant child process by name (/proc/[pid]/status + /proc/[pid]/cmdline)
    - yt-dlp subprocesses
    - deno subprocesses (children of yt-dlp)
    - ffmpeg subprocesses (children of ntgcalls)

Three measurement points in the resolver:
  BEFORE  — just before the semaphore is acquired
  PEAK    — polled periodically while the subprocess runs (best-effort async poll)
  AFTER   — immediately after resolve() returns or raises

Also samples the /proc tree for all processes matching target names so that
processes spawned by ntgcalls (not directly tracked by Python) are included.

Output is always at ERROR level so it is visible regardless of log level config,
and prefixed with [MEM] for easy grepping.

Remove this module (and its call sites in resolver.py and bootstrap/startup.py)
once actual RSS values are recorded from production.

IMPORTANT: This module contains NO behaviour changes. It does not start, stop,
kill, or modify any process. Every read is from /proc (Linux only, works on Render).
"""

from __future__ import annotations

import asyncio
import glob
import os
import time
from typing import Dict, Optional

from app.infrastructure.logger import logger


# ── Target process names to scan ──────────────────────────────────────────────
_TARGETS = {"yt-dlp", "deno", "ffmpeg", "python3", "python"}


# ── /proc helpers ─────────────────────────────────────────────────────────────

def _read_proc_status(pid: int) -> Optional[Dict[str, str]]:
    """Parse /proc/<pid>/status into a dict. Returns None if the process is gone."""
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            lines = f.readlines()
        return {
            k.strip(): v.strip()
            for line in lines
            if ":" in line
            for k, v in [line.split(":", 1)]
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _rss_kb(pid: int) -> Optional[int]:
    """Return RSS in KB for pid, or None if the process has exited."""
    status = _read_proc_status(pid)
    if status is None:
        return None
    vmrss = status.get("VmRSS", "")
    try:
        return int(vmrss.split()[0])
    except (IndexError, ValueError):
        return None


def _proc_name(pid: int) -> Optional[str]:
    """Return the process name from /proc/<pid>/status."""
    status = _read_proc_status(pid)
    if status is None:
        return None
    return status.get("Name", "")


def _proc_cmdline(pid: int) -> str:
    """Return the first 120 chars of the process cmdline (NUL-separated → space)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read(120)
        return raw.replace(b"\x00", b" ").decode(errors="replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return ""


def _all_pids() -> list[int]:
    """Return all numeric entries in /proc."""
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                pids.append(int(entry))
    except PermissionError:
        pass
    return pids


def _scan_processes() -> Dict[str, list[Dict]]:
    """
    Walk /proc and collect RSS for every process whose name matches _TARGETS.
    Returns dict: name → list of {pid, rss_mb, cmdline}.
    """
    result: Dict[str, list[Dict]] = {t: [] for t in _TARGETS}
    for pid in _all_pids():
        name = _proc_name(pid)
        if name is None:
            continue
        # Match against targets — proc name is truncated to 15 chars in Linux
        matched = None
        for t in _TARGETS:
            if name.startswith(t[:15]):
                matched = t
                break
        if matched is None:
            continue
        rss = _rss_kb(pid)
        if rss is None:
            continue
        cmd = _proc_cmdline(pid)
        result[matched].append({
            "pid": pid,
            "rss_mb": round(rss / 1024, 1),
            "cmd": cmd[:80],
        })
    return result


def _self_rss_mb() -> float:
    """Return the Python process RSS in MB."""
    rss = _rss_kb(os.getpid())
    return round((rss or 0) / 1024, 1)


def _total_relevant_mb(scan: Dict[str, list[Dict]]) -> float:
    """Sum RSS across all tracked processes."""
    total = 0.0
    for procs in scan.values():
        for p in procs:
            total += p["rss_mb"]
    return round(total, 1)


def _format_scan(scan: Dict[str, list[Dict]]) -> str:
    """Format the scan result for logging."""
    lines = []
    for name in sorted(scan):
        procs = scan[name]
        if not procs:
            continue
        for p in procs:
            lines.append(
                f"    {name}[{p['pid']}]: {p['rss_mb']} MB  "
                f"cmd={p['cmd']!r}"
            )
    return "\n".join(lines) if lines else "    (no matching processes)"


# ── Public API ─────────────────────────────────────────────────────────────────

def log_memory(label: str) -> None:
    """
    Take a synchronous memory snapshot and log it at ERROR level.

    label: short tag, e.g. "BEFORE_RESOLVE", "AFTER_RESOLVE", "STARTUP"
    """
    self_mb = _self_rss_mb()
    scan = _scan_processes()
    total = _total_relevant_mb(scan)
    # Add self to total (python3 in scan already counts self, but let's be explicit)
    proc_lines = _format_scan(scan)

    logger.error(
        "[MEM][{}] self={} MB  total_tracked={} MB\n{}",
        label,
        self_mb,
        total,
        proc_lines,
    )


async def poll_memory_during(
    label: str,
    interval_sec: float = 5.0,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Poll memory every interval_sec until stop_event is set.

    Used to measure peak RSS during a long-running operation (Deno JIT).
    Runs as a background asyncio task — does not block the resolver.
    """
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        self_mb = _self_rss_mb()
        scan = _scan_processes()
        total = _total_relevant_mb(scan)
        proc_lines = _format_scan(scan)
        logger.error(
            "[MEM][{}][t+{}s] self={} MB  total_tracked={} MB\n{}",
            label,
            int(i * interval_sec),
            self_mb,
            total,
            proc_lines,
        )
        i += 1
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            break


def log_deno_cache(label: str = "STARTUP") -> None:
    """
    Check the Deno cache directory and log its contents.

    Specifically looks for v8_code_cache_v* directories, which are written
    only by 'deno run' (not 'deno cache'). Their presence/absence determines
    whether Deno starts warm or cold.
    """
    deno_dir = os.path.expanduser("~/.cache/deno")
    # Also check the explicit DENO_DIR set by the Dockerfile pre-warm
    explicit = "/home/botuser/.cache/deno"

    lines = []
    for d in sorted({deno_dir, explicit}):
        if not os.path.isdir(d):
            lines.append(f"    {d}: DOES NOT EXIST")
            continue
        try:
            entries = os.listdir(d)
        except PermissionError:
            lines.append(f"    {d}: PERMISSION DENIED")
            continue

        lines.append(f"    {d}: exists, {len(entries)} entries: {sorted(entries)}")

        # v8_code_cache_v* is the critical indicator
        v8_caches = [e for e in entries if e.startswith("v8_code_cache")]
        gen_entries = [e for e in entries if e == "gen"]

        if v8_caches:
            for vc in v8_caches:
                vc_path = os.path.join(d, vc)
                try:
                    count = sum(len(fs) for _, _, fs in os.walk(vc_path))
                    lines.append(
                        f"    {d}/{vc}: EXISTS ({count} files) "
                        f"← deno run has run at least once — WARM DENO"
                    )
                except Exception:
                    lines.append(f"    {d}/{vc}: EXISTS (size unknown)")
        else:
            lines.append(
                f"    {d}/v8_code_cache_v*: DOES NOT EXIST "
                f"← deno run has NEVER run — COLD DENO on first invocation"
            )

        if gen_entries:
            gen_path = os.path.join(d, "gen")
            try:
                count = sum(len(fs) for _, _, fs in os.walk(gen_path))
                lines.append(
                    f"    {d}/gen: EXISTS ({count} files) "
                    f"← 'deno cache' pre-warm ran (module bytecode only)"
                )
            except Exception:
                lines.append(f"    {d}/gen: EXISTS (size unknown)")
        else:
            lines.append(
                f"    {d}/gen: DOES NOT EXIST ← 'deno cache' pre-warm also skipped"
            )

    logger.error(
        "[MEM][{}][DENO_CACHE]\n{}",
        label,
        "\n".join(lines),
    )
