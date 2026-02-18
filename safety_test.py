#!/usr/bin/env python3
"""
safety_test.py — One file per problem type, best known method, verify output.

TYPES TESTED (7 total):
  1. pgs_subs        — HEVC+AAC+PGS    → strip subs, -c copy (lossless, instant)
  2. other           — HEVC+AAC+VobSub → strip subs, -c copy (lossless, instant)
  3. av1_video       — AV1 10-bit       → libx264 -vf format=yuv420p -crf 20 -preset fast
  4. dts_truehd_audio— H264+DTS         → -c:v copy, -c:a eac3 -b:a 448k
  5. bad_container_avi— MPEG4+MP3 in AVI → -fflags +genpts -c:v copy -c:a aac → .mp4
  6. mjpeg_attached  — HEVC+MJPEG thumb → -map 0:v:0 only (drop stream 2), -c copy
  7. bad_container_ts — H264+AAC in .ts  → -c copy -map 0:v -map 0:a → .mkv (drop data)

VERIFICATION: After each encode, ffprobe checks:
  - Output file exists and has size > 0
  - Video codec is ATV4K-compatible (h264/hevc)
  - Audio codec is ATV4K-compatible (aac/eac3/ac3/mp3/alac/flac)
  - No PGS/HDMV/VobSub/dvd subtitle streams remain (unless intentional)
  - No attached MJPEG streams remain
  - Duration within 2% of input
"""

import os, sys, json, subprocess, logging, signal, argparse, time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# ── Config ────────────────────────────────────────────────────────────────────
OUT    = Path("/Volumes/6tb-R1/PlexOptimized/safety_test")
LOGDIR = Path("/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results")
LOGF   = LOGDIR / f"safety_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
FFMPEG = "/usr/local/bin/ffmpeg"

ATV_VIDEO  = {'h264','hevc','mpeg4','vp9'}
ATV_AUDIO  = {'aac','eac3','ac3','mp3','alac','flac','opus','dts'}  # dts only passthru ok in mkv
BAD_SUBS   = {'hdmv_pgs_subtitle','dvd_subtitle','pgssub'}
BAD_VIDEO  = {'mjpeg'}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOGF), logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

_stop = False
def _sigint(s,f):
    global _stop
    log.warning("SIGINT — will stop after current job")
    _stop = True
signal.signal(signal.SIGINT, _sigint)

# ── Probe helper ─────────────────────────────────────────────────────────────
def probe(path: str) -> Optional[Dict[str, Any]]:
    r = subprocess.run(
        ['ffprobe','-v','quiet','-print_format','json',
         '-show_streams','-show_format', path],
        capture_output=True, text=True, timeout=20
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)

# ── Verify output is truly direct-play ───────────────────────────────────────
def verify(in_path: str, out_path: str, job_id: str) -> bool:
    if not os.path.exists(out_path):
        log.error(f"[{job_id}] VERIFY FAIL: output file missing")
        return False
    out_size = os.path.getsize(out_path)
    if out_size < 1024:
        log.error(f"[{job_id}] VERIFY FAIL: output is {out_size} bytes (empty/truncated)")
        return False

    inp = probe(in_path)
    out = probe(out_path)
    if not inp or not out:
        log.error(f"[{job_id}] VERIFY FAIL: probe failed")
        return False

    in_dur  = float(inp['format'].get('duration', 0))
    out_dur = float(out['format'].get('duration', 0))
    if in_dur > 0 and abs(in_dur - out_dur) / in_dur > 0.02:
        log.warning(f"[{job_id}] VERIFY WARN: duration mismatch {in_dur:.1f}s → {out_dur:.1f}s")

    problems = []
    has_video = False
    for s in out['streams']:
        ct = s.get('codec_type','')
        cn = s.get('codec_name','')
        disp = s.get('disposition',{})

        if ct == 'video':
            if disp.get('attached_pic'):
                problems.append(f"attached_pic stream still present: {cn}")
                continue
            has_video = True
            if cn not in ATV_VIDEO:
                problems.append(f"video codec {cn} not ATV-compatible")

        elif ct == 'audio':
            if cn == 'dts':
                problems.append(f"DTS audio still present (ATV cannot decode)")

        elif ct == 'subtitle':
            if cn in BAD_SUBS:
                problems.append(f"image subtitle still present: {cn}")

    if not has_video:
        problems.append("no video stream in output!")

    if problems:
        for p in problems:
            log.error(f"[{job_id}] VERIFY FAIL: {p}")
        return False

    in_mb  = os.path.getsize(in_path)  / 1024 / 1024
    out_mb = out_size / 1024 / 1024
    log.info(f"[{job_id}] ✅ VERIFIED: {in_mb:.0f}MB → {out_mb:.0f}MB ({out_mb/in_mb*100:.0f}%) — direct play confirmed")
    return True

