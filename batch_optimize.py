#!/usr/bin/env python3
"""
batch_optimize.py — In-place ATV4K codec fixer for Plex NAS library.

STRATEGY
  For each problem file on the NAS:
    1. Encode the fixed version to a .tmp file in the SAME directory as the original
    2. Verify the .tmp output is valid (codec check, duration check, size sanity)
    3. Rename original → original.bak (backup stays in same folder)
    4. Rename .tmp → final name (replaces original in-place)
  On any failure the .tmp is deleted and original is never touched.

METHODS (all validated by safety_test.py 2026-02-17):
  pgs_vobsub       ffmpeg -c copy, drop subtitle streams    (I/O only, ~instant)
  mjpeg            ffmpeg -map 0:v:0 -c copy                (I/O only, ~instant)
  bad_container    ffmpeg -c copy or -c:a aac → new ext     (I/O only, fast)
  dts              ffmpeg -c:v copy, -c:a eac3 448k         (audio transcode)
  av1              ffmpeg libx264 -vf format=yuv420p -crf20 (CPU heavy)

USAGE
  python3 batch_optimize.py --test          # 5 files (one per type), then STOP
  python3 batch_optimize.py --dry-run       # show what would happen, no changes
  python3 batch_optimize.py                 # full batch
  python3 batch_optimize.py --type pgs_vobsub,mjpeg   # specific types only
  python3 batch_optimize.py --limit 50      # cap at N files
  python3 batch_optimize.py --workers-io 16 --workers-light 8 --workers-heavy 2

WATCH / STOP / RESTART
  tail -f "/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/batch_current.log"
  ps aux | grep -E "[b]atch_optimize|[f]fmpeg" | awk '{print $2,$3"%CPU",$11}'
  kill $(pgrep -f "batch_optimize") && pkill -f ffmpeg   # graceful stop (SIGINT)
  python3 batch_optimize.py                               # restart (skips completed)
"""

import os, sys, json, time, signal, logging, argparse, threading, subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Set
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE      = Path('/Users/mProAdmin/Claude Scripts and Venvs/MediaScan')
RESULTS   = BASE / 'results'
SCAN_JSON = RESULTS / 'scan_results.json'
DONE_FILE = RESULTS / 'batch_completed.json'
FAIL_FILE = RESULTS / 'batch_failed.json'
# Always write to the "current" symlink-style log (overwritten each run)
# AND a timestamped archive log
TS        = datetime.now().strftime('%Y%m%d_%H%M%S')
LOG_ARCH  = RESULTS / f'batch_{TS}.log'
LOG_LIVE  = RESULTS / 'batch_current.log'
FFMPEG    = '/usr/local/bin/ffmpeg'

# ── ATV4K-compatible codecs ────────────────────────────────────────────────────
ATV_VIDEO_OK = {'h264', 'hevc', 'mpeg4', 'vp9'}
BAD_SUB      = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'pgssub'}

# ── Logging (dual: live symlink + archive) ────────────────────────────────────
RESULTS.mkdir(parents=True, exist_ok=True)
fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('batch')
log.setLevel(logging.INFO)
for handler in [
    logging.FileHandler(LOG_ARCH),
    logging.FileHandler(LOG_LIVE, mode='w'),   # overwrite each run
    logging.StreamHandler(sys.stdout),
]:
    handler.setFormatter(fmt)
    log.addHandler(handler)

# ── Graceful stop ──────────────────────────────────────────────────────────────
_stop = threading.Event()
def _sigint(s, f):
    log.warning('SIGINT received — finishing current jobs then stopping.')
    log.warning('Ctrl+C again to force-kill (may leave .tmp files).')
    _stop.set()
signal.signal(signal.SIGINT, _sigint)

# ── Resume state ───────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_completed: Set[str] = set()
_failed:    Dict[str, str] = {}

def _load_state():
    if DONE_FILE.exists():
        _completed.update(json.loads(DONE_FILE.read_text()))
    if FAIL_FILE.exists():
        _failed.update(json.loads(FAIL_FILE.read_text()))

def _save_state():
    with _state_lock:
        DONE_FILE.write_text(json.dumps(sorted(_completed), indent=2))
        FAIL_FILE.write_text(json.dumps(_failed, indent=2))

def _mark_done(path: str):
    with _state_lock:
        _completed.add(path)
    _save_state()

def _mark_fail(path: str, reason: str):
    with _state_lock:
        _failed[path] = reason
    _save_state()

