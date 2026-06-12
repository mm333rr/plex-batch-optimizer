#!/usr/bin/env python3
"""
watcher.py — Automatic ATV4K optimizer for NFS-mounted Plex library.

Runs periodically via launchd (StartInterval=300). On each run:
  1. Walks /Volumes/tv and /Volumes/movies
  2. Compares each video file against the persisted index
  3. New or changed files are classified and fixed if needed
  4. Updates .plexfix/manifest.json at the root of each watched volume

NFS NOTE: FSEvents and launchd WatchPaths only work on local APFS/HFS+
volumes. For NFS mounts, periodic polling via launchd StartInterval is
the macOS-recommended approach. This script is designed to be cheap:
  - stat() is used (not open/read) for unchanged files
  - Only new/changed files trigger ffprobe + encode
  - Full walk of 14k files takes ~8s — negligible at 5-min intervals

INDEX FORMAT (.plexfix/manifest.json):
  {
    "path/to/file.mkv": {
      "size": 123456789,
      "mtime": 1708200000.0,
      "status": "clean" | "fixed" | "failed" | "skipped",
      "issue": "av1" | "dts" | ... | null,
      "processed_at": "2026-02-17T22:00:00",
      "error": "..." | null
    }
  }

USAGE:
  python3 watcher.py                          # normal run
  python3 watcher.py --dry-run                # classify only, no encoding
  python3 watcher.py --full-rescan            # reprocess every file (ignore index)
  python3 watcher.py --paths /Volumes/tv      # watch specific paths
  python3 watcher.py --settle-secs 120        # wait N seconds after mtime before touching
  python3 watcher.py --boot-grace 600         # skip run if system booted <N secs ago (default 600)
  python3 watcher.py --max-jobs-per-run 1     # max ffmpeg jobs per tick (default 1)
  python3 watcher.py --inter-job-sleep 30     # seconds between encode jobs (default 30)
"""

import errno, os, re, sys, json, time, logging, argparse, subprocess, fcntl
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, List, Tuple

# ── Import shared core ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from plexfix import (
    probe, classify_probe, build_cmd, verify, output_ext,
    VIDEO_EXTS, ATV_CONT_OK
)

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_PATHS   = ['/Volumes/tv', '/Volumes/movies']
INDEX_DIR_NAME  = '.plexfix'           # dot-prefixed → hidden from Plex
MANIFEST_NAME   = 'manifest.json'
SETTLE_SECS     = 60                   # ignore files modified within last N seconds
MAX_LOG_LINES   = 2000                 # rolling log cap in index dir
LOCK_FILE       = '/tmp/com.mproadmin.plexwatcher.lock'  # exclusive run lock
BOOT_GRACE_DEFAULT     = 600           # seconds after boot before encoding begins
MAX_JOBS_PER_RUN_DEFAULT = 1           # max ffmpeg jobs per launchd tick
INTER_JOB_SLEEP_DEFAULT  = 30         # seconds to sleep between encode jobs


# ── Run lock (prevent overlapping launchd invocations) ────────────────────────

_lock_fh = None   # module-level file handle kept open for duration of run

def acquire_lock() -> bool:
    """Try to acquire an exclusive advisory lock.

    Uses fcntl.LOCK_EX | fcntl.LOCK_NB on a well-known lock file.
    If another instance of watcher.py is already running (e.g. a long
    AV1 encode from a previous launchd tick), this returns False
    immediately so the new invocation can exit cleanly.

    The lock is automatically released when the process exits (OS closes
    all file descriptors). We also keep the FD open in _lock_fh so it
    isn't garbage-collected mid-run.
    """
    global _lock_fh
    _lock_fh = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        return True
    except OSError:
        # Another instance holds the lock
        _lock_fh.close()
        _lock_fh = None
        return False


def release_lock() -> None:
    """Explicitly release the lock and remove the lock file."""
    global _lock_fh
    if _lock_fh:
        try:
            fcntl.flock(_lock_fh, fcntl.LOCK_UN)
            _lock_fh.close()
        except OSError:
            pass
        _lock_fh = None
    try:
        os.unlink(LOCK_FILE)
    except OSError:
        pass


# ── Boot age check ─────────────────────────────────────────────────────────────