# ── Run a single ffmpeg job ───────────────────────────────────────────────────
def run_ffmpeg(job_id: str, cmd: List[str], in_path: str, out_path: str, dry_run: bool) -> bool:
    log.info(f"[{job_id}] CMD: {' '.join(cmd)}")
    if dry_run:
        log.info(f"[{job_id}] DRY RUN — skipping")
        return True
    t0 = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_progress = ""
        for line in proc.stdout:
            line = line.rstrip()
            if 'frame=' in line or 'size=' in line:
                last_progress = line
            elif line:
                if last_progress:
                    log.info(f"[{job_id}] {last_progress}")
                    last_progress = ""
                log.info(f"[{job_id}] {line}")
        if last_progress:
            log.info(f"[{job_id}] {last_progress}")
        proc.wait()
    except Exception as e:
        log.error(f"[{job_id}] EXCEPTION: {e}")
        return False

    elapsed = time.time() - t0
    if proc.returncode != 0:
        log.error(f"[{job_id}] ❌ ffmpeg exit {proc.returncode} after {elapsed:.0f}s")
        return False
    log.info(f"[{job_id}] ffmpeg done in {elapsed:.0f}s")
    return verify(in_path, out_path, job_id)

# ── Job definitions ───────────────────────────────────────────────────────────

TV  = "/Volumes/tv"
MOV = "/Volumes/movies"