# ── Classifier ─────────────────────────────────────────────────────────────────
def classify(r: Dict) -> Optional[str]:
    """Return primary issue type or None if clean."""
    vid    = r.get('video', [{}])
    aud    = r.get('audio', [])
    subs   = r.get('subtitles', [])
    vd     = vid[0] if vid else {}
    vcodec = vd.get('codec', '').lower()
    acodec = aud[0].get('codec', '').lower() if aud else ''
    cont   = r.get('container', '').lower()
    sub_cc = [s.get('codec', '').lower() for s in subs]
    has_mjpeg   = any(v.get('codec', '').lower() == 'mjpeg' for v in vid[1:])
    has_bad_sub = any(c in BAD_SUB for c in sub_cc)

    if vcodec == 'av1':                                   return 'av1'
    if acodec in ('dts', 'truehd', 'mlp'):                return 'dts'
    if cont in ('.avi', '.wmv', '.rm', '.rmvb', '.flv'):  return 'bad_container_avi'
    if cont in ('.ts', '.m2ts'):                          return 'bad_container_ts'
    if has_mjpeg:                                         return 'mjpeg'
    if has_bad_sub:                                       return 'pgs_vobsub'
    return None

def output_ext(issue: str, src: str) -> str:
    """Final output extension for this issue type."""
    if issue == 'bad_container_avi': return '.mp4'
    if issue == 'bad_container_ts':  return '.mkv'
    return Path(src).suffix          # keep original extension

# ── ffmpeg command builder ─────────────────────────────────────────────────────
# ── Helper: determine text-only sub stream maps ─────────────────────────────

IMAGE_SUBS: set = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'pgssub'}

def text_sub_maps(probe_data: Dict) -> List[str]:
    """Return explicit -map 0:N args for text subtitle streams only.
    Image-based subs (PGS/VobSub) are excluded by omission.
    NOTE: ffmpeg negative map (:m:codec_name:X) selects by USER TAGS, not
    codec properties, so it is unreliable for this use case. Explicit positive
    index mapping is the only guaranteed way to exclude image subs."""
    maps: List[str] = []
    for s in probe_data.get('streams', []):
        if (s.get('codec_type') == 'subtitle' and
                s.get('codec_name', '').lower() not in IMAGE_SUBS):
            maps += ['-map', f"0:{s['index']}"]
    return maps

def attachment_maps(probe_data: Dict) -> List[str]:
    """Return -map args for font/attachment streams (needed for ASS rendering)."""
    maps: List[str] = []
    for s in probe_data.get('streams', []):
        if s.get('codec_type') == 'attachment':
            maps += ['-map', f"0:{s['index']}"]
    return maps


def build_cmd(issue: str, src: str, tmp: str,
              probe_data: Optional[Dict] = None) -> List[str]:
    """Build ffmpeg command for this issue type.

    probe_data: live ffprobe output for this file. If None, fetched automatically.
    We build EXPLICIT positive stream-index maps for text subs only.
    Negative maps (-map -0:s:m:codec_name:X) are unreliable — see text_sub_maps.
    """
    if probe_data is None:
        probe_data = probe(src) or {'streams': []}

    base = [FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning', '-stats', '-i', src]

    if issue == 'pgs_vobsub':
        # Drop image subs, preserve text subs (ASS/SRT) and font attachments.
        return base + [
            '-map', '0:v', '-map', '0:a',
            *text_sub_maps(probe_data),    # explicit indices: skips PGS/VobSub
            *attachment_maps(probe_data),  # fonts for ASS
            '-c', 'copy', tmp
        ]

    if issue == 'mjpeg':
        # 0:v:0 = primary video only (skips attached MJPEG thumbnail).
        # Preserve text subs; silently drop any co-present PGS (Demon Slayer).
        return base + [
            '-map', '0:v:0', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c', 'copy', tmp
        ]

    if issue == 'dts':
        # Copy HEVC/H264 video, transcode DTS/TrueHD → EAC3.
        # ~97% of DTS files (Boruto etc.) also have PGS — drop them here.
        return base + [
            '-map', '0:v', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c:v', 'copy',
            '-c:a', 'eac3', '-b:a', '448k',
            '-c:s', 'copy', tmp
        ]

    if issue == 'bad_container_avi':
        # MPEG4+MP3 in AVI → MP4. -fflags +genpts fixes VBR MP3 timestamps.
        return [FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning', '-stats',
                '-fflags', '+genpts', '-i', src,
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                '-movflags', '+faststart', tmp]

    if issue == 'bad_container_ts':
        # Drop timed_id3 data streams, copy A/V only to MKV.
        return base + ['-map', '0:v', '-map', '0:a', '-c', 'copy', tmp]

    if issue == 'av1':
        # AV1 10-bit (yuv420p10le) → H.264 8-bit via libx264.
        # format=yuv420p: mandatory 10→8 bit conversion for libx264 default profile.
        # Drops PGS/image subs; preserves ASS/SRT text subs and font attachments.
        return base + [
            '-map', '0:v:0', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-vf', 'format=yuv420p',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
            '-profile:v', 'high', '-level', '4.1',
            '-threads', '12',
            '-c:a', 'copy', '-c:s', 'copy',
            '-movflags', '+faststart', tmp,
        ]

    raise ValueError(f'Unknown issue type: {issue}')


def probe(path: str) -> Optional[Dict]:
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True, timeout=30
    )
    return json.loads(r.stdout) if r.returncode == 0 else None

