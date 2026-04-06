# Changelog

All notable changes to plex-batch-optimizer are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).  
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.2] — 2026-04-05

### Fixed

**Bug 1 — CRITICAL: No post-reboot cooldown caused immediate CPU spike after every reboot**
- Root cause: `RunAtLoad=true` in the launchd plist caused watcher to fire within
  seconds of boot, launching ffmpeg before Plex, NFS mounts, and WindowServer had
  stabilised. This was the primary cause of the overnight CPU panic storm (38 events,
  2 reboots between Apr 1–5 2026).
- Added `boot_age_secs()` function that reads `kern.boottime` via sysctl to determine
  seconds elapsed since last boot.
- Added `--boot-grace N` CLI argument (default 600 seconds). On invocation, if the
  system has been up less than `boot_grace` seconds, the watcher logs the remaining
  wait time and exits cleanly. launchd retries at the next `StartInterval` tick (300s).
- Added `import re` (required by `boot_age_secs` sysctl output parser).
- Added three module-level constants: `BOOT_GRACE_DEFAULT=600`,
  `MAX_JOBS_PER_RUN_DEFAULT=1`, `INTER_JOB_SLEEP_DEFAULT=30`.
- **Affected:** `watcher.py` — docstring, imports, constants, new `boot_age_secs()`,
  new argparse args, boot-grace block in `main()`.

**Bug 2 — HIGH: No ffmpeg job cap — watcher encoded files back-to-back indefinitely**
- Root cause: A single watcher run would process every file in `to_process`
  sequentially with no pause between jobs. The run-lock (`fcntl.LOCK_EX`) prevented
  concurrent watcher invocations, but one run could encode for hours, sustaining
  >90% CPU. plex-guardian could SIGSTOP individual ffmpeg PIDs but the watcher
  immediately spawned a new one on the next file.
- Added `--max-jobs-per-run N` (default 1). Watcher processes at most N files per
  5-minute tick. Remaining files are deferred and logged. The launchd StartInterval
  naturally throttles the overall encode rate to at most 12 jobs/hour.
- Added `--inter-job-sleep S` (default 30 seconds). Watcher sleeps S seconds between
  encode jobs, giving plex-guardian and Plex breathing room before the next CPU spike.
- Only real encode attempts (`fixed` or `failed` status) count toward the job cap.
  `clean`, `dry-run`, and `indexed` files are not counted.
- **Affected:** `watcher.py` — new argparse args, `jobs_this_run` counter and cap/sleep
  block in the process loop.

### Changed
- `com.mproadmin.plexwatcher.plist` — Added `--boot-grace 600`,
  `--max-jobs-per-run 1`, `--inter-job-sleep 30` to `ProgramArguments`.

---

## [1.0.1] — 2026-02-21

### Fixed

**Bug 1 — CRITICAL: `OSError: [Errno 5] Input/output error` flooding stderr on every log call**
- Added `SafeStreamHandler` subclass that overrides `emit()` to silently swallow
  `errno.EIO`, `errno.EPIPE`, and `errno.EBADF` on the stdout pipe.
- Under launchd `StandardOutPath` capture, the stdout pipe buffer fills (~65 KB)
  and subsequent `flush()` calls raise EIO. The stock `StreamHandler` printed a
  full traceback to stderr on every single log record — filling
  `watcher_launchd_stderr.log` with thousands of lines of noise per run.
- The `SafeStreamHandler` discards these benign pipe errors silently. NFS file
  handlers are unaffected and continue to receive every record.
- `setup_logging()` now also wraps `FileHandler` creation in try/except so a
  missing or unmounted index dir doesn't abort logging setup.
- **Affected:** `watcher.py` — `setup_logging()`, new `SafeStreamHandler` class.

**Bug 2 — CRITICAL: `PermissionError: [Errno 13]` when NFS volume not mounted**
- Removed `or os.path.isdir(p)` fallback from the `active_paths` mount guard.
  An unmounted NFS mountpoint is a root-owned stub directory that passes
  `isdir()` but fails any write attempt with `PermissionError`. Now uses
  `os.path.ismount(p)` exclusively.
- Added secondary readability check: `os.listdir(p)` in a try/except confirms
  the volume is actually accessible before proceeding.
- `save_manifest()` now wraps all I/O in try/except and returns `bool` (True =
  saved, False = failed). Callers log a warning on failure and continue —
  a single volume save failure no longer aborts the entire run.
- **Affected:** `watcher.py` — `save_manifest()`, `main()` mount guard.

**Bug 3 — MEDIUM: AVI files re-queued on every run (infinite retry loop)**
- Root cause was Bug 2: `save_manifest()` was crashing before writing `failed`
  status, so AVI files were never indexed and re-classified as `new` every run.
  Fixed by Bug 2 patch above.