def boot_age_secs() -> float:
    """Return seconds elapsed since the last system boot via kern.boottime sysctl.

    Parses the sysctl output:  { sec = 1775435301, usec = 515163 } ...
    Returns float('inf') if the value cannot be determined so that the caller
    conservatively allows the run to proceed.
    """
    try:
        r = subprocess.run(
            ['sysctl', '-n', 'kern.boottime'],
            capture_output=True, text=True, timeout=5
        )
        m = re.search(r'sec\s*=\s*(\d+)', r.stdout)
        if m:
            return time.time() - int(m.group(1))
    except Exception:
        pass
    return float('inf')  # unknown → assume old boot, allow run


# ── Logging ───────────────────────────────────────────────────────────────────

class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that silently swallows EIO / BrokenPipe on stdout.

    When watcher.py runs under launchd with StandardOutPath redirection,
    stdout is a pipe whose read-end may be closed or whose buffer may be
    full. Python's logging.StreamHandler calls flush() after every emit,
    which raises OSError(EIO) in that situation. The default logging
    error-handler prints a traceback to stderr on every log call —
    flooding watcher_launchd_stderr.log with thousands of lines of noise.

    This subclass catches those specific I/O errors and discards them
    silently. The file handlers (writing to the NFS index dir) are
    unaffected and continue to receive every record.
    """

    _SWALLOWED_ERRNOS = frozenset({
        errno.EIO,          # [Errno 5]  — pipe buffer full / read-end closed
        errno.EPIPE,        # [Errno 32] — broken pipe
        errno.EBADF,        # [Errno 9]  — fd closed under us
    })

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record, swallowing benign pipe/IO errors on stdout."""
        try:
            super().emit(record)
        except OSError as exc:
            if exc.errno in self._SWALLOWED_ERRNOS:
                return   # launchd pipe issue — not fatal, discard silently
            raise       # unexpected I/O error — let logging handle it normally


def setup_logging(index_dirs: List[Path]) -> None:
    """Log to stdout (via SafeStreamHandler) and to each index dir.

    File handlers are only created for index dirs that already exist or
    can be created. If the NFS volume is not mounted, mkdir will fail —
    the SafeStreamHandler on stdout will still work as a fallback.
    """
    handlers: List[logging.Handler] = [SafeStreamHandler(sys.stdout)]
    for d in index_dirs:
        try:
            d.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(d / 'watcher.log'))
        except (OSError, PermissionError) as exc:
            # Volume not mounted or read-only — skip the file handler.
            # The SafeStreamHandler above will capture everything to stdout.
            print(f'{datetime.now().isoformat(timespec="seconds")} '
                  f'[WARNING] Could not create log dir {d}: {exc}',
                  flush=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers,
    )

log = logging.getLogger(__name__)

# ── Index helpers ──────────────────────────────────────────────────────────────

def load_manifest(index_dir: Path) -> Dict:
    """Load existing manifest from index dir, return empty dict if missing."""
    p = index_dir / MANIFEST_NAME
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_manifest(index_dir: Path, manifest: Dict) -> bool:
    """Atomically write manifest JSON.

    Returns True on success, False on failure (with a warning logged to
    stdout via the SafeStreamHandler). Failures are non-fatal — a failed
    save means that run's updates are lost, but the next run will
    re-classify the same files and try again. This is far preferable to
    aborting the entire run for all volumes.
    """
    try:
        index_dir.mkdir(parents=True, exist_ok=True)
        tmp = index_dir / (MANIFEST_NAME + '.tmp')
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.rename(index_dir / MANIFEST_NAME)
        return True
    except (OSError, PermissionError) as exc:
        # Log to stdout — the file handler for this volume may not exist
        # if the volume wasn't mounted when setup_logging() ran.
        print(f'{datetime.now().isoformat(timespec="seconds")} '
              f'[WARNING] save_manifest failed for {index_dir}: {exc}',
              flush=True)
        return False


def trim_log(log_path: Path) -> None:
    """Keep log file under MAX_LOG_LINES by trimming oldest lines."""
    if not log_path.exists():
        return
    lines = log_path.read_text(errors='replace').splitlines()
    if len(lines) > MAX_LOG_LINES:
        log_path.write_text('\n'.join(lines[-MAX_LOG_LINES:]) + '\n')

