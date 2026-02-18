#!/usr/bin/env python3
"""
media_scan.py — Parallel media audit for Plex/AppleTV 4K compatibility.
Scans /Volumes/tv and /Volumes/movies using ffprobe, saves JSON + reports.

Target: mMacPro (MacPro6,1) → Plex → Apple TV 4K via VideoToolbox
Usage:
  python3 media_scan.py [--dry-run] [--workers N] [--dir /path]
  python3 media_scan.py --report   # just print report from saved results
"""

import os
import sys
import json
import subprocess
import argparse
import logging
import signal
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, List, Dict, Any

# ── Config ────────────────────────────────────────────────────────────────────
SCAN_DIRS = ["/Volumes/tv", "/Volumes/movies"]
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv",
              ".flv", ".webm", ".mpg", ".mpeg", ".m2ts", ".vob"}
JUNK_EXTS  = {".nfo", ".nzb", ".sfv", ".srr", ".jpg", ".jpeg", ".png",
              ".txt", ".url", ".lnk", ".db", ".DS_Store", ".idx"}
SAMPLE_PAT = ("sample", "trailer", "featurette", "behind", "extra-",
              "extras-", "-extra.", "-trailer.")

OUTPUT_DIR = Path(__file__).parent / "results"
LOG_FILE   = OUTPUT_DIR / f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
RESULTS_FILE = OUTPUT_DIR / "scan_results.json"
PROGRESS_FILE = OUTPUT_DIR / "scan_progress.json"

# ── Apple TV 4K + Plex Compatibility Matrix ───────────────────────────────────
# Direct Play = no transcode needed. Transcode = CPU/GPU work.
DIRECT_PLAY_CODECS = {"h264", "hevc", "mpeg4", "vp9"}  # ATV4K supports natively
DIRECT_PLAY_CONTAINERS = {".mkv", ".mp4", ".m4v", ".mov"}
DIRECT_AUDIO = {"aac", "ac3", "eac3", "mp3", "alac", "flac", "opus"}
TRANSCODE_AUDIO = {"dts", "truehd", "mlp", "dts-hd", "dtshd"}  # ATV can't decode these
PROBLEMATIC_SUB = {"pgs", "dvd_subtitle", "hdmv_pgs_subtitle"}  # image subs need burn-in
TEXT_SUB_OK = {"subrip", "ass", "ssa", "webvtt", "mov_text"}

HDR_TYPES = {"smpte2084", "arib-std-b67"}  # PQ and HLG

# ── Logging ───────────────────────────────────────────────────────────────────
OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

_shutdown = False
def _handle_sigint(sig, frame):
    global _shutdown
    log.warning("Interrupt received — finishing in-flight files then saving...")
    _shutdown = True
signal.signal(signal.SIGINT, _handle_sigint)