- Additionally fixed the underlying AVI encode failure: `build_cmd()` was using
  `-c:v copy` unconditionally, but Disney classic AVIs use `mpeg1video`,
  `mpeg2video`, and other codecs that cannot be stream-copied into MP4.
  Now inspects `probe_data` to determine the source video codec: copy-safe
  codecs (`mpeg4`, `h264`, `hevc`) still use `-c:v copy`; all others
  fall back to `-c:v libx264 -preset fast -crf 20` re-encode.
- `process_file()` now logs ffmpeg errors directly to stdout (via `log.warning`)
  so failures are visible in `watcher_launchd_stdout.log` rather than only in
  the NFS-backed `watcher.log` that may be unwritable.
- **Affected:** `plexfix.py` — `build_cmd()` AVI branch; `watcher.py` — `process_file()`.

**Bug 4 — LOW: No crash-safe fallback for unhandled exceptions**
- Added top-level `try/except Exception` around `main()` in `__main__` block.
- On uncaught exception: logs full traceback to `/tmp/plexwatcher-crash.log`
  (always writable, independent of NFS) and to stderr (captured by launchd),
  calls `release_lock()`, then exits with code 1.
- **Affected:** `watcher.py` — `__main__` block.



## [0.9.0] — 2026-02-17

**Status:** Feature-complete, safety-tested, snag-scanned. Batch run in progress (first full pass). Version 1.0.0 will be tagged after the batch completes successfully and `.bak` cleanup is verified.

### Added
- `media_scan.py` — Full library scanner using parallel ffprobe workers. Classifies 14,379 video files into: `direct_play`, `transcode_required`, `junk`, `sample`. Outputs structured JSON with codec, container, resolution, bitrate, subtitle stream details for every file.
- `safety_test.py` — One-file-per-type validation harness. Runs 7 jobs (T1–T7) covering all problem types, verifies each output is ATV4K direct-play compatible. All 7 pass: T1 PGS strip, T2 VobSub strip, T3 AV1→H264, T4 DTS→EAC3, T5 AVI→MP4, T6 MJPEG strip, T7 TS→MKV.
- `batch_optimize.py` — Production batch processor for all 2,844 problem files. Three parallel worker pools: I/O-only (12 workers), CPU-light/DTS (6 workers), CPU-heavy/AV1 (2 workers × 12 threads each). Resume-safe via `batch_completed.json`. In-place replacement with `.bak` originals.
- `results/scan_results.json` — Complete library classification data (14,379 files).
- `results/safety_test_result.json` — Safety test pass/fail record per job.
- `results/snag_report.json` — Output of deep snag scan (30 files sampled per type, 150 total live ffprobe calls).

### Fixed (discovered by snag scan before batch run)
- **DTS + PGS co-presence (97% of DTS files):** Original command used `-map 0:s? -c:s copy`, which passed PGS image subtitle streams into output, causing verifier rejection. Fixed by probe-based explicit index mapping in `build_cmd`.
- **MJPEG + PGS co-presence (17% of MJPEG files):** Same root cause and fix.
- **AV1 + PGS co-presence (~40% of AV1 files):** Broken negative map `-map -0:s:m:codec_name:hdmv_pgs_subtitle` looked correct in docs but `:m:` filters user metadata *tags*, not codec properties. Fixed by `text_sub_maps(probe_data)`.
- **Mixed ASS + PGS files (Boruto, Naruto, Demon Slayer series):** Original `pgs_vobsub` command dropped *all* subtitle streams (no `-map 0:s?`). Text subs (ASS/SRT) were being silently discarded. Fixed: now preserves text subs via explicit index mapping, drops only image subs.
- **AV1 10-bit encode failure (all AV1 files):** Missing `-vf format=yuv420p` caused ffmpeg to produce 0-byte output and exit 187 when source is `yuv420p10le`. Fix confirmed in safety test T3 and Primal S02E03 re-test.

### Technical Findings
- **VideoToolbox on AMD FirePro D700:** Decode-only. No H.264/HEVC hardware encode capability. Error -12903 on `h264_videotoolbox`. All encodes fall back to `libx264`/`libx265` (CPU). 16-thread Xeon E5-1680 v2 handles this well (~37 fps on 1080p AV1 decode + libx264 encode).
- **AVI + VBR MP3 timestamp issues:** `-fflags +genpts` required before remux. Without it, muxer rejects stream with "Can't write packet with unknown timestamp".
- **`ffmpeg -map -0:s:m:codec_name:X` doesn't work** for codec filtering — documented as a known limitation in stream specifier parsing. Only user-set metadata tags are matched by `:m:`.

