#!/usr/bin/env python3
"""
scan_library.py — PlexOptimizer
Scans /Volumes/tv and /Volumes/movies for files that will cause Plex to
force a full video transcode when streaming to Apple TV 4K.

Problematic subtitle types (require burn-in → full transcode):
  - ass/ssa    (vector/bitmap format, ATV4K can't render)
  - hdmv_pgs  (Blu-ray PGS image subtitles)
  - dvdsub    (DVD bitmap subtitles)
  - vobsub    (VOB bitmap subtitles)

Usage:
    python scan_library.py [--tv /Volumes/tv] [--movies /Volumes/movies]
                           [--output scan_results.json] [--verbose]
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.table import Table
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None

# ── Constants ────────────────────────────────────────────────────────────────
PLEX_FF = "/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder"
BREW_FF = "/usr/local/bin/ffmpeg"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts"}

# Subtitle codecs that force a full video transcode when streaming to ATV4K
BAD_SUBS = {"ass", "ssa", "hdmv_pgs", "dvdsub", "vobsub", "pgssub"}

# Video codecs that the ATV4K 3rd gen can hardware-decode natively
ATV_NATIVE_VIDEO = {"h264", "hevc", "av1"}

# Audio codecs ATV4K can pass through or decode natively
ATV_NATIVE_AUDIO = {"aac", "ac3", "eac3", "alac", "mp3", "truehd", "dts", "flac"}

# Containers ATV4K supports (mkv always needs at minimum a remux)
ATV_NATIVE_CONTAINERS = {".mp4", ".m4v", ".mov"}

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"scan_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def get_ffprobe() -> str:
    """Return path to best available ffprobe/ffmpeg binary."""
    for candidate in [BREW_FF, PLEX_FF]:
        if os.path.isfile(candidate):
            return candidate
    log.error("No ffmpeg/ffprobe found. Install via: brew install ffmpeg")
    sys.exit(1)


def probe_file(path: str, ffbin: str) -> Optional[Dict]:
    """
    Run ffprobe-style stream detection on a media file.
    Returns dict with video, audio, subtitle stream info, or None on failure.
    """
    try:
        result = subprocess.run(
            [ffbin, "-i", path],
            capture_output=True, text=True, timeout=30
        )
        output = result.stderr  # ffmpeg writes stream info to stderr
        streams = {"video": [], "audio": [], "subtitle": [], "raw": output}

        for line in output.splitlines():
            line = line.strip()
            if "Stream #" not in line:
                continue
            if "Video:" in line:
                codec = _extract_codec(line, "Video:")
                fps = _extract_fps(line)
                res = _extract_resolution(line)
                hdr = _detect_hdr(line)
                streams["video"].append({
                    "codec": codec, "fps": fps,
                    "resolution": res, "hdr": hdr, "raw": line
                })
            elif "Audio:" in line:
                codec = _extract_codec(line, "Audio:")
                lang = _extract_lang(line)
                streams["audio"].append({"codec": codec, "lang": lang, "raw": line})
            elif "Subtitle:" in line:
                codec = _extract_codec(line, "Subtitle:")
                lang = _extract_lang(line)
                streams["subtitle"].append({"codec": codec, "lang": lang, "raw": line})

        # Extract bitrate and duration
        for line in output.splitlines():
            if "Duration:" in line and "bitrate:" in line:
                streams["duration"] = line.strip()

        return streams
    except subprocess.TimeoutExpired:
        log.warning(f"Timeout probing: {path}")
        return None
    except Exception as e:
        log.warning(f"Error probing {path}: {e}")
        return None


def _extract_codec(line: str, marker: str) -> str:
    """Pull codec name from a stream line."""
    try:
        part = line.split(marker, 1)[1].strip()
        return part.split(",")[0].split(" ")[0].split("(")[0].lower().strip()
    except Exception:
        return "unknown"


def _extract_fps(line: str) -> Optional[str]:
    for token in line.split(","):
        if "fps" in token:
            return token.strip().split(" ")[0]
    return None


def _extract_resolution(line: str) -> Optional[str]:
    import re
    m = re.search(r'(\d{3,5}x\d{3,5})', line)
    return m.group(1) if m else None


def _detect_hdr(line: str) -> bool:
    hdr_markers = ["bt2020", "smpte2084", "arib-std-b67", "hdr10", "dolby_vision"]
    return any(m in line.lower() for m in hdr_markers)


def _extract_lang(line: str) -> str:
    import re
    m = re.search(r'Stream #\d+:\d+\((\w+)\)', line)
    return m.group(1) if m else "und"


def classify_file(path: str, streams: Dict) -> Dict:
    """
    Determine what Plex will do with this file when streaming to ATV4K.
    Returns a classification dict.
    """
    ext = Path(path).suffix.lower()
    issues = []
    actions_needed = []

    # --- Subtitle issues ---
    bad_sub_codecs = []
    for s in streams.get("subtitle", []):
        codec = s["codec"]
        if codec in BAD_SUBS:
            bad_sub_codecs.append(codec)

    if bad_sub_codecs:
        issues.append(f"BAD_SUBS:{','.join(sorted(set(bad_sub_codecs)))}")
        actions_needed.append("convert_subs")

    # --- Video codec ---
    vid_codec = streams["video"][0]["codec"] if streams.get("video") else "unknown"
    vid_hdr = streams["video"][0]["hdr"] if streams.get("video") else False

    # --- Audio codec ---
    aud_codec = streams["audio"][0]["codec"] if streams.get("audio") else "unknown"
    if aud_codec not in ATV_NATIVE_AUDIO:
        issues.append(f"BAD_AUDIO:{aud_codec}")
        actions_needed.append("convert_audio")

    # --- Container ---
    if ext == ".mkv":
        issues.append("MKV_CONTAINER")
        # Only flag as needing action if it also has bad subs or bad video
        # Pure MKV+H264+SRT is fine as a remux target

    # --- Severity ---
    if "convert_subs" in actions_needed:
        severity = "HIGH"   # Will crash / force full transcode
    elif "convert_audio" in actions_needed:
        severity = "MEDIUM"
    elif "MKV_CONTAINER" in issues:
        severity = "LOW"    # Just needs a remux
    else:
        severity = "OK"

    return {
        "path": path,
        "size_bytes": os.path.getsize(path),
        "ext": ext,
        "video_codec": vid_codec,
        "video_hdr": vid_hdr,
        "audio_codec": aud_codec,
        "bad_sub_codecs": sorted(set(bad_sub_codecs)),
        "all_sub_codecs": [s["codec"] for s in streams.get("subtitle", [])],
        "issues": issues,
        "actions_needed": actions_needed,
        "severity": severity,
        "resolution": streams["video"][0].get("resolution") if streams.get("video") else None,
    }


def scan_directory(root: str, ffbin: str, verbose: bool = False) -> List[Dict]:
    """Walk a directory tree and classify all video files."""
    results = []
    all_files = []

    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if Path(fname).suffix.lower() in VIDEO_EXTS and not fname.startswith("."):
                all_files.append(os.path.join(dirpath, fname))

    log.info(f"Found {len(all_files)} video files in {root}")

    for i, fpath in enumerate(all_files):
        if verbose or (i % 50 == 0):
            log.info(f"  [{i+1}/{len(all_files)}] {os.path.basename(fpath)}")

        streams = probe_file(fpath, ffbin)
        if streams is None:
            log.warning(f"  Skipping (probe failed): {fpath}")
            continue

        classification = classify_file(fpath, streams)
        results.append(classification)

    return results


def print_summary(results: List[Dict]) -> None:
    """Print a human-readable summary of scan results."""
    high = [r for r in results if r["severity"] == "HIGH"]
    medium = [r for r in results if r["severity"] == "MEDIUM"]
    low = [r for r in results if r["severity"] == "LOW"]
    ok = [r for r in results if r["severity"] == "OK"]

    print(f"\n{'='*70}")
    print(f"  SCAN COMPLETE — {len(results)} files analysed")
    print(f"{'='*70}")
    print(f"  🔴 HIGH   (bad subs → full transcode crash): {len(high)}")
    print(f"  🟠 MEDIUM (bad audio):                       {len(medium)}")
    print(f"  🟡 LOW    (mkv container, remux only):       {len(low)}")
    print(f"  ✅ OK:                                       {len(ok)}")
    print(f"{'='*70}")

    if high:
        print(f"\n  🔴 HIGH PRIORITY FILES ({len(high)}):")
        for r in sorted(high, key=lambda x: x["path"]):
            subs = ",".join(r["bad_sub_codecs"])
            size_mb = r["size_bytes"] // (1024*1024)
            print(f"    [{subs}] [{r['video_codec']}] {size_mb}MB  {r['path']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Plex libraries for Apple TV 4K streaming problems"
    )
    parser.add_argument("--tv", default="/Volumes/tv",
                        help="Path to TV library (default: /Volumes/tv)")
    parser.add_argument("--movies", default="/Volumes/movies",
                        help="Path to movies library (default: /Volumes/movies)")
    parser.add_argument("--output", default="scan_results.json",
                        help="Output JSON file (default: scan_results.json)")
    parser.add_argument("--verbose", action="store_true",
                        help="Log every file, not just every 50th")
    args = parser.parse_args()

    ffbin = get_ffprobe()
    log.info(f"Using binary: {ffbin}")

    all_results: List[Dict] = []

    for lib_path in [args.tv, args.movies]:
        if os.path.isdir(lib_path):
            log.info(f"\nScanning: {lib_path}")
            results = scan_directory(lib_path, ffbin, args.verbose)
            all_results.extend(results)
        else:
            log.warning(f"Library not found, skipping: {lib_path}")

    output_path = Path(__file__).parent / args.output
    with open(output_path, "w") as f:
        json.dump({
            "scan_date": datetime.now().isoformat(),
            "total_files": len(all_results),
            "results": all_results
        }, f, indent=2)

    log.info(f"\nResults saved to: {output_path}")
    print_summary(all_results)


if __name__ == "__main__":
    main()