# ── ffprobe ───────────────────────────────────────────────────────────────────
def probe_file(path: str) -> Optional[Dict[str, Any]]:
    """Run ffprobe on a file, return parsed JSON or None on error."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        log.debug(f"ffprobe failed on {path}: {e}")
        return None

def analyze_file(path: str) -> Dict[str, Any]:
    """Analyze a single media file and return compatibility assessment."""
    result = {
        "path": path,
        "size_mb": round(os.path.getsize(path) / 1024 / 1024, 1),
        "container": Path(path).suffix.lower(),
        "video": [],
        "audio": [],
        "subtitles": [],
        "issues": [],
        "verdict": "unknown",
        "is_junk": False,
        "is_sample": False,
    }

    fname = Path(path).name.lower()
    if any(p in fname for p in SAMPLE_PAT):
        result["is_sample"] = True
        result["issues"].append("SAMPLE/TRAILER file — candidate for deletion")
        result["verdict"] = "junk"
        return result

    data = probe_file(path)
    if not data:
        result["issues"].append("ffprobe failed — file may be corrupt")
        result["verdict"] = "error"
        return result

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    result["duration_min"] = round(float(fmt.get("duration", 0)) / 60, 1)

    issues = []
    needs_transcode = False
    can_direct_play = True

    for s in streams:
        codec_type = s.get("codec_type", "")
        codec_name = s.get("codec_name", "").lower()
        codec_tag  = s.get("codec_tag_string", "")

        if codec_type == "video":
            width  = s.get("width", 0)
            height = s.get("height", 0)
            fps_str = s.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                fps = round(float(num) / float(den), 2)
            except Exception:
                fps = 0.0
            color_transfer = s.get("color_transfer", "")
            is_hdr = color_transfer in HDR_TYPES
            profile = s.get("profile", "")
            level   = s.get("level", 0)
            bit_depth = s.get("bits_per_raw_sample", s.get("pix_fmt", ""))

            vinfo = {
                "codec": codec_name,
                "profile": profile,
                "level": level,
                "width": width,
                "height": height,
                "fps": fps,
                "hdr": is_hdr,
                "color_transfer": color_transfer,
                "bit_depth": bit_depth,
            }
            result["video"].append(vinfo)

            # Compatibility checks
            if codec_name not in DIRECT_PLAY_CODECS:
                can_direct_play = False
                needs_transcode = True
                issues.append(f"VIDEO: {codec_name} not natively supported on ATV4K — needs transcode")

            if codec_name == "h264":
                if isinstance(level, int) and level > 51:
                    issues.append(f"H.264 level {level} too high for some ATV4K direct play")
                if "10" in str(bit_depth):
                    issues.append("H.264 10-bit — ATV4K may not direct play, transcode likely")
                    needs_transcode = True

            if codec_name == "hevc" and is_hdr:
                issues.append("HEVC HDR — ATV4K supports HDR10, check for Dolby Vision")

            if width >= 3840:
                issues.append(f"4K content ({width}×{height}) — verify ATV4K direct play eligibility")

        elif codec_type == "audio":
            lang  = s.get("tags", {}).get("language", "und")
            channels = s.get("channels", 0)
            ainfo = {"codec": codec_name, "lang": lang, "channels": channels}
            result["audio"].append(ainfo)

            if codec_name in TRANSCODE_AUDIO:
                needs_transcode = True
                issues.append(f"AUDIO: {codec_name} ({lang}) needs transcode — ATV4K can't decode natively")
            elif codec_name not in DIRECT_AUDIO:
                issues.append(f"AUDIO: {codec_name} unknown compatibility — may need transcode")

        elif codec_type == "subtitle":
            lang   = s.get("tags", {}).get("language", "und")
            sinfo  = {"codec": codec_name, "lang": lang}
            result["subtitles"].append(sinfo)
            if codec_name in PROBLEMATIC_SUB:
                needs_transcode = True
                issues.append(f"SUBTITLE: {codec_name} ({lang}) is image-based — Plex must burn-in (CPU intensive)")

    # Container check
    if result["container"] not in DIRECT_PLAY_CONTAINERS:
        can_direct_play = False
        needs_transcode = True
        issues.append(f"CONTAINER: {result['container']} — not ideal, prefer MKV or MP4")

    # Verdict
    if not issues:
        result["verdict"] = "direct_play"
    elif needs_transcode:
        result["verdict"] = "transcode_required"
        result["issues"] = issues
    else:
        result["verdict"] = "direct_play_with_warnings"
        result["issues"] = issues

    return result

# ── File discovery ────────────────────────────────────────────────────────────
def find_files(scan_dirs: List[str]) -> tuple:
    """Return (video_files, junk_files) lists."""
    video_files = []
    junk_files  = []
    log.info(f"Discovering files in: {scan_dirs}")
    for base in scan_dirs:
        for root, dirs, files in os.walk(base):
            # skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(root, fname)
                ext = Path(fname).suffix.lower()
                if ext in VIDEO_EXTS:
                    video_files.append(fpath)
                elif ext in JUNK_EXTS:
                    junk_files.append(fpath)
    log.info(f"Found {len(video_files)} video files, {len(junk_files)} junk-extension files")
    return video_files, junk_files

# ── Reporting ─────────────────────────────────────────────────────────────────
def print_report(results: List[Dict], junk_files: List[str]) -> None:
    """Print a summary report to stdout and log."""
    total = len(results)
    direct_play = [r for r in results if r["verdict"] == "direct_play"]
    transcode   = [r for r in results if r["verdict"] == "transcode_required"]
    warnings    = [r for r in results if r["verdict"] == "direct_play_with_warnings"]
    errors      = [r for r in results if r["verdict"] in ("error", "unknown")]
    samples     = [r for r in results if r.get("is_sample")]

    log.info("=" * 70)
    log.info("MEDIA AUDIT REPORT — mMacPro → Plex → Apple TV 4K")
    log.info("=" * 70)
    log.info(f"Total video files scanned : {total}")
    log.info(f"  Direct Play (no work)   : {len(direct_play)}")
    log.info(f"  Transcode Required      : {len(transcode)}")
    log.info(f"  Direct Play w/ Warnings : {len(warnings)}")
    log.info(f"  Errors / Unknown        : {len(errors)}")
    log.info(f"  Sample/Junk video files : {len(samples)}")
    log.info(f"Junk extension files      : {len(junk_files)}")
    log.info("")

    if transcode:
        log.info("── FILES REQUIRING TRANSCODE ─────────────────────────────────────")
        for r in transcode:
            log.info(f"  {r['path']}")
            for issue in r["issues"]:
                log.info(f"    ⚠ {issue}")
        log.info("")

    if warnings:
        log.info("── DIRECT PLAY WITH WARNINGS ─────────────────────────────────────")
        for r in warnings:
            log.info(f"  {r['path']}")
            for issue in r["issues"]:
                log.info(f"    ⚡ {issue}")
        log.info("")

    if samples:
        log.info("── SAMPLE/TRAILER FILES (safe to delete) ─────────────────────────")
        for r in samples:
            log.info(f"  {r['path']} ({r['size_mb']} MB)")
        log.info("")

    if junk_files:
        log.info("── JUNK FILES (NFO/JPG/TXT/etc) ──────────────────────────────────")
        for f in junk_files[:50]:
            log.info(f"  {f}")
        if len(junk_files) > 50:
            log.info(f"  ... and {len(junk_files)-50} more (see scan_results.json)")
        log.info("")

    if errors:
        log.info("── ERRORS / UNREADABLE ───────────────────────────────────────────")
        for r in errors:
            log.info(f"  {r['path']}: {r['issues']}")

    log.info("=" * 70)
    log.info(f"Full results: {RESULTS_FILE}")
    log.info(f"Full log:     {LOG_FILE}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Plex media compatibility scanner")
    parser.add_argument("--dry-run", action="store_true", help="Discover files only, no ffprobe")
    parser.add_argument("--workers", type=int, default=12, help="Parallel workers (default: 12)")
    parser.add_argument("--dir", nargs="+", help="Override scan directories")
    parser.add_argument("--report", action="store_true", help="Print report from saved results only")
    args = parser.parse_args()

    scan_dirs = args.dir or SCAN_DIRS

    if args.report:
        if not RESULTS_FILE.exists():
            print(f"No results file at {RESULTS_FILE} — run a scan first.")
            sys.exit(1)
        data = json.loads(RESULTS_FILE.read_text())
        print_report(data["results"], data.get("junk_files", []))
        return

    log.info(f"Starting media scan — workers={args.workers} dirs={scan_dirs}")
    video_files, junk_files = find_files(scan_dirs)

    if args.dry_run:
        log.info("Dry run — skipping ffprobe analysis")
        return

    results = []
    completed = 0
    start = time.time()

    # Save progress so we can resume or inspect mid-scan
    def save_progress():
        PROGRESS_FILE.write_text(json.dumps({
            "completed": completed,
            "total": len(video_files),
            "elapsed_min": round((time.time() - start) / 60, 1),
            "results_so_far": len(results),
        }, indent=2))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(analyze_file, f): f for f in video_files}
        for future in as_completed(futures):
            if _shutdown:
                log.warning("Shutting down early — saving partial results...")
                pool.shutdown(wait=False, cancel_futures=True)
                break
            completed += 1
            try:
                r = future.result()
                results.append(r)
            except Exception as e:
                log.error(f"Worker error on {futures[future]}: {e}")

            if completed % 100 == 0 or completed == len(video_files):
                elapsed = time.time() - start
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(video_files) - completed) / rate if rate > 0 else 0
                log.info(f"Progress: {completed}/{len(video_files)} ({rate:.1f}/s, ETA {eta/60:.1f} min)")
                save_progress()

    # Save full results
    output = {
        "scan_time": datetime.now().isoformat(),
        "scan_dirs": scan_dirs,
        "total_files": len(video_files),
        "completed": completed,
        "results": results,
        "junk_files": junk_files,
    }
    RESULTS_FILE.write_text(json.dumps(output, indent=2))
    log.info(f"Results saved to {RESULTS_FILE}")
    print_report(results, junk_files)

if __name__ == "__main__":
    main()