# ── Output verifier ────────────────────────────────────────────────────────────
def verify(src: str, tmp: str, label: str) -> bool:
    if not os.path.exists(tmp) or os.path.getsize(tmp) < 4096:
        log.error(f'[{label}] VERIFY FAIL: output missing or empty')
        return False

    inp = probe(src)
    out = probe(tmp)
    if not inp or not out:
        log.error(f'[{label}] VERIFY FAIL: ffprobe failed on output')
        return False

    # Duration must be within 3%
    in_dur  = float(inp['format'].get('duration', 0))
    out_dur = float(out['format'].get('duration', 0))
    if in_dur > 5 and abs(in_dur - out_dur) / in_dur > 0.03:
        log.error(f'[{label}] VERIFY FAIL: duration {in_dur:.1f}s→{out_dur:.1f}s (>{3}% drift)')
        return False

    # Output must not be smaller than 10% of input (sanity — avoids empty encodes)
    in_bytes  = os.path.getsize(src)
    out_bytes = os.path.getsize(tmp)
    if out_bytes < in_bytes * 0.10:
        log.error(f'[{label}] VERIFY FAIL: output suspiciously small '
                  f'({out_bytes//1024//1024}MB vs {in_bytes//1024//1024}MB input)')
        return False

    # Stream checks
    has_video = False
    for s in out['streams']:
        ct   = s.get('codec_type', '')
        cn   = s.get('codec_name', '').lower()
        disp = s.get('disposition', {})
        if ct == 'video':
            if disp.get('attached_pic'):
                log.error(f'[{label}] VERIFY FAIL: MJPEG attached pic still present')
                return False
            has_video = True
            if cn not in ATV_VIDEO_OK:
                log.error(f'[{label}] VERIFY FAIL: video codec {cn} not ATV4K-compatible')
                return False
        elif ct == 'audio':
            if cn == 'dts':
                log.error(f'[{label}] VERIFY FAIL: DTS audio still present (not ATV4K-compatible)')
                return False
        elif ct == 'subtitle':
            if cn in BAD_SUB:
                log.error(f'[{label}] VERIFY FAIL: image subtitle {cn} still present')
                return False

    if not has_video:
        log.error(f'[{label}] VERIFY FAIL: no video stream in output')
        return False

    in_mb  = in_bytes  / 1024 / 1024
    out_mb = out_bytes / 1024 / 1024
    log.info(f'[{label}] ✅ verified {in_mb:.0f}MB→{out_mb:.0f}MB ({out_mb/in_mb*100:.0f}%) direct-play OK')
    return True

