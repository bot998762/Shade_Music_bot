# SHADE MUSIC BOT — Render Standard Production Validation Procedure

**For Ak to execute. All steps are manual.**

This document contains the exact commands, log markers, and decision points
for validating the Render Standard (2 GB) deployment.

---

## Step 0: Git Push

Push the restored repository to your git remote (GitHub/GitLab):

```bash
git add -A
git commit -m "chore: restore Deno+yt-dlp-ejs, upgrade to Render Standard

- render.yaml: plan starter → plan standard (2 GB, $25/month)
- Dockerfile: restore Deno binary, wrapper (strips --no-code-cache),
  60s pre-warm. Remove nodejs.
- requirements.txt: yt-dlp[default]>=2026.07.04 (restores yt-dlp-ejs)
- resolver.py: docstrings corrected (Node.js hypothesis removed)
- memprobe.py: _TARGETS restored with deno
- All 117 tests pass"
git push origin main
```

Render will automatically redeploy when your branch updates (if auto-deploy is configured).
Or trigger manually from the Render dashboard.

---

## Step 1: Monitor the Build

In the Render dashboard → your service → Logs (Build tab):

**Expected build sequence:**
```
==> Building with Dockerfile
...
==> RUN apt-get update && apt-get install ffmpeg libssl3 ca-certificates curl unzip
...
==> RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
Archive:  /root/.deno/bin/deno
deno was installed successfully to /usr/local/bin/deno
...
==> RUN mv /usr/local/bin/deno /usr/local/bin/deno.real && printf ...
...
==> RUN pip install --no-cache-dir -r requirements.txt
...Installing yt-dlp-ejs...   ← MUST appear
...
==> RUN python3 -c "... pre-warm ..."
yt-dlp-ejs found: /usr/local/lib/python3.x/dist-packages/yt_dlp_ejs/...
pre-warm exit=0                ← 0 or 1 is acceptable; || true handles failure
```

**STOP if:** Build fails at any step. Diagnose before proceeding.

---

## Step 2: Startup Log Markers

After service starts (Runtime logs tab):

**REQUIRED lines (within first 30 seconds):**
```
[MEM][STARTUP_BASELINE]
  self_rss ≈ NNN MB
  self_pss ≈ NNN MB       ← RECORD THIS VALUE
  cgroup current ≈ NNN MB ← RECORD THIS VALUE

[MEM][STARTUP][DENO_CACHE]
  /home/botuser/.cache/deno/v8_code_cache_v*: EXISTS (N files) — WARM Deno
```
OR:
```
[MEM][STARTUP][DENO_CACHE]
  /home/botuser/.cache/deno/v8_code_cache_v*: ABSENT — COLD Deno on first invocation
```

The ABSENT case means the pre-warm didn't populate the cache. First /play will be
slower but still should succeed within 90s.

---

## Step 3: Test A — First /play

**Command:** `/play Phool by AUR` (or paste URL directly)
**Track:** https://www.youtube.com/watch?v=XsGCQUYwzVU

**Watch Runtime logs. Record EVERY [MEM] and [RESOLVE] line.**

### Required sequence:
```
[RESOLVE] start chat_id=... url=https://www.youtube.com/watch?v=XsGCQUYwzVU
[MEM][BEFORE_RESOLVE] ...
[MEM][DURING_RESOLVE][t+1s]  ...
[MEM][DURING_RESOLVE][t+2s]  ...
```

### Required Deno marker (in verbose stderr from yt-dlp):
```
[jsc:deno] Using challenge solver lib script v0.8.0
```
**If this line is ABSENT:** Deno/yt-dlp-ejs is not active.
Check that `yt-dlp-ejs` is installed: add `/usr/local/bin/python3 -m pip show yt-dlp-ejs` to lifecycle startup.

### Required success marker:
```
[RESOLVE] OK  title="Phool by AUR..."  url_preview=https://rr...googlevideo.com...  elapsed=NNs
```
**RECORD:** the `elapsed=NNs` value — this is your resolution latency.

### Required cgroup readings (from DURING_RESOLVE logs):
```
cgroup current ≈ NNN MB   ← record peak value across all t+Ns entries
cgroup peak    ≈ NNN MB   ← record this
```

### Required process list (from DURING_RESOLVE logs, shows at least once):
```
yt-dlp[PID]: rss=NNN pss=NNN   ← RECORD
deno[PID]:   rss=NNN pss=NNN   ← RECORD — this confirms Deno is running
```

### Required FFmpeg + playback:
```
[FFMPEG] starting  chat_id=...  url_preview=https://rr...
[STATE] PLAYING  chat_id=...
```
**And:** You must hear audio in the Telegram voice chat.

