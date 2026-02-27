# Handoff Prompt: plexwatcher.py Bug Review & Patch
**Generated:** 2026-02-21  
**Project:** `/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/`  
**Repo:** `https://github.com/mm333rr/MediaScan` (branch: `main`, current tag: `v1.0.0`)  
**Runtime:** Python 3.14.3 at `/usr/local/bin/python3`  
**Daemon:** `com.mproadmin.plexwatcher` — launchd `StartInterval=300`, currently loaded and running  
**No Docker involved.** Pure macOS launchd → Python → ffmpeg/ffprobe pipeline.

---

## What This System Does

`watcher.py` is a periodic launchd daemon that automatically makes a Plex media library
fully direct-play compatible on Apple TV 4K. Every 5 minutes it:

1. Walks NFS volumes `/Volumes/tv` and `/Volumes/movies`
2. Compares each video file against a persisted index (`/Volumes/<vol>/.plexfix/manifest.json`)
3. Classifies new/changed files using `plexfix.py` shared core (via ffprobe)
4. Fixes incompatible files in-place using ffmpeg (original kept as `.bak`)
5. Updates the manifest

The shared core library `plexfix.py` handles all probe/classify/build_cmd/verify logic.

### Fix Types (all validated in safety tests)
| Type | Method |
|------|--------|
| `pgs_vobsub` | Drop image subs (PGS/VobSub), copy video+audio+text subs |
| `av1` | libx264 re-encode with `-vf format=yuv420p` (10→8 bit required) |
| `dts` | Copy video, transcode DTS/TrueHD → EAC3 448k |
| `bad_container_avi` | Remux to MP4, `-fflags +genpts`, AAC audio |
| `bad_container_ts` | Remux to MKV, copy streams |
| `bad_container_m4v` | Remux to MKV, strip data streams |
| `mjpeg` | Drop MJPEG attached_pic thumbnail, `0:v:0` only |

---

## Confirmed Bugs (from live `watcher_launchd_stderr.log`)

### Bug 1 — CRITICAL: `OSError: [Errno 5] Input/output error` on `logging.flush()`

**What's happening:**  
Every single log call (every `log.info(...)`) is throwing an `OSError: [Errno 5]`
(EIO — I/O error on stream flush). This happens because `setup_logging()` attaches a
`FileHandler` writing to `/Volumes/tv/.plexfix/watcher.log` and
`/Volumes/movies/.plexfix/watcher.log`. When launchd captures stdout via
`StandardOutPath`, the stdout stream is a pipe — **not a TTY**. When that pipe's
read end has been closed or is full, `flush()` on the `StreamHandler(sys.stdout)`
raises EIO.

Python's `logging` module catches these internally and prints "--- Logging error ---"
to stderr rather than crashing, so the script keeps running — but stderr is completely
filled with thousands of lines of logging error tracebacks, and the actual stdout log
(`watcher_launchd_stdout.log`) may be truncated or lost.

**Root cause:** The `StreamHandler(sys.stdout)` is flushing on every emit. Under
launchd pipe capture, this is unreliable when the pipe buffer fills (typically
after ~65KB) or the capturing side is slow. The file handlers writing to NFS
(`.plexfix/watcher.log`) may also stall if the NFS mount has I/O issues.

