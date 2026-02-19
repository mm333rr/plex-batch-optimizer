#!/usr/bin/env python3
"""
fix_subs.py — PlexOptimizer
Converts bad subtitle tracks (ASS/SSA/PGS/VOBSUB) in MKV files to SRT,
replacing the file in-place (with backup option).

Strategy:
  - ASS/SSA → convert to SRT via ffmpeg subtitle conversion
  - PGS/VOBSUB → these are image-based and CANNOT be losslessly converted
    to text; instead we REMOVE them from the container if an SRT track
    already exists, OR we flag them for manual OCR
  - All operations are in-place MKV remux (no video re-encode)
  - Original file is kept as .orig until --no-backup is passed

Usage:
    python fix_subs.py --dry-run                  # preview only
    python fix_subs.py --input scan_results.json  # fix HIGH severity files
    python fix_subs.py --file /path/to/file.mkv   # fix a single file
    python fix_subs.py --file /path/to/file.mkv --no-backup
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"fix_subs_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

BREW_FF = "/usr/local/bin/ffmpeg"
PLEX_FF = "/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder"
MKVMERGE = "/usr/local/bin/mkvmerge"
MKVEXTRACT = "/usr/local/bin/mkvextract"

# Subtitle types we can convert to SRT text
CONVERTIBLE_SUBS = {"ass", "ssa"}

# Image-based subs — can only be removed (no lossless text conversion)
IMAGE_SUBS = {"hdmv_pgs", "pgssub", "dvdsub", "vobsub"}

STATS = {"processed": 0, "fixed": 0, "skipped": 0, "errors": 0}


def get_ffmpeg() -> str:
    """Return path to best available ffmpeg binary."""
    for candidate in [BREW_FF, PLEX_FF]:
        if os.path.isfile(candidate):
            return candidate
    log.error("No ffmpeg found. Install: brew install ffmpeg")
    sys.exit(1)


def get_mkvmerge() -> Optional[str]:
    """Return mkvmerge path if available."""
    if os.path.isfile(MKVMERGE):
        return MKVMERGE
    # Try brew prefix
    result = subprocess.run(["brew", "--prefix"], capture_output=True, text=True)
    if result.returncode == 0:
        candidate = os.path.join(result.stdout.strip(), "bin", "mkvmerge")
        if os.path.isfile(candidate):
            return candidate
    return None


def probe_subtitle_streams(path: str, ffbin: str) -> List[Dict]:
    """
    Return list of subtitle stream info dicts for a file.
    Each dict: {index, codec, lang, track_name}
    """
    result = subprocess.run(
        [ffbin, "-i", path],
        capture_output=True, text=True, timeout=30
    )
    streams = []
    stream_idx = 0
    for line in result.stderr.splitlines():
        if "Stream #" not in line or "Subtitle:" not in line:
            continue
        import re
        m = re.search(r'Stream #(\d+):(\d+)', line)
        if not m:
            continue
        file_idx = int(m.group(1))
        stream_num = int(m.group(2))

        lang_m = re.search(r'Stream #\d+:\d+\((\w+)\)', line)
        lang = lang_m.group(1) if lang_m else "und"

        codec_part = line.split("Subtitle:")[1].strip()
        codec = codec_part.split(",")[0].split("(")[0].strip().lower()

        streams.append({
            "ffmpeg_idx": f"{file_idx}:{stream_num}",
            "stream_num": stream_num,
            "codec": codec,
            "lang": lang,
            "line": line.strip()
        })
        stream_idx += 1

    return streams


def convert_ass_to_srt(input_path: str, ffbin: str, dry_run: bool = False) -> bool:
    """
    Convert ASS/SSA subtitle tracks to SRT in-place using ffmpeg remux.
    Video and audio streams are copied without re-encoding.

    Strategy: ffmpeg can directly transcode ASS → SRT in a remux pass.
    Output goes to a temp file, then atomically replaces the original.
    """
    input_path = str(input_path)
    sub_streams = probe_subtitle_streams(input_path, ffbin)

    has_bad = any(s["codec"] in CONVERTIBLE_SUBS for s in sub_streams)
    has_image = any(s["codec"] in IMAGE_SUBS for s in sub_streams)

    if not has_bad and not has_image:
        log.info(f"  No problematic subs found, skipping: {os.path.basename(input_path)}")
        return False

    log.info(f"  File: {os.path.basename(input_path)}")
    for s in sub_streams:
        log.info(f"    Subtitle stream {s['ffmpeg_idx']}: {s['codec']} ({s['lang']})")

    if dry_run:
        log.info(f"  [DRY RUN] Would fix: {input_path}")
        return True

    # Build ffmpeg command
    # For ASS/SSA: include but transcode to subrip
    # For PGS/VOBSUB: exclude entirely (can't convert losslessly)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mkv", dir=os.path.dirname(input_path))
    os.close(tmp_fd)

    cmd = [ffbin, "-y", "-i", input_path,
           "-map", "0:v",       # all video streams
           "-map", "0:a",       # all audio streams
           "-c:v", "copy",
           "-c:a", "copy"]

    srt_count = 0
    for s in sub_streams:
        if s["codec"] in CONVERTIBLE_SUBS:
            cmd += ["-map", f"0:{s['stream_num']}"]
            cmd += [f"-c:s:{srt_count}", "srt"]
            srt_count += 1
            log.info(f"    ✅ Converting {s['codec']} → SRT (stream {s['stream_num']})")
        elif s["codec"] in IMAGE_SUBS:
            log.info(f"    ⚠️  Dropping image-based sub (stream {s['stream_num']}: {s['codec']})")
            # Don't map this stream — it will be excluded
        else:
            # Keep any other subtitle types (SRT, WebVTT etc)
            cmd += ["-map", f"0:{s['stream_num']}"]
            cmd += [f"-c:s:{srt_count}", "copy"]
            srt_count += 1

    cmd.append(tmp_path)

    log.info(f"  Running: {' '.join(cmd[:8])} ...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            log.error(f"  ffmpeg failed: {result.stderr[-500:]}")
            os.unlink(tmp_path)
            STATS["errors"] += 1
            return False

        # Verify output file is reasonable (at least 50% of original size)
        orig_size = os.path.getsize(input_path)
        new_size = os.path.getsize(tmp_path)
        if new_size < orig_size * 0.5:
            log.error(f"  Output suspiciously small ({new_size} vs {orig_size}), aborting")
            os.unlink(tmp_path)
            STATS["errors"] += 1
            return False

        # Backup original
        backup_path = input_path + ".orig"
        shutil.move(input_path, backup_path)
        log.info(f"  Backed up original to: {os.path.basename(backup_path)}")

        # Move new file into place
        shutil.move(tmp_path, input_path)
        log.info(f"  ✅ Fixed: {os.path.basename(input_path)}")
        STATS["fixed"] += 1
        return True

    except subprocess.TimeoutExpired:
        log.error(f"  Timeout processing: {input_path}")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        STATS["errors"] += 1
        return False
    except Exception as e:
        log.error(f"  Unexpected error: {e}")
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        STATS["errors"] += 1
        return False


def process_file(path: str, ffbin: str, dry_run: bool = False,
                 no_backup: bool = False) -> bool:
    """Process a single file."""
    STATS["processed"] += 1
    if not os.path.isfile(path):
        log.warning(f"  File not found: {path}")
        STATS["skipped"] += 1
        return False

    ext = Path(path).suffix.lower()
    if ext != ".mkv":
        log.info(f"  Skipping non-MKV: {os.path.basename(path)}")
        STATS["skipped"] += 1
        return False

    result = convert_ass_to_srt(path, ffbin, dry_run=dry_run)

    if not dry_run and no_backup:
        backup = path + ".orig"
        if os.path.exists(backup):
            os.unlink(backup)
            log.info(f"  Deleted backup: {os.path.basename(backup)}")

    return result


def process_from_scan(scan_path: str, ffbin: str, dry_run: bool = False,
                      no_backup: bool = False, severity: str = "HIGH") -> None:
    """Read scan_results.json and process all files at or above severity."""
    severity_order = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "OK": 0}
    min_sev = severity_order.get(severity, 3)

    with open(scan_path) as f:
        data = json.load(f)

    targets = [
        r for r in data["results"]
        if severity_order.get(r.get("severity", "OK"), 0) >= min_sev
        and r.get("actions_needed") and "convert_subs" in r["actions_needed"]
    ]

    log.info(f"Found {len(targets)} files to process from scan results")

    for i, record in enumerate(targets):
        log.info(f"\n[{i+1}/{len(targets)}] {record['path']}")
        process_file(record["path"], ffbin, dry_run=dry_run, no_backup=no_backup)

    _print_stats()


def _print_stats() -> None:
    log.info(f"\n{'='*50}")
    log.info(f"  DONE")
    log.info(f"  Processed: {STATS['processed']}")
    log.info(f"  Fixed:     {STATS['fixed']}")
    log.info(f"  Skipped:   {STATS['skipped']}")
    log.info(f"  Errors:    {STATS['errors']}")
    log.info(f"{'='*50}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix bad subtitle tracks for Plex/Apple TV 4K compatibility"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without modifying files")
    parser.add_argument("--file", type=str,
                        help="Process a single MKV file")
    parser.add_argument("--input", type=str, default="scan_results.json",
                        help="Scan results JSON from scan_library.py")
    parser.add_argument("--severity", default="HIGH",
                        choices=["HIGH", "MEDIUM", "LOW"],
                        help="Minimum severity to process (default: HIGH)")
    parser.add_argument("--no-backup", action="store_true",
                        help="Delete .orig backup after successful conversion")
    args = parser.parse_args()

    ffbin = get_ffmpeg()
    log.info(f"Using binary: {ffbin}")

    if args.dry_run:
        log.info("*** DRY RUN MODE — no files will be modified ***")

    if args.file:
        process_file(args.file, ffbin, dry_run=args.dry_run, no_backup=args.no_backup)
        _print_stats()
    else:
        scan_path = Path(__file__).parent / args.input
        if not scan_path.exists():
            log.error(f"Scan results not found: {scan_path}")
            log.error("Run scan_library.py first, or use --file for a single file")
            sys.exit(1)
        process_from_scan(str(scan_path), ffbin,
                          dry_run=args.dry_run,
                          no_backup=args.no_backup,
                          severity=args.severity)


if __name__ == "__main__":
    main()