### Library Audit Results
- 14,379 total video files / 14.5 TB
- 9,386 already ATV4K direct-play (65%)
- 2,844 require fix (20%)
- 2,299 thin bitrate <1.5 Mbps 1080p (quality issue, not a streaming issue)
- 0 files exceed 600 Mbps — entire library streams without buffering
- ~200 GB in exact duplicate files identified (safe to delete)

---

## [Unreleased] — planned for v1.0.0

- Confirm batch_optimize.py completed all 2,844 files with 0 unexpected failures
- `.bak` cleanup script with Plex playback verification step
- Duplicate detection and safe-delete script for ~200 GB of identified dupes
- Plex library refresh trigger via API after batch completes

---

## [0.9.5] — 2026-02-17

### Added
- `plexfix.py` — Shared core library extracted from batch_optimize.py. Provides `probe()`, `classify_probe()`, `classify_scan_record()`, `build_cmd()`, `verify()`, `text_sub_maps()`, `attachment_maps()`, `output_ext()`. Single source of truth for both batch and watcher.
- `watcher.py` — Periodic auto-fix daemon for NFS-mounted Plex library. Runs via launchd `StartInterval=300` (every 5 min). On each run: walks volumes, compares against persisted index, classifies and fixes only new/changed files, updates `.plexfix/manifest.json` at each volume root.
- `com.mproadmin.plexwatcher.plist` — launchd user agent definition. RunAtLoad=true, StartInterval=300, ThrottleInterval=60. Logs to results/watcher_launchd_stdout.log.
- `install_watcher.sh` — One-command installer: `./install_watcher.sh [install|uninstall|restart|status|run-now]`.

### Architecture Decisions
- **NFS volumes require polling.** FSEvents and launchd `WatchPaths` only work on local APFS/HFS+ volumes. `/Volumes/tv` and `/Volumes/movies` are NFS mounts — launchd `StartInterval` is the macOS-native solution.
- **Index lives in `.plexfix/` at volume root.** Dot-prefix hides it from Plex metadata scan. Contains `manifest.json` (mtime+size fingerprints for every video file), `watcher.log` (rolling, capped at 2000 lines), `errors.json`.
- **`--build-index` fast-path.** First run on an existing library uses stat() only (no ffprobe) to populate the manifest in ~5 seconds per volume. Subsequent runs probe only new/changed files.
- **Settle delay (default 60s).** Files modified within the last 60 seconds are skipped — prevents processing partial downloads or mid-copy files.
- **Manifest keyed by path with mtime+size fingerprint.** File is re-processed if either changes. Previously failed files are automatically retried.

### CLI (watcher.py)
  --dry-run        Classify only, no encoding
  --full-rescan    Reprocess all files, ignore index
  --build-index    Fast stat()-only index population (first-run bootstrap)
  --paths PATH...  Override default [/Volumes/tv, /Volumes/movies]
  --settle-secs N  Settle threshold in seconds (default 60)

### Fixed in 0.9.5 (patch — 2026-02-17)
- **Race condition:** `StartInterval=300` fires every 5 minutes regardless of
  whether the previous run has finished. A long AV1 encode (3+ minutes per file)
  would cause concurrent ffmpeg instances competing for the same CPU threads and
  writing to the same `.atv_tmp` files — corrupting outputs and thrashing the NAS.
  Fixed with `fcntl.LOCK_EX | LOCK_NB` advisory lock on `/tmp/com.mproadmin.plexwatcher.lock`.
  A second invocation that finds the lock held exits cleanly in ~0.1s with
  `"plexwatcher already running — skipping this tick"`. Lock is automatically
  released on process exit. Verified: concurrent-run test showed run2 exiting
  in 0.1s while run1 held the lock for its full 4s duration.

---

## [1.0.0] — 2026-02-17

**Status:** Batch running in production. 2,845 files being processed across /Volumes/tv and /Volumes/movies. Watcher daemon live and guarding both volumes.

### Summary of all changes since v0.9.0
This release represents the complete, production-hardened system:

- Shared core (`plexfix.py`) — single source of truth for all encode logic
- Batch optimizer (`batch_optimize.py`) — 3-pool parallel processor, resume-safe
- Auto-watcher (`watcher.py`) — launchd periodic daemon for NFS volumes
- Run lock (`fcntl.LOCK_EX`) — prevents overlapping launchd ticks during long encodes
- Resource fork filter — skips macOS `._` sidecar files in NFS walker
- Probe-based subtitle mapping — explicit stream index maps replace broken negative maps
- Verified against 150-file snag scan before batch run
- All 7 fix methods safety-tested (T1–T7, 7/7 pass)
- Index at `.plexfix/manifest.json` — hidden from Plex, mtime+size fingerprints