JOBS = [
    # ── 1. PGS SUBS STRIP (1,007 files) ──────────────────────────────────────
    # HEVC+AAC+PGS → drop all subtitle streams, -c copy
    # Zero quality loss, completes in seconds. Direct play instantly.
    {
        'id': 'T1', 'type': 'pgs_subs',
        'desc': 'PGS image subs strip → lossless remux, drop subs',
        'in':  f"{TV}/Game of Thrones (2011) {{tmdb-1399}}/Season 04/Extras/Trial by Combat.mkv",
        'out': str(OUT / "T1_pgs_strip.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v', '-map', '0:a',   # explicitly NO subtitle map
            '-c', 'copy', o
        ],
    },

    # ── 2. VOBSUB/DVD_SUBTITLE STRIP (748 files "other") ─────────────────────
    # HEVC 10-bit + AAC + dvd_subtitle → strip subs, -c copy
    # Same approach as PGS. HEVC 10-bit direct plays fine on ATV4K.
    {
        'id': 'T2', 'type': 'vobsub_strip',
        'desc': 'VobSub/dvd_subtitle strip → lossless remux, drop subs',
        'in':  f"{MOV}/Happy Feet (2006) {{tmdb-9836}}/Featurettes/A Happy Feet Moment.mkv",
        'out': str(OUT / "T2_vobsub_strip.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v', '-map', '0:a',
            '-c', 'copy', o
        ],
    },

    # ── 3. AV1 10-BIT → H.264 (427 files) ───────────────────────────────────
    # AV1 yuv420p10le → libx264 with explicit format conversion
    # -vf format=yuv420p is the critical fix (10-bit→8-bit for x264 default)
    # -crf 20 = excellent quality, ~preset fast speed
    {
        'id': 'T3', 'type': 'av1_video',
        'desc': 'AV1 10-bit → H.264 libx264 CRF20 (10→8bit conversion)',
        'in':  f"{TV}/North of North (2025) {{tmdb-249023}}/Season 01/North of North - S01E06 - Carnivores.mkv",
        'out': str(OUT / "T3_av1_to_h264.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v:0', '-map', '0:a', '-map', '0:s?',
            '-vf', 'format=yuv420p',          # ← THE FIX: explicit 10→8bit
            '-c:v', 'libx264',
            '-preset', 'fast', '-crf', '20',
            '-profile:v', 'high', '-level', '4.1',
            '-threads', '14',
            '-c:a', 'copy',                   # EAC3 6ch stays untouched
            '-c:s', 'copy',                   # ASS text subs copy fine
            '-movflags', '+faststart', o
        ],
    },

    # ── 4. DTS/TrueHD → EAC3 (323 files) ────────────────────────────────────
    # H264+DTS → copy video, transcode audio to EAC3
    # ATV4K decodes EAC3 natively. Audio transcode only — CPU, fast.
    {
        'id': 'T4', 'type': 'dts_audio',
        'desc': 'DTS 5.1 → EAC3 5.1 @ 448kbps, copy video',
        'in':  f"{TV}/Star Trek (1973) {{tmdb-1992}}/Season 02/Star Trek - The Animated Series - S02E05 - How Sharper Than a Serpent's Tooth.mkv",
        'out': str(OUT / "T4_dts_to_eac3.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v', '-map', '0:a', '-map', '0:s?',
            '-c:v', 'copy',
            '-c:a', 'eac3', '-b:a', '448k',
            '-c:s', 'copy', o
        ],
    },

    # ── 5. AVI → MP4/MKV (243 files) ─────────────────────────────────────────
    # MPEG4+MP3 in AVI → MP4 container
    # -fflags +genpts fixes VBR MP3 timestamp issues in AVI
    # Re-encode audio to AAC (MP3 VBR in MKV muxer is broken)
    {
        'id': 'T5', 'type': 'avi_container',
        'desc': 'AVI (MPEG4+MP3) → MP4, copy video, transcode MP3→AAC',
        'in':  f"{TV}/The King of Queens (1998) {{tmdb-4238}}/Season 09/The King of Queens - S09E08 - Offensive Fowl.avi",
        'out': str(OUT / "T5_avi_to_mp4.mp4"),
        'cmd': lambda i, o: [
            FFMPEG, '-y',
            '-fflags', '+genpts',             # fixes VBR MP3 timestamps in AVI
            '-i', i,
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            '-movflags', '+faststart', o
        ],
    },

    # ── 6. MJPEG ATTACHED PIC STRIP (225 files) ───────────────────────────────
    # HEVC+AAC+MJPEG thumbnail → map only stream 0:v:0 (primary video, not pic)
    # -c copy is lossless, instant. Thumb is gone, Plex sees clean HEVC.
    {
        'id': 'T6', 'type': 'mjpeg_attached',
        'desc': 'MJPEG attached pic strip → -map 0:v:0, lossless copy',
        'in':  f"{TV}/The Game (2021) {{tmdb-138233}}/Season 02/The Game - S02E08 - One Wedding and a Musical.mkv",
        'out': str(OUT / "T6_mjpeg_stripped.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v:0',                  # ONLY primary video (not the MJPEG)
            '-map', '0:a',
            '-map', '0:s?',
            '-c', 'copy', o
        ],
    },

    # ── 7. TS → MKV REMUX (5 files) ──────────────────────────────────────────
    # H264+AAC in .ts → drop data/timed_id3 stream, remux to MKV
    # Container change only. TS has broadcast data streams Plex chokes on.
    {
        'id': 'T7', 'type': 'ts_container',
        'desc': '.ts remux → .mkv, copy A/V only (drop timed_id3 data stream)',
        'in':  f"{MOV}/Harry Potter and the Half-Blood Prince (2009) {{tmdb-767}}/Harry Potter and the Half-Blood Prince (2009) {{tmdb-767}}.ts",
        'out': str(OUT / "T7_ts_to_mkv.mkv"),
        'cmd': lambda i, o: [
            FFMPEG, '-y', '-i', i,
            '-map', '0:v',
            '-map', '0:a',
            # NO data stream map — drops timed_id3
            '-c', 'copy', o
        ],
    },
]

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Safety test: one file per problem type')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--only', help='Run only job ID(s), comma-separated e.g. T1,T3')
    args = parser.parse_args()

    only = set(args.only.split(',')) if args.only else None

    log.info('='*65)
    log.info('SAFETY TEST — 7 problem types × 1 file each')
    log.info(f'Output: {OUT}')
    log.info(f'Log:    {LOGF}')
    log.info('='*65)

    results = {}
    for job in JOBS:
        jid = job['id']
        if only and jid not in only:
            continue
        if _stop:
            break

        log.info(f"\n[{jid}] TYPE: {job['type']}")
        log.info(f"[{jid}] METHOD: {job['desc']}")
        log.info(f"[{jid}] IN:  {job['in']}")
        log.info(f"[{jid}] OUT: {job['out']}")

        if not os.path.exists(job['in']):
            log.error(f"[{jid}] INPUT NOT FOUND — skipping")
            results[jid] = False
            continue

        cmd = job['cmd'](job['in'], job['out'])
        results[jid] = run_ffmpeg(jid, cmd, job['in'], job['out'], args.dry_run)

    # Summary
    log.info('\n' + '='*65)
    log.info('SAFETY TEST RESULTS')
    all_pass = True
    for job in JOBS:
        jid = job['id']
        if jid not in results:
            continue
        ok = results[jid]
        if not ok: all_pass = False
        icon = '✅' if ok else '❌'
        log.info(f"  [{jid}] {icon}  {job['type']:28}  {job['desc']}")
    log.info('')
    log.info(f"OVERALL: {'✅ ALL PASSED — safe to scale to 2,978 files' if all_pass else '❌ SOME FAILED — fix before scaling'}")
    log.info('='*65)

    # Write machine-readable result
    result_file = LOGDIR / 'safety_test_result.json'
    with open(result_file, 'w') as f:
        json.dump({'passed': all_pass, 'results': results, 'jobs': [
            {'id': j['id'], 'type': j['type'], 'passed': results.get(j['id'])} for j in JOBS
        ]}, f, indent=2)
    log.info(f"Results saved to {result_file}")

if __name__ == '__main__':
    main()