# ── File fingerprint ───────────────────────────────────────────────────────────

def fingerprint(path: str) -> Tuple[int, float]:
    """Return (size_bytes, mtime) for a file — cheap stat(), no read."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


def is_settled(mtime: float, settle_secs: int) -> bool:
    """Return True if file mtime is older than settle_secs (done writing)."""
    return (time.time() - mtime) >= settle_secs


def needs_processing(path: str, manifest: Dict, full_rescan: bool,
                     settle_secs: int) -> Tuple[bool, str]:
    """Decide whether this file needs to be classified/processed.

    Returns (should_process, reason).
    """
    size, mtime = fingerprint(path)
    if size == 0:
        return False, 'zero-size'

    if not is_settled(mtime, settle_secs):
        return False, f'still writing (mtime {settle_secs}s ago)'

    if full_rescan:
        return True, 'full-rescan'

    entry = manifest.get(path)
    if entry is None:
        return True, 'new file'

    # Re-process if file changed since last index
    if entry.get('size') != size or abs(entry.get('mtime', 0) - mtime) > 1:
        return True, 'file changed'

    # Re-process previously failed files (maybe batch_optimize fixed the command)
    if entry.get('status') == 'failed':
        return True, 'retry failed'

    return False, f"already {entry.get('status','indexed')}"

# ── Single-file processor ──────────────────────────────────────────────────────

def is_faststart(path: str) -> bool:
    """True if an MP4/MOV has its moov atom before mdat (QuickLook + stream friendly)."""
    import struct
    try:
        with open(path, "rb") as f:
            order = []
            while True:
                h = f.read(8)
                if len(h) < 8:
                    break
                size = struct.unpack(">I", h[:4])[0]
                typ = h[4:8].decode("latin1", "replace")
                hdr = 8
                if size == 1:
                    size = struct.unpack(">Q", f.read(8))[0]
                    hdr = 16
                if typ in ("moov", "mdat"):
                    order.append(typ)
                    if "moov" in order and "mdat" in order:
                        break
                if size == 0:
                    break
                f.seek(size - hdr, 1)
        return bool(order) and order[0] == "moov"
    except Exception:
        return True


def process_file(path: str, dry_run: bool) -> Dict:
    """Classify, optionally fix, and return an updated manifest entry."""
    size, mtime = fingerprint(path)
    now = datetime.now().isoformat(timespec='seconds')

    base_entry = {
        'size':         size,
        'mtime':        mtime,
        'processed_at': now,
        'error':        None,
    }

    # ── Probe ────────────────────────────────────────────────────────────────
    probe_data = probe(path)
    if not probe_data:
        return {**base_entry, 'status': 'failed', 'issue': None,
                'error': 'ffprobe failed'}

    # ── Classify ─────────────────────────────────────────────────────────────
    issue = classify_probe(probe_data)
    if issue is None and Path(path).suffix.lower() in ('.mp4', '.mov') and not is_faststart(path):
        issue = 'faststart'
    base_entry['issue'] = issue

    if issue is None:
        return {**base_entry, 'status': 'clean'}

    log.info(f'  [{issue.upper()}] {Path(path).name}')

    if dry_run:
        return {**base_entry, 'status': 'dry-run'}

    # ── Determine output paths ────────────────────────────────────────────────
    src_path   = Path(path)
    ext        = output_ext(issue, path)
    tmp_path   = src_path.parent / (src_path.stem + '.atv_tmp' + ext)
    final_path = src_path.parent / (src_path.stem + ext)
    bak_path   = Path(path + '.bak')

    # Clean stale tmp
    if tmp_path.exists():
        tmp_path.unlink()

    # ── Encode ───────────────────────────────────────────────────────────────
    cmd = build_cmd(issue, path, str(tmp_path), probe_data=probe_data)
    t0  = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
            preexec_fn=lambda: os.nice(5),  # ffmpeg nice→15 (plist base 10 + 5)
        )
    except subprocess.TimeoutExpired:
        return {**base_entry, 'status': 'failed', 'error': 'encode timeout'}
    except Exception as e:
        return {**base_entry, 'status': 'failed', 'error': str(e)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '')[-500:].strip()
        if tmp_path.exists():
            tmp_path.unlink()
        err_msg = f'ffmpeg exit {proc.returncode}: {err}'
        log.warning(f'    ❌ {Path(path).name}: {err_msg}')
        return {**base_entry, 'status': 'failed',
                'error': err_msg}

    elapsed = time.time() - t0

    # ── Verify ───────────────────────────────────────────────────────────────
    ok, msg = verify(path, str(tmp_path))
    if not ok:
        if tmp_path.exists():
            tmp_path.unlink()
        return {**base_entry, 'status': 'failed', 'error': f'verify: {msg}'}

    # ── Replace original ─────────────────────────────────────────────────────
    try:
        src_path.rename(bak_path)                      # original → .bak
        tmp_path.rename(final_path)                    # tmp → final
    except Exception as e:
        # Try to restore
        if bak_path.exists() and not src_path.exists():
            bak_path.rename(src_path)
        if tmp_path.exists():
            tmp_path.unlink()
        return {**base_entry, 'status': 'failed', 'error': f'rename: {e}'}

    out_mb = os.path.getsize(str(final_path)) / 1024 / 1024
    in_mb  = size / 1024 / 1024
    log.info(f'    ✅ fixed in {elapsed:.0f}s  {in_mb:.0f}MB→{out_mb:.0f}MB  '
             f'{"(ext changed) " if str(final_path) != path else ""}'
             f'bak={bak_path.name}')

    # Remove safety backup now that encode + verify + rename all succeeded
    try:
        bak_path.unlink()
        log.info(f'    removed backup {bak_path.name}')
    except OSError as e:
        log.warning(f'    could not remove backup {bak_path.name}: {e}')

    # Update manifest key if extension changed
    final_key = str(final_path)
    updated = {**base_entry,
               'status': 'fixed',
               'size':   os.path.getsize(str(final_path)),
               'mtime':  os.stat(str(final_path)).st_mtime}
    return updated, final_key  # type: ignore

# ── Volume walker ─────────────────────────────────────────────────────────────

def walk_volume(volume_root: str, settle_secs: int) -> List[str]:
    """Yield all video file paths under volume_root, skipping dot dirs."""
    paths = []
    for root, dirs, files in os.walk(volume_root):
        # Skip dot-dirs (including .plexfix index dir) and @eaDir (Synology)
        dirs[:] = [d for d in dirs
                   if not d.startswith('.') and d != '@eaDir' and d != '#recycle']
        for f in files:
            # Skip macOS resource fork sidecars (._filename) and hidden files
            if not f.startswith('.') and Path(f).suffix.lower() in VIDEO_EXTS:
                paths.append(os.path.join(root, f))
    return paths

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Periodic ATV4K optimizer for NFS-mounted Plex library')
    parser.add_argument('--dry-run',      action='store_true',
                        help='Classify only, no encoding')
    parser.add_argument('--full-rescan',  action='store_true',
                        help='Reprocess all files, ignore existing index')
    parser.add_argument('--paths',        nargs='+', default=DEFAULT_PATHS,
                        help='Volume roots to watch')
    parser.add_argument('--settle-secs',  type=int, default=SETTLE_SECS,
                        help='Ignore files modified within this many seconds (default 60)')
    parser.add_argument('--build-index',  action='store_true',
                        help='Fast first-run mode: stat() every file and record as '
                             '"indexed" without probing. Run once on an existing clean '
                             'library so subsequent watcher runs only probe new files.')
    parser.add_argument('--boot-grace',   type=int, default=BOOT_GRACE_DEFAULT,
                        help=f'Skip run if system booted less than N seconds ago '
                             f'(default {BOOT_GRACE_DEFAULT}). Prevents immediate '
                             f'post-reboot CPU spike. Set 0 to disable.')
    parser.add_argument('--max-jobs-per-run', type=int, default=MAX_JOBS_PER_RUN_DEFAULT,
                        help=f'Max ffmpeg encode jobs per watcher tick '
                             f'(default {MAX_JOBS_PER_RUN_DEFAULT}). Remaining files '
                             f'deferred to next launchd interval.')
    parser.add_argument('--inter-job-sleep', type=int, default=INTER_JOB_SLEEP_DEFAULT,
                        help=f'Seconds to sleep between encode jobs '
                             f'(default {INTER_JOB_SLEEP_DEFAULT}). Gives plex-guardian '
                             f'breathing room between CPU spikes.')
    args = parser.parse_args()

    # ── Single-instance lock ───────────────────────────────────────────────
    # launchd StartInterval fires every 300s regardless of whether the
    # previous run finished. A long AV1 encode can take hours. Without
    # the lock a new invocation would spawn concurrent ffmpeg processes
    # competing for the same CPU threads and writing to the same .atv_tmp
    # files — corrupting outputs and thrashing the NAS.
    if not acquire_lock():
        # Another instance is still running — log to stdout (captured by
        # launchd) and exit cleanly so the next tick schedules normally.
        print(f'{datetime.now().isoformat(timespec="seconds")} '
              f'[INFO] plexwatcher already running — skipping this tick',
              flush=True)
        sys.exit(0)

    # ── Boot grace period ───────────────────────────────────────────────────
    # launchd RunAtLoad=true fires this script within seconds of boot.
    # Starting ffmpeg immediately after boot races with Plex, NFS mounts,
    # and WindowServer stabilisation — the primary cause of the overnight
    # CPU panic storm. Wait until the system has been up for boot_grace secs.
    if args.boot_grace > 0:
        age = boot_age_secs()
        if age < args.boot_grace:
            remaining = int(args.boot_grace - age)
            print(
                f'{datetime.now().isoformat(timespec="seconds")} [INFO] '
                f'Boot grace active: system up {age:.0f}s, '
                f'grace={args.boot_grace}s, waiting {remaining}s. '
                f'Exiting — launchd will retry in {300}s.',
                flush=True
            )
            release_lock()
            sys.exit(0)

    # ── Verify mounts are up ────────────────────────────────────────────────
    # os.path.isdir() fallback is intentionally omitted: an unmounted NFS
    # mountpoint is an empty root-owned directory that passes isdir() but
    # fails any write attempt with PermissionError. ismount() is the only
    # reliable guard. A secondary os.listdir() confirms the volume is
    # actually readable (not just present as a stub).
    active_paths = []
    for p in args.paths:
        if not os.path.ismount(p):
            log.warning(f'  {p} is not mounted — skipping')
            continue
        try:
            os.listdir(p)
        except OSError as exc:
            log.warning(f'  {p} mounted but not readable ({exc}) — skipping')
            continue
        active_paths.append(p)
    if not active_paths:
        print('No watched paths are mounted and readable — exiting.')
        sys.exit(0)

    # ── Set up index dirs + logging ─────────────────────────────────────────
    index_dirs = {p: Path(p) / INDEX_DIR_NAME for p in active_paths}
    setup_logging(list(index_dirs.values()))

    log.info('=' * 60)
    log.info(f'plexwatcher run  dry={args.dry_run}  '
             f'full={args.full_rescan}  settle={args.settle_secs}s')
    log.info(f'Watching: {active_paths}')

    total_new = total_clean = total_fixed = total_failed = total_skipped = 0

    for vol_path, index_dir in index_dirs.items():
        manifest = load_manifest(index_dir)
        updated_keys = {}   # path → new manifest entry

        # Walk volume
        t_walk = time.time()
        all_files = walk_volume(vol_path, args.settle_secs)
        walk_dur  = time.time() - t_walk
        log.info(f'  {vol_path}: {len(all_files):,} video files  (walk {walk_dur:.1f}s)')

        # ── Fast index build (--build-index) ──────────────────────────────
        if args.build_index:
            added = 0
            for path in all_files:
                if path in manifest:
                    continue
                size, mtime = fingerprint(path)
                if size > 0 and is_settled(mtime, args.settle_secs):
                    manifest[path] = {
                        'size': size, 'mtime': mtime,
                        'status': 'indexed', 'issue': None,
                        'processed_at': datetime.now().isoformat(timespec='seconds'),
                        'error': None,
                    }
                    added += 1
            save_manifest(index_dir, manifest)
            log.info(f'  --build-index: added {added:,} entries  '                     f'total={len(manifest):,}  → {index_dir}/{MANIFEST_NAME}')
            log.info(f'  Re-run without --build-index to classify and fix new files.')
            continue   # skip processing loop for this volume

        to_process = []
        for path in all_files:
            should, reason = needs_processing(
                path, manifest, args.full_rescan, args.settle_secs)
            if should:
                to_process.append(path)
            else:
                total_skipped += 1

        if not to_process:
            log.info(f'  No new or changed files in {vol_path}')
        else:
            log.info(f'  {len(to_process)} files to process')

        jobs_this_run = 0
        for path in to_process:
            total_new += 1
            result = process_file(path, args.dry_run)

            # process_file returns (entry, final_key) tuple when ext changes
            if isinstance(result, tuple):
                entry, final_key = result
                updated_keys[final_key] = entry
                # Remove old path from manifest (ext changed: .avi→.mp4)
                manifest.pop(path, None)
            else:
                entry = result
                updated_keys[path] = entry

            status = entry.get('status', '?')
            if status == 'clean':       total_clean  += 1
            elif status in ('fixed', 'dry-run'): total_fixed  += 1
            elif status == 'failed':    total_failed += 1

            # ── Job cap + inter-job sleep ──────────────────────────────
            # Limit ffmpeg jobs per launchd tick to prevent sustained CPU
            # saturation. The 5-min StartInterval naturally throttles the
            # overall encode rate. Sleep between jobs to give plex-guardian
            # and Plex breathing room before the next CPU spike.
            if status in ('fixed', 'failed'):  # only count real encode attempts
                jobs_this_run += 1
                if jobs_this_run >= args.max_jobs_per_run:
                    remaining_files = len(to_process) - to_process.index(path) - 1
                    if remaining_files > 0:
                        log.info(
                            f'  Job cap reached ({args.max_jobs_per_run}/run) — '
                            f'{remaining_files} file(s) deferred to next tick'
                        )
                    break
                # Inter-job sleep (only if more jobs remain and cap not hit)
                next_idx = to_process.index(path) + 1
                if next_idx < len(to_process) and args.inter_job_sleep > 0:
                    log.info(f'  Sleeping {args.inter_job_sleep}s between jobs...')
                    time.sleep(args.inter_job_sleep)

        # ── Update manifest ────────────────────────────────────────────────
        # Also add unprocessed files to manifest so they're indexed as 'clean'
        # (avoids re-probing them next run)
        for path in all_files:
            if path not in manifest and path not in updated_keys:
                size, mtime = fingerprint(path)
                if size > 0 and is_settled(mtime, args.settle_secs):
                    manifest[path] = {
                        'size': size, 'mtime': mtime,
                        'status': 'indexed', 'issue': None,
                        'processed_at': datetime.now().isoformat(timespec='seconds'),
                        'error': None,
                    }

        manifest.update(updated_keys)

        # Prune manifest entries for files that no longer exist
        missing = [p for p in list(manifest.keys()) if not os.path.exists(p)]
        for p in missing:
            del manifest[p]
        if missing:
            log.info(f'  Pruned {len(missing)} missing files from manifest')

        saved = save_manifest(index_dir, manifest)
        if saved:
            log.info(f'  Index updated: {len(manifest):,} entries → {index_dir}/{MANIFEST_NAME}')
        else:
            log.warning(f'  Index NOT saved for {vol_path} — will retry next run')

        # Trim rolling log
        trim_log(index_dir / 'watcher.log')

    # ── Summary ─────────────────────────────────────────────────────────────
    log.info('-' * 60)
    log.info(f'Run complete: '
             f'new={total_new}  clean={total_clean}  '
             f'fixed={total_fixed}  failed={total_failed}  '
             f'skipped={total_skipped}')
    if total_failed:
        log.warning(f'  {total_failed} file(s) failed — check watcher.log in index dirs')
    release_lock()
    log.info('=' * 60)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Last-resort crash handler — write to /tmp so it's always writable
        # even if NFS volumes are down or logging handlers failed to init.
        import traceback
        crash_path = '/tmp/plexwatcher-crash.log'
        ts = datetime.now().isoformat(timespec='seconds')
        msg = f'{ts} UNHANDLED EXCEPTION in plexwatcher:\n{traceback.format_exc()}\n'
        try:
            with open(crash_path, 'a') as f:
                f.write(msg)
        except OSError:
            pass
        # Also print to stderr so launchd captures it in watcher_launchd_stderr.log
        print(msg, file=sys.stderr, flush=True)
        release_lock()
        sys.exit(1)