### PASS criteria for Test A:
- [jsc:deno] appears ✓
- [RESOLVE] OK appears ✓
- cgroup peak < 1500 MB (comfortably under 2 GB) ✓
- [STATE] PLAYING appears ✓
- Audio heard ✓

---

## Step 4: Test B — Advance with Active FFmpeg

While Test A track is playing:

**Command:** `/play` any second YouTube song (e.g. `/play Pasoori`)

Let the first song finish naturally (or if it's long, just queue and let the bot advance).

The critical overlap window is:
```
Old FFmpeg → STILL ACTIVE
yt-dlp subprocess → STARTING
Deno → STARTING
```

**Watch for:**
```
[MEM][DURING_RESOLVE][t+Ns]
  deno[PID]: rss=NNN pss=NNN
  ffmpeg[PID]: rss=NNN pss=NNN   ← BOTH must appear simultaneously
  cgroup current ≈ NNN MB         ← THIS IS THE PEAK TO RECORD
```

**Required:**
```
[RESOLVE] OK  title="..."  elapsed=NNs
[FFMPEG] replacing stream  chat_id=...
[STATE] PLAYING  chat_id=...
```
And audio from second track is heard.

### PASS criteria for Test B:
- Old FFmpeg + yt-dlp + Deno overlap observed ✓
- cgroup peak during overlap < 1500 MB ✓
- No OOM kill ✓
- Second track plays ✓
- No Render restart ✓

---

## Step 5: Test C — Multiple Active Groups (Optional, do safely)

Open a second Telegram group, join a VC, run `/play` in that group too.

Record:
- Number of active VCs
- Number of FFmpeg processes (from DURING_RESOLVE process list)
- cgroup peak during any resolve

**Stop if** cgroup approaches 1600 MB or if any instability is observed.
Do not intentionally OOM the service.

---

## Step 6: Test D — Repeated Advances (Memory Stability)

Play 5+ songs sequentially in one group. Let each one advance naturally.

After each advance, look for:
```
[MEM][AFTER_RESOLVE]
  self_pss ≈ NNN MB   ← should return near baseline (~144 MB)
  cgroup current ≈ NNN MB
```

**If Python PSS grows by >20 MB across 5 songs:** possible memory accumulation — investigate.
**If Python PSS returns to ~144 MB each time:** bounded memory confirmed.

---

## Step 7: Timeout Decision

From Test A and B logs, record:
- First resolve elapsed time (cold Deno): NNs
- Second resolve elapsed time (warm Deno): NNs
- Worst observed elapsed time: NNs

**Decision rule:**
- If worst elapsed < 25s: reduce `STREAM_RESOLVE_TIMEOUT_SEC` to 30
- If worst elapsed 25-45s: keep at 90s temporarily, or set to 60s
- If worst elapsed > 45s: investigate — something is wrong

---

## Step 8: Post-Validation Changes (only if all tests pass)

Only after collecting all evidence:

**Change 1:** `app/search/resolver.py`
```python
_DIAGNOSTIC: bool = False  # Validated on Render Standard YYYY-MM-DD; set False to reduce log volume
```

**Change 2:** `app/shared/constants.py`
```python
STREAM_RESOLVE_TIMEOUT_SEC: int = 30  # Reduced from 90 after Render Standard validation confirmed NNs typical
```
(Use the actual measured value to justify the number.)

**Change 3:** Record all measurements in `SHADE_MUSIC_BOT_PROJECT_STATE.md` (new dated entry).

---

## Log Collection Template

Fill in after validation:

```
=== RENDER STANDARD VALIDATION — [DATE] ===

Deployment:
  Build result: SUCCESS / FAIL
  Deploy time: N minutes
  Service health: HEALTHY / UNHEALTHY

Startup:
  Python idle PSS: NNN MB [MEASURED]
  Deno cache: WARM / COLD

Test A (idle → /play):
  [jsc:deno] marker: PRESENT / ABSENT
  Resolution elapsed: NNs [MEASURED]
  cgroup peak: NNN MB [MEASURED]
  yt-dlp PSS: NNN MB [MEASURED]
  Deno PSS: NNN MB [MEASURED]
  cgroup peak with Deno: NNN MB [MEASURED]
  Playback started: YES / NO
  Audio heard: YES / NO

Test B (advance with FFmpeg):
  FFmpeg + Deno overlap: OBSERVED / NOT OBSERVED
  cgroup peak during overlap: NNN MB [MEASURED]
  FFmpeg PSS: NNN MB [MEASURED]
  Second track played: YES / NO
  OOM kill: YES / NO

Test D (repeated advances, N songs):
  Python PSS after each: NNN / NNN / NNN MB
  Progressive growth: YES / NO

Timeout decision: KEEP 90s / REDUCE TO NN s
Diagnostic decision: KEEP TRUE / SET FALSE

Final status: PASS — PRODUCTION VALIDATED / FAIL
```

