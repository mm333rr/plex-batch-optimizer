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
  python3 watcher.py                        # normal run
  python3 watcher.py --dry-run              # classify only, no encoding
  python3 watcher.py --full-rescan          # reprocess every file (ignore index)
  python3 watcher.py --paths /Volumes/tv    # watch specific paths
  python3 watcher.py --settle-secs 120      # wait N seconds after mtime before touching
"""

import os, sys, json, time, logging, argparse, subprocess
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

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(index_dirs: List[Path]) -> None:
    """Log to stdout (captured by launchd) and to each index dir."""
    handlers = [logging.StreamHandler(sys.stdout)]
    for d in index_dirs:
        d.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(d / 'watcher.log'))
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


def save_manifest(index_dir: Path, manifest: Dict) -> None:
    """Atomically write manifest JSON."""
    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = index_dir / (MANIFEST_NAME + '.tmp')
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.rename(index_dir / MANIFEST_NAME)


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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        return {**base_entry, 'status': 'failed', 'error': 'encode timeout'}
    except Exception as e:
        return {**base_entry, 'status': 'failed', 'error': str(e)}

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '')[-300:].strip()
        if tmp_path.exists():
            tmp_path.unlink()
        return {**base_entry, 'status': 'failed',
                'error': f'ffmpeg exit {proc.returncode}: {err}'}

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
            if Path(f).suffix.lower() in VIDEO_EXTS:
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
    args = parser.parse_args()

    # ── Verify mounts are up ────────────────────────────────────────────────
    active_paths = [p for p in args.paths if os.path.ismount(p) or os.path.isdir(p)]
    if not active_paths:
        print('No watched paths are mounted — exiting.')
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
            log.info(f'  --build-index: added {added:,} entries  '
                     f'total={len(manifest):,}  → {index_dir}/{MANIFEST_NAME}')
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

        save_manifest(index_dir, manifest)
        log.info(f'  Index updated: {len(manifest):,} entries → {index_dir}/{MANIFEST_NAME}')

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
    log.info('=' * 60)


if __name__ == '__main__':
    main()