**Fix needed:**
- Wrap the `StreamHandler` emit in a try/except to silently swallow EIO on stdout  
  (launchd already captures it — a broken pipe isn't fatal)
- Add a custom `SafeStreamHandler` subclass that overrides `emit()` with EIO protection
- Or: remove the `StreamHandler(sys.stdout)` entirely and rely on the file handler +
  launchd's `StandardOutPath` redirect — but only if the NFS file handler is reliable
- Consider adding `logging.handlers.RotatingFileHandler` with explicit flush=False
  to avoid per-emit flushes on slow NFS

**Affected file:** `watcher.py`, `setup_logging()` (line ~105–115)

---

### Bug 2 — CRITICAL: `PermissionError: [Errno 13]` on `save_manifest()` when NFS not yet mounted

**What's happening:**  
```
FileNotFoundError: [Errno 2] No such file or directory: '/Volumes/tv/.plexfix'
PermissionError: [Errno 13] Permission denied: '/Volumes/tv'
```

This occurs when `/Volumes/tv` or `/Volumes/movies` exists as a directory (macOS
creates the mountpoint) but the NFS volume **is not yet mounted**. The mountpoint
directory itself is owned by root and not writable by mProAdmin. So when
`save_manifest()` tries `index_dir.mkdir(parents=True, exist_ok=True)`, it tries
to create `.plexfix` inside `/Volumes/tv` — which it can't because `/Volumes/tv`
is an unmounted root-owned stub.

The current `active_paths` guard uses `os.path.ismount(p) or os.path.isdir(p)` —
the `isdir()` fallback lets unmounted stub directories through.

**Fix needed:**
- Change the `active_paths` guard to use **only** `os.path.ismount(p)`, removing
  the `or os.path.isdir(p)` fallback
- Add a secondary sanity check: after determining `active_paths`, verify at least
  one file is readable inside the volume root before proceeding (a cheap `os.listdir(p)`
  with try/except is sufficient)
- Wrap `save_manifest()` in a try/except `PermissionError`/`OSError` so a single
  volume save failure doesn't abort the entire run — log the error and continue to
  the next volume

**Affected file:** `watcher.py`, `main()` around line ~330, and `save_manifest()` line ~132

---

### Bug 3 — MEDIUM: AVI files failing every run (never written to manifest as `failed`)

**What's happening (from stdout log):**  
The 16 AVI files (`Hercules.avi`, `Tarzan.avi`, `Snow White.avi`, etc.) appear in
`to_process` on **every single run** and fail every time:
```
Run complete: new=16  clean=0  fixed=0  failed=16  skipped=14958
```

Failed files are supposed to be retried (per `needs_processing()` logic), but a file
can only be marked as `failed` in the manifest if `save_manifest()` succeeds. Given
Bug 2, `save_manifest()` is crashing on unmounted volumes — so the manifest never
gets updated with `status: failed`, and the files are treated as **new** on every
run (never indexed), causing them to be re-classified and re-attempted every 5 minutes.

This is a cascading failure from Bug 2.

**Separately**, the AVI encode itself may be failing. Check whether ffmpeg can
actually process these specific AVI files (Disney classics from the 1930s–2000s
— they may have unusual MPEG-1/MPEG-2 video or non-standard MP3 tracks that
`-fflags +genpts -c:v copy -c:a aac` can't handle cleanly). The `ffprobe` output
for one of these files would confirm. Look at `results/watcher_launchd_stdout.log`
around the AVI entries — there should be `error:` fields in the manifest entries
if they were being saved.

**Fix needed:**
- Fix Bug 2 first — once `save_manifest()` is reliable, failed files will be
  recorded and won't re-queue every run
- Add logging of the specific ffmpeg error for each failed file directly to stdout
  (currently only goes to `watcher.log` inside `.plexfix/`, which may not be
  writable — see Bug 2)
- Investigate whether the AVI `build_cmd` needs a fallback for files whose video
  codec isn't `mpeg4` (e.g., `mpeg1video`, `mpeg2video`, `divx`) — those may need
  `-c:v libx264` rather than `-c:v copy`

---

### Bug 4 — LOW: `launchctl list` shows exit code `-` (dash) not `0`

**What's happening:**
```
-    0    com.mproadmin.plexwatcher
```
The first column is the PID (dash = not currently running, which is correct for a
`StartInterval` job between ticks). The second column `0` is the last exit code —
but given the cascading EIO errors on stdout flush, confirm the script is actually
exiting `0` and not being killed by a signal or Python exception. If the EIO errors
cause an unhandled exception to propagate, the exit code will be non-zero and
launchd will apply the `ThrottleInterval=60` penalty.

**Fix needed:** Add a top-level `try/except Exception` in `main()` with explicit
`sys.exit(1)` and error logging to a fallback path (`/tmp/plexwatcher-crash.log`)
that doesn't depend on the NFS-backed log handlers being healthy.

---

## Files to Read and Patch

```
/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/watcher.py    ← primary patch target
/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/plexfix.py    ← review only (may be fine)
/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/CHANGELOG.md  ← update after patching
```

Live logs to inspect for additional failure details:
```
/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/watcher_launchd_stderr.log
/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/watcher_launchd_stdout.log
/Volumes/tv/.plexfix/watcher.log      (if mount is up)
/Volumes/movies/.plexfix/watcher.log  (if mount is up)
```

---

## Environment & Toolchain

| Item | Value |
|------|-------|
| Python | 3.14.3 at `/usr/local/bin/python3` (Homebrew) |
| ffmpeg | `/usr/local/bin/ffmpeg` (version 8.x) |
| ffprobe | `ffprobe` (on PATH) |
| NFS mounts | `/Volumes/tv`, `/Volumes/movies` (mounted from mbuntu NAS) |
| Index dirs | `/Volumes/tv/.plexfix/`, `/Volumes/movies/.plexfix/` |
| Lock file | `/tmp/com.mproadmin.plexwatcher.lock` |
| LaunchAgent plist | `~/Library/LaunchAgents/com.mproadmin.plexwatcher.plist` |
| Log (stdout) | `.../MediaScan/results/watcher_launchd_stdout.log` |
| Log (stderr) | `.../MediaScan/results/watcher_launchd_stderr.log` |
| Mac hardware | Mac Pro 2013, AMD FirePro D700 (VideoToolbox decode-only, no HW encode) |
| CPU | Xeon E5-1680 v2, 16 threads |

**GPU note:** VideoToolbox `-c:v h264_videotoolbox` returns error -12903 on this
hardware. All video encoding must use `libx264`/`libx265` (CPU).

**NFS note:** FSEvents and `WatchPaths` are unreliable on NFS. The `StartInterval=300`
polling design is correct and intentional. Do not change to an event-based approach.

---

## Coding Standards (match existing style)

- Python 3.9+ compatible type hints (`List`, `Optional`, `Tuple` from `typing`)
- All functions have docstrings
- CLI-runnable with `argparse`, `--dry-run` flag preserved
- Logs to both stdout (launchd captures) and file handlers
- Git commit after each meaningful patch with conventional commit prefix
  (`fix:`, `refactor:`, `docs:`)
- Update `CHANGELOG.md` with a new `[1.0.1]` or `[1.1.0]` entry
- Push to `github.com/mm333rr/MediaScan`

---

## How to Test After Patching

```bash
cd "/Users/mProAdmin/Claude Scripts and Venvs/MediaScan"

# 1. Dry run — confirm no crashes, no OSError on logging
python3 watcher.py --dry-run

# 2. Check stderr is clean (should be empty or near-empty after fix)
python3 watcher.py --dry-run 2>/tmp/watcher-test-stderr.log
cat /tmp/watcher-test-stderr.log

# 3. Test mount-guard: unmount one volume and confirm script skips it cleanly
# (don't actually unmount — just verify the ismount() logic in code review)

# 4. Run once live (no --dry-run) and verify AVI files either fix or record
#    a meaningful error in manifest
python3 watcher.py --paths /Volumes/movies

# 5. Restart the launchd agent to pick up the patched script
launchctl unload ~/Library/LaunchAgents/com.mproadmin.plexwatcher.plist
launchctl load  ~/Library/LaunchAgents/com.mproadmin.plexwatcher.plist
launchctl list | grep plexwatcher

# 6. Tail live log
tail -f "/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/watcher_launchd_stdout.log"
```

---

## Summary of Changes Requested

1. **Fix `OSError: [Errno 5]` on stdout flush** — add `SafeStreamHandler` that
   catches EIO/BrokenPipe silently on the stdout handler; keep file handlers intact
2. **Fix `PermissionError` on unmounted volumes** — replace `isdir()` fallback with
   `ismount()` only; wrap `save_manifest()` in try/except with fallback logging
3. **Fix AVI infinite retry loop** — cascades from #2, but also investigate whether
   specific AVI codecs need `-c:v libx264` fallback instead of `-c:v copy`
4. **Add crash-safe top-level exception handler** — log to `/tmp/plexwatcher-crash.log`
   as fallback if NFS handlers are unavailable
5. **Update CHANGELOG.md** and commit + push

The script is doing real useful work (it's successfully fixing Digimon PGS episodes,
visible in the stderr log between all the error noise). The goal is to clean up the
error handling so runs are silent when healthy and clear when something actually fails.
