"""
app.infrastructure.memprobe
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
TEMPORARY diagnostic module — memory measurement for OOM root-cause analysis.

Phase 2 additions over Phase 1:
  • PSS (Proportional Set Size) from /proc/<pid>/smaps_rollup
    PSS counts each shared page once, divided by the number of processes
    that share it. It is a more accurate measure of per-process physical
    RAM cost than RSS.
  • Container (cgroup) memory from /sys/fs/cgroup/memory/
    Reads the kernel's own accounting of total container RAM use,
    independent of per-process RSS/PSS sums.
  • 1-second polling interval during resolver execution
    Previous 5-second interval missed the true Deno peak.
  • PPID and process-group (pgid) tracking for process-tree verification
  • All four metrics logged together: RSS / PSS / cgroup / private

Render uses cgroup v1 (memory.usage_in_bytes). This module reads cgroup v1
only. If Render migrates to cgroup v2, add /sys/fs/cgroup/memory.current.

CRITICAL DISTINCTIONS:
  RSS  — per-process; counts shared pages multiple times across processes
  PSS  — per-process; counts shared pages proportionally
  cgroup — container-total; kernel's own accounting; most authoritative
  Private_{Clean,Dirty} — memory truly unique to one process (not shared)

Remove this module once production PSS + cgroup measurements are collected.
IMPORTANT: No behaviour changes. Read-only /proc and /sys access only.

Stage log: [MEM]
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional, Tuple

from app.infrastructure.logger import logger


# ── Target process names ───────────────────────────────────────────────────────
_TARGETS = {"yt-dlp", "deno", "ffmpeg", "python3", "python"}

# ── Cgroup v1 paths (Render Linux) ────────────────────────────────────────────
_CG_USAGE   = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
_CG_LIMIT   = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CG_MAXUSE  = "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"
_CG_SWLIMIT = "/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes"
# Cgroup v2 paths (future-proofing, non-fatal if absent)
_CG2_CURRENT = "/sys/fs/cgroup/memory.current"
_CG2_PEAK    = "/sys/fs/cgroup/memory.peak"


# ── /proc helpers ─────────────────────────────────────────────────────────────

def _read_proc_status(pid: int) -> Optional[Dict[str, str]]:
    try:
        with open(f"/proc/{pid}/status") as f:
            lines = f.readlines()
        return {
            k.strip(): v.strip()
            for line in lines
            if ":" in line
            for k, v in [line.split(":", 1)]
        }
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _read_smaps_rollup(pid: int) -> Optional[Dict[str, int]]:
    """
    Parse /proc/<pid>/smaps_rollup for PSS and private memory fields.
    Returns None if the file is inaccessible (process exited or no permission).
    All values in kB.
    """
    try:
        result: Dict[str, int] = {}
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if ":" in line:
                    key, val = line.split(":", 1)
                    try:
                        result[key.strip()] = int(val.split()[0])
                    except (ValueError, IndexError):
                        pass
        return result or None
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _proc_info(pid: int) -> Optional[Dict]:
    """Return name, RSS, PSS, private_dirty, cmdline, ppid, pgid for a pid."""
    status = _read_proc_status(pid)
    if status is None:
        return None

    name = status.get("Name", "")
    vmrss_kb_str = status.get("VmRSS", "")
    ppid_str = status.get("PPid", "")
    try:
        rss_kb = int(vmrss_kb_str.split()[0])
    except (IndexError, ValueError):
        rss_kb = 0
    try:
        ppid = int(ppid_str)
    except ValueError:
        ppid = -1

    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read(120).replace(b"\x00", b" ").decode(errors="replace").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        cmd = ""

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = -1

    smaps = _read_smaps_rollup(pid)
    if smaps:
        pss_kb = smaps.get("Pss", 0)
        priv_dirty_kb = smaps.get("Private_Dirty", 0)
        priv_clean_kb = smaps.get("Private_Clean", 0)
        shared_clean_kb = smaps.get("Shared_Clean", 0)
        shared_dirty_kb = smaps.get("Shared_Dirty", 0)
    else:
        pss_kb = priv_dirty_kb = priv_clean_kb = 0
        shared_clean_kb = shared_dirty_kb = 0

    return {
        "pid":              pid,
        "ppid":             ppid,
        "pgid":             pgid,
        "name":             name,
        "rss_mb":           round(rss_kb / 1024, 1),
        "pss_mb":           round(pss_kb / 1024, 1) if smaps else None,
        "priv_dirty_mb":    round(priv_dirty_kb / 1024, 1) if smaps else None,
        "priv_clean_mb":    round(priv_clean_kb / 1024, 1) if smaps else None,
        "shared_clean_mb":  round(shared_clean_kb / 1024, 1) if smaps else None,
        "shared_dirty_mb":  round(shared_dirty_kb / 1024, 1) if smaps else None,
        "smaps_available":  smaps is not None,
        "cmd":              cmd[:80],
    }


# ── Cgroup helpers ─────────────────────────────────────────────────────────────

def _read_int_file(path: str) -> Optional[int]:
    try:
        val = int(open(path).read().strip())
        # Treat effectively-unlimited sentinel (> 1 PB) as None
        return val if val < 2**50 else None
    except Exception:
        return None


def _cgroup_snapshot() -> Dict[str, Optional[float]]:
    """
    Read current cgroup memory metrics.
    Returns values in MB (or None if unavailable).
    Priority: cgroup v2, fall back to cgroup v1.
    """
    def _mb(path: str) -> Optional[float]:
        v = _read_int_file(path)
        return round(v / (1024 * 1024), 1) if v is not None else None

    # Try cgroup v2 first (systemd-native, newer kernels)
    v2_current = _mb(_CG2_CURRENT)
    v2_peak    = _mb(_CG2_PEAK)

    # Try cgroup v1 (Render Linux as of 2026)
    v1_usage   = _mb(_CG_USAGE)
    v1_limit   = _mb(_CG_LIMIT)
    v1_max     = _mb(_CG_MAXUSE)
    v1_swlimit = _mb(_CG_SWLIMIT)

    return {
        "cg_current_mb":    v2_current or v1_usage,
        "cg_peak_mb":       v2_peak or v1_max,
        "cg_limit_mb":      v1_limit,
        "cg_swap_limit_mb": v1_swlimit,
        "cg_version":       2 if v2_current is not None else (1 if v1_usage is not None else None),
    }


# ── Process scan ───────────────────────────────────────────────────────────────

def _scan_processes() -> Dict[str, List[Dict]]:
    result: Dict[str, List[Dict]] = {t: [] for t in _TARGETS}
    try:
        pids = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except PermissionError:
        return result

    for pid in pids:
        info = _proc_info(pid)
        if info is None:
            continue
        name = info["name"]
        for t in _TARGETS:
            if name.startswith(t[:15]):
                result[t].append(info)
                break
    return result


def _format_proc(p: Dict) -> str:
    rss = f"{p['rss_mb']} MB RSS"
    pss = f"PSS={p['pss_mb']} MB" if p['pss_mb'] is not None else "PSS=N/A"
    priv = (
        f"private={p['priv_dirty_mb']+p['priv_clean_mb']:.1f} MB"
        if p['priv_dirty_mb'] is not None else "private=N/A"
    )
    return (
        f"    {p['name']}[{p['pid']}] ppid={p['ppid']} pgid={p['pgid']}: "
        f"{rss}  {pss}  {priv}  cmd={p['cmd']!r}"
    )


def _format_cgroup(cg: Dict) -> str:
    ver = cg.get("cg_version")
    if ver is None:
        return "  cgroup: UNAVAILABLE"
    cur  = cg.get("cg_current_mb")
    peak = cg.get("cg_peak_mb")
    lim  = cg.get("cg_limit_mb")
    swap = cg.get("cg_swap_limit_mb")
    cur_s  = f"{cur} MB"    if cur  is not None else "N/A"
    peak_s = f"{peak} MB"   if peak is not None else "N/A"
    lim_s  = f"{lim} MB"    if lim  is not None else "unlimited"
    swap_s = f"{swap} MB"   if swap is not None else "unlimited"
    return (
        f"  cgroup v{ver}: current={cur_s}  peak={peak_s}  "
        f"limit={lim_s}  swap_limit={swap_s}"
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def log_memory(label: str) -> None:
    """
    Synchronous memory snapshot: RSS + PSS + cgroup, logged at ERROR level.
    """
    scan = _scan_processes()
    cg   = _cgroup_snapshot()

    total_rss = sum(p["rss_mb"] for procs in scan.values() for p in procs)
    total_pss = sum(
        p["pss_mb"] for procs in scan.values() for p in procs
        if p["pss_mb"] is not None
    )
    pss_note = f"total_pss={round(total_pss,1)} MB" if total_pss else "total_pss=N/A"

    proc_lines = "\n".join(
        _format_proc(p)
        for procs in scan.values()
        for p in procs
    ) or "  (no matching processes)"

    logger.error(
        "[MEM][{}] total_rss={} MB  {}  self_rss={} MB\n{}\n{}",
        label,
        round(total_rss, 1),
        pss_note,
        round(_proc_info(os.getpid())["rss_mb"], 1) if _proc_info(os.getpid()) else "?",
        _format_cgroup(cg),
        proc_lines,
    )


async def poll_memory_during(
    label: str,
    interval_sec: float = 1.0,   # DEFAULT NOW 1 SECOND (was 5)
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Poll every interval_sec (default 1s for high-resolution Deno peak capture).
    Logs RSS + PSS + cgroup at each sample.
    """
    i = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break

        scan = _scan_processes()
        cg   = _cgroup_snapshot()

        total_rss = sum(p["rss_mb"] for procs in scan.values() for p in procs)
        total_pss = sum(
            p["pss_mb"] for procs in scan.values() for p in procs
            if p["pss_mb"] is not None
        )
        pss_note = f"pss={round(total_pss,1)} MB" if total_pss else "pss=N/A"

        proc_lines = "\n".join(
            _format_proc(p)
            for procs in scan.values()
            for p in procs
        ) or "  (no matching processes)"

        logger.error(
            "[MEM][{}][t+{}s] rss={} MB  {}  cg={}\n{}",
            label,
            int(i * interval_sec),
            round(total_rss, 1),
            pss_note,
            _format_cgroup(cg).strip(),
            proc_lines,
        )
        i += 1
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            break