# ── Core per-file worker ───────────────────────────────────────────────────────
def process_file(r: Dict, issue: str, args: argparse.Namespace,
                 counters: Dict, ctr_lock: threading.Lock) -> bool:
    src   = r['path']
    label = Path(src).name[:50]

    if src in _completed:
        return True

    if not os.path.exists(src):
        log.warning(f'[{label}] SKIP: source not found on NAS')
        _mark_fail(src, 'source missing')
        return False

    # Determine output paths
    src_path = Path(src)
    ext      = output_ext(issue, src)
    # .tmp lives alongside the original while encoding
    tmp_path = src_path.parent / (src_path.stem + '.atv_tmp' + ext)
    # Final path (same dir, correct extension)
    final_path = src_path.parent / (src_path.stem + ext)
    # Backup path (original gets .bak appended to its full name)
    bak_path = Path(src + '.bak')

    # Skip if bak already exists (means we already processed this one)
    if bak_path.exists():
        log.info(f'[{label}] SKIP: .bak exists — already processed')
        _mark_done(src)
        return True

    log.info(f'[{issue.upper()}] {label}')
    if args.dry_run:
        log.info(f'  DRY-RUN → {final_path.name}  (orig→{bak_path.name})')
        with ctr_lock:
            counters['dry'] += 1
        return True

    # Clean up any stale .tmp from a previous interrupted run
    if tmp_path.exists():
        tmp_path.unlink()

    # ── Encode ────────────────────────────────────────────────────────────────
    probe_data = probe(src) or {'streams': []}
    cmd = build_cmd(issue, src, str(tmp_path), probe_data=probe_data)
    log.info(f'  cmd: {" ".join(cmd[-6:])}')   # log last 6 args (output side)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    except subprocess.TimeoutExpired:
        log.error(f'[{label}] TIMEOUT after 2h — original untouched')
        tmp_path.unlink(missing_ok=True)
        _mark_fail(src, 'timeout')
        return False
    except Exception as e:
        log.error(f'[{label}] EXCEPTION: {e}')
        tmp_path.unlink(missing_ok=True)
        _mark_fail(src, str(e))
        return False

    elapsed = time.time() - t0

    if proc.returncode != 0:
        err = (proc.stderr or '')[-300:].strip()
        log.error(f'[{label}] ❌ ffmpeg exit {proc.returncode} in {elapsed:.0f}s')
        log.error(f'  {err}')
        tmp_path.unlink(missing_ok=True)
        _mark_fail(src, f'exit {proc.returncode}: {err[:150]}')
        return False

    log.info(f'  encode done in {elapsed:.0f}s')

    # ── Verify ────────────────────────────────────────────────────────────────
    if not verify(src, str(tmp_path), label):
        tmp_path.unlink(missing_ok=True)
        _mark_fail(src, 'verify failed')
        return False

    # ── Atomic swap: orig→.bak  .tmp→final ───────────────────────────────────
    try:
        src_path.rename(bak_path)          # orig  → orig.bak
        tmp_path.rename(final_path)        # .tmp  → final (same ext or new ext)
    except Exception as e:
        log.error(f'[{label}] SWAP FAILED: {e} — attempting rollback')
        # If bak succeeded but rename of tmp failed, restore original
        if bak_path.exists() and not src_path.exists():
            bak_path.rename(src_path)
        tmp_path.unlink(missing_ok=True)
        _mark_fail(src, f'swap failed: {e}')
        return False

    in_mb  = bak_path.stat().st_size  / 1024 / 1024
    out_mb = final_path.stat().st_size / 1024 / 1024
    log.info(f'  ✅ {src_path.name} → .bak  |  new={final_path.name} ({out_mb:.0f}MB)')
    _mark_done(src)
    with ctr_lock:
        counters['ok'] += 1
        counters['ok_by_type'][issue] = counters['ok_by_type'].get(issue, 0) + 1
        counters['saved_mb'] += (in_mb - out_mb)
    return True

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='In-place ATV4K codec fixer')
    ap.add_argument('--test',          action='store_true',
                    help='Run 2 files per type (~10 total) then stop')
    ap.add_argument('--dry-run',       action='store_true',
                    help='Preview only — no encoding, no file changes')
    ap.add_argument('--type',          default='',
                    help='Comma list: av1,dts,pgs_vobsub,mjpeg,bad_container_avi,bad_container_ts')
    ap.add_argument('--limit',         type=int, default=0,
                    help='Max files to process (0 = unlimited)')
    ap.add_argument('--workers-io',    type=int, default=12)
    ap.add_argument('--workers-light', type=int, default=6)
    ap.add_argument('--workers-heavy', type=int, default=2)
    ap.add_argument('--skip-av1',      action='store_true',
                    help='Skip AV1 encodes (long-running) — do I/O jobs only')
    args = ap.parse_args()

    _load_state()

    type_filter = set(args.type.split(',')) if args.type else None
    if args.skip_av1:
        type_filter = (type_filter or
                       {'pgs_vobsub','mjpeg','dts','bad_container_avi','bad_container_ts'}) \
                       - {'av1'}

    # ── Load + classify ────────────────────────────────────────────────────────
    data    = json.loads(SCAN_JSON.read_text())
    records = [r for r in data['results']
               if not r.get('is_junk') and not r.get('is_sample')
               and r.get('video') and r.get('duration_min', 0) > 0.5
               and Path(r['path'] + '.bak').exists() is False]

    by_type: Dict[str, List[Dict]] = {}
    for r in records:
        issue = classify(r)
        if issue is None: continue
        if type_filter and issue not in type_filter: continue
        if r['path'] in _completed: continue
        by_type.setdefault(issue, []).append(r)

    # Sort each bucket by size ascending (small files first = faster early feedback)
    for t in by_type:
        by_type[t].sort(key=lambda x: x.get('size_mb', 0))

    # --test: take 2 per type
    if args.test:
        by_type = {t: files[:2] for t, files in by_type.items()}
        log.info('*** TEST MODE: 2 files per type ***')

    # --limit: global cap
    if args.limit:
        capped = {}
        remaining = args.limit
        for t, files in by_type.items():
            take = min(len(files), remaining)
            capped[t] = files[:take]
            remaining -= take
            if remaining <= 0: break
        by_type = capped

    total = sum(len(v) for v in by_type.values())

    # ── Header ─────────────────────────────────────────────────────────────────
    log.info('=' * 70)
    log.info('BATCH OPTIMIZE — In-Place ATV4K Codec Fixer')
    log.info(f'Log (live):  {LOG_LIVE}')
    log.info(f'Log (arch):  {LOG_ARCH}')
    log.info(f'Already done: {len(_completed)} files (skipping)')
    log.info(f'To process:   {total} files')
    log.info(f'Dry run:      {args.dry_run}')
    log.info(f'Test mode:    {args.test}')
    log.info(f'Strategy:     encode to .tmp alongside original → verify → orig→.bak → .tmp→final')
    log.info('-' * 70)
    for t, files in sorted(by_type.items(), key=lambda x: -len(x[1])):
        gb = sum(f['size_mb'] for f in files) / 1024
        log.info(f'  {t:<25} {len(files):>5} files  {gb:>8.1f} GB')
    log.info('=' * 70)

    if total == 0:
        log.info('Nothing to process — all files complete.')
        return

    # ── Thread pools ───────────────────────────────────────────────────────────
    IO_TYPES    = {'pgs_vobsub', 'mjpeg', 'bad_container_avi', 'bad_container_ts'}
    LIGHT_TYPES = {'dts'}
    # av1 → heavy pool

    counters = {'ok': 0, 'fail': 0, 'dry': 0, 'ok_by_type': {}, 'saved_mb': 0.0}
    ctr_lock = threading.Lock()

    futures: Dict = {}
    io_pool    = ThreadPoolExecutor(max_workers=args.workers_io,    thread_name_prefix='io')
    light_pool = ThreadPoolExecutor(max_workers=args.workers_light, thread_name_prefix='light')
    heavy_pool = ThreadPoolExecutor(max_workers=args.workers_heavy, thread_name_prefix='heavy')

    try:
        for itype, files in by_type.items():
            pool = (io_pool    if itype in IO_TYPES    else
                    light_pool if itype in LIGHT_TYPES else
                    heavy_pool)
            for f in files:
                if _stop.is_set(): break
                fut = pool.submit(process_file, f, itype, args, counters, ctr_lock)
                futures[fut] = f['path']
            if _stop.is_set(): break

        done_n = 0
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                ok = fut.result()
            except Exception as e:
                ok = False
                log.error(f'Worker exception ({src}): {e}')
                _mark_fail(src, str(e))
            with ctr_lock:
                if not ok: counters['fail'] += 1
            done_n += 1
            if done_n % 50 == 0 or done_n == total:
                total_done = counters['ok'] + counters['fail'] + counters['dry']
                saved_gb   = counters['saved_mb'] / 1024
                log.info(f'── Progress {total_done}/{total}  '
                         f'✅{counters["ok"]}  ❌{counters["fail"]}  '
                         f'saved≈{saved_gb:.1f}GB ──')

    finally:
        io_pool.shutdown(wait=False)
        light_pool.shutdown(wait=False)
        heavy_pool.shutdown(wait=False)

    # ── Final summary ──────────────────────────────────────────────────────────
    log.info('')
    log.info('=' * 70)
    log.info('BATCH COMPLETE')
    log.info(f'  ✅ Success:  {counters["ok"]}')
    log.info(f'  ❌ Failed:   {counters["fail"]}')
    saved_gb = counters['saved_mb'] / 1024
    log.info(f'  💾 Space saved (new vs bak): {saved_gb:+.1f} GB')
    log.info('')
    log.info('  Results by type:')
    for t, n in sorted(counters['ok_by_type'].items(), key=lambda x: -x[1]):
        log.info(f'    {t:<25} {n} files')
    if _failed:
        log.info(f'\n  ❌ Failed files: {FAIL_FILE}')
    log.info('=' * 70)

if __name__ == '__main__':
    main()