def log_deno_cache(label: str = "STARTUP") -> None:
    """Check Deno V8 code-cache directory."""
    deno_dir  = os.path.expanduser("~/.cache/deno")
    explicit  = "/home/botuser/.cache/deno"
    lines: List[str] = []

    for d in sorted({deno_dir, explicit}):
        if not os.path.isdir(d):
            lines.append(f"    {d}: DOES NOT EXIST")
            continue
        try:
            entries = os.listdir(d)
        except PermissionError:
            lines.append(f"    {d}: PERMISSION DENIED")
            continue

        v8_caches = [e for e in entries if e.startswith("v8_code_cache")]
        gen_ok    = "gen" in entries

        if v8_caches:
            for vc in v8_caches:
                try:
                    n = sum(len(fs) for _, _, fs in os.walk(os.path.join(d, vc)))
                    lines.append(f"    {d}/{vc}: EXISTS ({n} files) — WARM Deno (deno run has run)")
                except Exception:
                    lines.append(f"    {d}/{vc}: EXISTS (count unknown) — WARM Deno")
        else:
            lines.append(f"    {d}/v8_code_cache_v*: ABSENT — COLD Deno on first invocation")

        lines.append(
            f"    {d}/gen: {'EXISTS' if gen_ok else 'ABSENT'}"
            f" — {'deno cache ran' if gen_ok else 'deno cache never ran'}"
        )

    cg = _cgroup_snapshot()
    logger.error(
        "[MEM][{}][DENO_CACHE]\n{}\n{}",
        label,
        "\n".join(lines),
        _format_cgroup(cg),
    )


def log_cgroup_only(label: str) -> None:
    """Log only the cgroup snapshot (cheap — no /proc scan)."""
    cg = _cgroup_snapshot()
    logger.error("[MEM][{}][CGROUP] {}", label, _format_cgroup(cg).strip())
