#!/usr/bin/env python3
"""
optimize_video.py — PlexOptimizer
Tests and runs video conversion/repackaging strategies optimized for
the Mac Pro 6,1 (dual AMD FirePro GPUs, macOS 12.7) → Apple TV 4K 3rd gen.

GPU CAPABILITIES — Mac Pro 6,1 / macOS 12.7.6:
  • AMD FirePro D500/D700 (GCN 1.0/Tahiti/Hawaii)
  • VideoToolbox (VT): h264_videotoolbox, hevc_videotoolbox (software fallback)
  • OpenCL: available but Plex doesn't use it for encode on macOS
  • Best encode path: h264_videotoolbox → fastest, GPU-offloaded, ATV4K native

STRATEGIES (in preference order):
  1. REMUX_MP4    — MKV→MP4, copy all streams, fix subs. Zero quality loss.
                    Works if video is already H.264 or HEVC + audio is ATV4K native.
  2. VT_H264      — Re-encode video to H.264 via VideoToolbox GPU encoder.
                    Use for: HEVC-in-MKV with bad subs that can't just be remuxed.
                    Output: MP4, H.264 VT, AAC audio, SRT subs.
  3. VT_HEVC      — Re-encode to HEVC via VideoToolbox. Smaller files, same quality.
                    ATV4K can direct-play HEVC in MP4. Use for quality-critical content.

Usage:
    python optimize_video.py --test                  # benchmark all 3 strategies on a sample
    python optimize_video.py --file episode.mkv      # convert single file
    python optimize_video.py --file episode.mkv --strategy remux_mp4
    python optimize_video.py --file episode.mkv --strategy vt_h264
    python optimize_video.py --file episode.mkv --strategy vt_hevc
    python optimize_video.py --dry-run               # show what would happen
    python optimize_video.py --input scan_results.json --strategy remux_mp4
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"optimize_{datetime.now():%Y%m%d_%H%M%S}.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

BREW_FF = "/usr/local/bin/ffmpeg"
PLEX_FF = "/Applications/Plex Media Server.app/Contents/MacOS/Plex Transcoder"

# VideoToolbox encoder names in ffmpeg
VT_H264 = "h264_videotoolbox"
VT_HEVC = "hevc_videotoolbox"

# Audio codecs ATV4K handles natively (no transcode needed)
ATV_AUDIO_OK = {"aac", "alac", "ac3", "eac3", "mp3", "truehd", "dts"}

# Subtitle codecs that are text-based (safe in MP4/MKV, ATV4K overlay)
GOOD_SUBS = {"subrip", "srt", "webvtt", "mov_text", "ass", "ssa"}

# Image subs that must be dropped or burned
IMAGE_SUBS = {"hdmv_pgs", "pgssub", "dvdsub", "vobsub"}

STATS = {"processed": 0, "converted": 0, "skipped": 0, "errors": 0,
         "orig_bytes": 0, "new_bytes": 0}


# ── Binary discovery ──────────────────────────────────────────────────────────

def get_ffmpeg() -> str:
    """Return best available ffmpeg binary."""
    for candidate in [BREW_FF, PLEX_FF]:
        if os.path.isfile(candidate):
            return candidate
    log.error("ffmpeg not found. Install: brew install ffmpeg")
    sys.exit(1)


def check_videotoolbox(ffbin: str) -> Dict[str, bool]:
    """Check which VideoToolbox encoders are available."""
    result = subprocess.run(
        [ffbin, "-encoders"], capture_output=True, text=True, timeout=10
    )
    output = result.stdout + result.stderr
    return {
        "h264_videotoolbox": VT_H264 in output,
        "hevc_videotoolbox": VT_HEVC in output,
    }


# ── Stream probing ────────────────────────────────────────────────────────────

def probe_streams(path: str, ffbin: str) -> Dict:
    """Return detailed stream info for a media file."""
    result = subprocess.run(
        [ffbin, "-i", path],
        capture_output=True, text=True, timeout=30
    )
    output = result.stderr
    info = {"video": [], "audio": [], "subtitle": [], "duration_s": None}

    import re
    # Duration
    dur_m = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', output)
    if dur_m:
        h, m, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
        info["duration_s"] = h * 3600 + m * 60 + s

    for line in output.splitlines():
        if "Stream #" not in line:
            continue

        # Stream index
        idx_m = re.search(r'Stream #(\d+):(\d+)', line)
        if not idx_m:
            continue
        file_i, stream_i = int(idx_m.group(1)), int(idx_m.group(2))

        lang_m = re.search(r'Stream #\d+:\d+\((\w+)\)', line)
        lang = lang_m.group(1) if lang_m else "und"

        if "Video:" in line:
            codec_raw = line.split("Video:")[1].strip().split(",")[0]
            codec = codec_raw.split("(")[0].strip().lower()
            res_m = re.search(r'(\d{3,5}x\d{3,5})', line)
            hdr = any(x in line.lower() for x in ["bt2020", "smpte2084", "hdr10"])
            info["video"].append({
                "file_i": file_i, "stream_i": stream_i,
                "codec": codec, "lang": lang,
                "resolution": res_m.group(1) if res_m else None,
                "hdr": hdr
            })
        elif "Audio:" in line:
            codec_raw = line.split("Audio:")[1].strip().split(",")[0]
            codec = codec_raw.split("(")[0].strip().lower()
            info["audio"].append({
                "file_i": file_i, "stream_i": stream_i,
                "codec": codec, "lang": lang
            })
        elif "Subtitle:" in line:
            codec_raw = line.split("Subtitle:")[1].strip().split(",")[0]
            codec = codec_raw.split("(")[0].strip().lower()
            info["subtitle"].append({
                "file_i": file_i, "stream_i": stream_i,
                "codec": codec, "lang": lang
            })

    return info


# ── Strategy: REMUX_MP4 ───────────────────────────────────────────────────────

def strategy_remux_mp4(src: str, dst: str, streams: Dict,
                       ffbin: str, dry_run: bool = False) -> bool:
    """
    Remux MKV → MP4. Copy all streams. Convert ASS→mov_text, drop PGS.
    Zero re-encode. Fastest possible. Works when video is already H.264/HEVC.

    Note: mov_text (MP4 subtitle format) is basic SRT-equivalent that ATV4K
    renders natively. ASS styling is lost but text is preserved.
    """
    log.info(f"  Strategy: REMUX_MP4 (copy video+audio, convert text subs)")

    if dry_run:
        log.info(f"  [DRY RUN] {src} → {dst}")
        return True

    cmd = [ffbin, "-y", "-i", src,
           "-map", "0:v", "-c:v", "copy",
           "-map", "0:a", "-c:a", "copy"]

    sub_out_idx = 0
    for s in streams["subtitle"]:
        codec = s["codec"]
        si = s["stream_i"]
        if codec in IMAGE_SUBS:
            log.info(f"    Dropping image sub stream {si} ({codec})")
            continue
        # Text subs: convert to mov_text for MP4 compatibility
        cmd += ["-map", f"0:{si}", f"-c:s:{sub_out_idx}", "mov_text"]
        sub_out_idx += 1
        log.info(f"    Text sub stream {si} ({codec}) → mov_text")

    cmd += ["-movflags", "+faststart", dst]
    return _run_ffmpeg(cmd, src, dst)


# ── Strategy: VT_H264 ────────────────────────────────────────────────────────

def strategy_vt_h264(src: str, dst: str, streams: Dict,
                     ffbin: str, dry_run: bool = False,
                     quality: int = 65) -> bool:
    """
    Re-encode video to H.264 via VideoToolbox GPU encoder.
    Audio: copy if ATV4K-compatible, else transcode to AAC.
    Subs: ASS→mov_text, PGS dropped.

    quality: VideoToolbox quality 0-100 (higher = better). 65 ≈ CRF 20 equivalent.
    This is GPU-offloaded on the AMD FirePro via macOS VideoToolbox framework.
    """
    log.info(f"  Strategy: VT_H264 (GPU VideoToolbox H.264, quality={quality})")

    if dry_run:
        log.info(f"  [DRY RUN] {src} → {dst}")
        return True

    vid = streams["video"][0] if streams["video"] else None
    if not vid:
        log.error("  No video stream found")
        return False

    cmd = [ffbin, "-y", "-i", src,
           "-map", "0:v:0",
           "-c:v", VT_H264,
           "-q:v", str(quality),          # VT quality scale
           "-profile:v", "high",
           "-level", "4.1",               # H.264 High 4.1 — universal ATV4K
           "-pix_fmt", "yuv420p",         # Force 8-bit — widest compatibility
           "-map", "0:a"]

    # Audio: copy if compatible, else AAC
    aud_codec = streams["audio"][0]["codec"] if streams["audio"] else "unknown"
    if aud_codec in ATV_AUDIO_OK:
        cmd += ["-c:a", "copy"]
        log.info(f"    Audio: copy ({aud_codec})")
    else:
        cmd += ["-c:a", "aac", "-b:a", "256k"]
        log.info(f"    Audio: transcode {aud_codec} → AAC 256k")

    # Subtitles
    sub_out_idx = 0
    for s in streams["subtitle"]:
        if s["codec"] in IMAGE_SUBS:
            log.info(f"    Dropping image sub {s['stream_i']} ({s['codec']})")
            continue
        cmd += ["-map", f"0:{s['stream_i']}", f"-c:s:{sub_out_idx}", "mov_text"]
        sub_out_idx += 1

    cmd += ["-movflags", "+faststart", dst]
    return _run_ffmpeg(cmd, src, dst)


# ── Strategy: VT_HEVC ────────────────────────────────────────────────────────

def strategy_vt_hevc(src: str, dst: str, streams: Dict,
                     ffbin: str, dry_run: bool = False,
                     quality: int = 65) -> bool:
    """
    Re-encode to HEVC via VideoToolbox. ~40% smaller files vs H.264 at same quality.
    ATV4K 3rd gen can direct-play HEVC in MP4 natively.
    Note: hevc_videotoolbox may fall back to software on older AMD GPUs.
    """
    log.info(f"  Strategy: VT_HEVC (VideoToolbox HEVC, quality={quality})")

    if dry_run:
        log.info(f"  [DRY RUN] {src} → {dst}")
        return True

    cmd = [ffbin, "-y", "-i", src,
           "-map", "0:v:0",
           "-c:v", VT_HEVC,
           "-q:v", str(quality),
           "-tag:v", "hvc1",            # Apple-compatible HEVC tag
           "-pix_fmt", "yuv420p",
           "-map", "0:a"]

    aud_codec = streams["audio"][0]["codec"] if streams["audio"] else "unknown"
    if aud_codec in ATV_AUDIO_OK:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "256k"]

    sub_out_idx = 0
    for s in streams["subtitle"]:
        if s["codec"] in IMAGE_SUBS:
            continue
        cmd += ["-map", f"0:{s['stream_i']}", f"-c:s:{sub_out_idx}", "mov_text"]
        sub_out_idx += 1

    cmd += ["-movflags", "+faststart", dst]
    return _run_ffmpeg(cmd, src, dst)


# ── Core runner ───────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: List[str], src: str, dst: str,
                timeout: int = 7200) -> bool:
    """Run an ffmpeg command, validate output, return success bool."""
    log.info(f"  CMD: {' '.join(cmd[:10])} ...")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - t0

        if result.returncode != 0:
            log.error(f"  ffmpeg failed (rc={result.returncode})")
            # Print last 20 lines of stderr for diagnosis
            for line in result.stderr.splitlines()[-20:]:
                log.error(f"  > {line}")
            if os.path.exists(dst):
                os.unlink(dst)
            STATS["errors"] += 1
            return False

        orig_size = os.path.getsize(src)
        new_size = os.path.getsize(dst)
        ratio = new_size / orig_size if orig_size else 0

        if ratio < 0.1:
            log.error(f"  Output suspiciously small ({new_size:,} vs {orig_size:,}), aborting")
            os.unlink(dst)
            STATS["errors"] += 1
            return False

        speed = ""
        # Estimate encode speed from stderr
        for line in result.stderr.splitlines():
            if "speed=" in line:
                speed = line.strip().split("speed=")[-1].strip()

        log.info(f"  ✅ Done in {elapsed:.1f}s | {orig_size//1024//1024}MB → "
                 f"{new_size//1024//1024}MB ({ratio:.1%}) | speed={speed}")

        STATS["orig_bytes"] += orig_size
        STATS["new_bytes"] += new_size
        STATS["converted"] += 1
        return True

    except subprocess.TimeoutExpired:
        log.error(f"  Timeout after {timeout}s: {src}")
        if os.path.exists(dst):
            os.unlink(dst)
        STATS["errors"] += 1
        return False


# ── Benchmark / test mode ─────────────────────────────────────────────────────

def run_benchmark(ffbin: str) -> None:
    """
    Find a small TV episode (< 500MB, HEVC/MKV) and test all three strategies.
    Outputs timing and file size comparison.
    Reports which VideoToolbox encoders are actually available.
    """
    log.info("\n" + "="*60)
    log.info("  BENCHMARK MODE — testing all strategies on sample files")
    log.info("="*60)

    # Check VT availability first
    vt = check_videotoolbox(ffbin)
    log.info(f"\nVideoToolbox encoders available:")
    log.info(f"  h264_videotoolbox: {'✅' if vt['h264_videotoolbox'] else '❌'}")
    log.info(f"  hevc_videotoolbox: {'✅' if vt['hevc_videotoolbox'] else '❌'}")

    # Find small HEVC+MKV+ASS episodes (our exact problem files)
    candidates = _find_benchmark_files(ffbin)
    if not candidates:
        log.error("No suitable benchmark files found in /Volumes/tv")
        return

    work_dir = Path(__file__).parent / "work"
    work_dir.mkdir(exist_ok=True)

    results_table = []

    for src_path, info in candidates[:2]:  # Test at most 2 files
        fname = Path(src_path).stem
        size_mb = os.path.getsize(src_path) // (1024 * 1024)
        duration = info.get("duration_s", 0)

        log.info(f"\n{'─'*60}")
        log.info(f"  Test file: {Path(src_path).name}")
        log.info(f"  Size: {size_mb}MB | Duration: {duration:.0f}s")
        log.info(f"  Video: {info['video'][0]['codec']} {info['video'][0].get('resolution')}")
        log.info(f"  Audio: {info['audio'][0]['codec'] if info['audio'] else 'none'}")
        log.info(f"  Subs:  {[s['codec'] for s in info['subtitle']]}")

        strategies = [
            ("remux_mp4", lambda s, d, st: strategy_remux_mp4(s, d, st, ffbin)),
            ("vt_h264", lambda s, d, st: strategy_vt_h264(s, d, st, ffbin)),
        ]
        if vt["hevc_videotoolbox"]:
            strategies.append(
                ("vt_hevc", lambda s, d, st: strategy_vt_hevc(s, d, st, ffbin))
            )

        for strat_name, strat_fn in strategies:
            dst = str(work_dir / f"{fname}_{strat_name}.mp4")
            if os.path.exists(dst):
                os.unlink(dst)

            log.info(f"\n  --- Testing: {strat_name} ---")
            t0 = time.time()
            ok = strat_fn(src_path, dst, info)
            elapsed = time.time() - t0

            if ok and os.path.exists(dst):
                out_mb = os.path.getsize(dst) // (1024 * 1024)
                ratio = out_mb / size_mb if size_mb else 0
                fps = duration / elapsed if elapsed > 0 else 0
                results_table.append({
                    "file": Path(src_path).name,
                    "strategy": strat_name,
                    "in_mb": size_mb,
                    "out_mb": out_mb,
                    "ratio": f"{ratio:.1%}",
                    "time_s": f"{elapsed:.1f}s",
                    "fps": f"{fps:.1f}x realtime"
                })
            else:
                results_table.append({
                    "file": Path(src_path).name,
                    "strategy": strat_name,
                    "in_mb": size_mb,
                    "out_mb": 0, "ratio": "FAILED",
                    "time_s": f"{elapsed:.1f}s", "fps": "—"
                })

    # Print results table
    log.info(f"\n\n{'='*60}")
    log.info("  BENCHMARK RESULTS")
    log.info(f"{'='*60}")
    log.info(f"  {'Strategy':<14} {'In':>6} {'Out':>6} {'Ratio':>7} {'Time':>8} {'Speed':>12}")
    log.info(f"  {'-'*14} {'-'*6} {'-'*6} {'-'*7} {'-'*8} {'-'*12}")
    for r in results_table:
        log.info(f"  {r['strategy']:<14} {r['in_mb']:>5}M {r['out_mb']:>5}M "
                 f"{r['ratio']:>7} {r['time_s']:>8} {r['fps']:>12}")

    # Save results
    bench_path = Path(__file__).parent / "benchmark_results.json"
    with open(bench_path, "w") as f:
        json.dump(results_table, f, indent=2)
    log.info(f"\n  Results saved to: {bench_path}")

    # Recommendation
    log.info(f"\n  RECOMMENDATION for Mac Pro 6,1 → ATV4K:")
    log.info(f"  • H.264 MKV files with only ASS subs → remux_mp4 (fastest, lossless)")
    log.info(f"  • HEVC MKV files with ASS subs       → vt_h264 (GPU encode, great compat)")
    log.info(f"  • Quality-critical content            → vt_hevc (smaller, ATV4K native)")


def _find_benchmark_files(ffbin: str) -> List[Tuple[str, Dict]]:
    """Find small HEVC+MKV+ASS episodes suitable for benchmarking."""
    targets = []
    tv_root = "/Volumes/tv"

    # Focus on Celebrity Traitors — our known problem files, small episodes
    priority_dirs = [
        "/Volumes/tv/The Celebrity Traitors (2025) {tmdb-291431}/Season 01",
        "/Volumes/tv/Carême (2025) {tmdb-231001}/Season 01",
    ]

    for search_dir in priority_dirs:
        if not os.path.isdir(search_dir):
            continue
        for fname in sorted(os.listdir(search_dir)):
            fpath = os.path.join(search_dir, fname)
            if not fname.endswith(".mkv"):
                continue
            size = os.path.getsize(fpath)
            # Pick files under 700MB for fast benchmarking
            if size > 700 * 1024 * 1024:
                continue
            info = probe_streams(fpath, ffbin)
            has_bad_sub = any(
                s["codec"] in {"ass", "ssa", "hdmv_pgs"} for s in info["subtitle"]
            )
            if has_bad_sub and info.get("video"):
                targets.append((fpath, info))
                if len(targets) >= 2:
                    return targets

    return targets


# ── Single file conversion ────────────────────────────────────────────────────

def convert_file(src: str, ffbin: str, strategy: str = "remux_mp4",
                 dry_run: bool = False, keep_orig: bool = True,
                 out_dir: Optional[str] = None) -> bool:
    """Convert a single file using the specified strategy."""
    src = str(Path(src).resolve())
    if not os.path.isfile(src):
        log.error(f"File not found: {src}")
        return False

    STATS["processed"] += 1
    streams = probe_streams(src, ffbin)

    if not streams["video"]:
        log.error(f"No video streams found in: {src}")
        STATS["skipped"] += 1
        return False

    # Determine output path
    src_path = Path(src)
    if out_dir:
        dst = str(Path(out_dir) / (src_path.stem + ".mp4"))
    else:
        dst = str(src_path.with_suffix(".mp4"))

    if os.path.exists(dst) and dst != src:
        log.warning(f"Output already exists, skipping: {dst}")
        STATS["skipped"] += 1
        return False

    log.info(f"\nConverting: {src_path.name}")
    log.info(f"  → {Path(dst).name}")
    log.info(f"  Video: {streams['video'][0]['codec']} "
             f"{streams['video'][0].get('resolution', '?')}")
    log.info(f"  Audio: {streams['audio'][0]['codec'] if streams['audio'] else 'none'}")
    log.info(f"  Subs:  {[s['codec'] for s in streams['subtitle']]}")

    strategy_lower = strategy.lower()
    if strategy_lower == "remux_mp4":
        ok = strategy_remux_mp4(src, dst, streams, ffbin, dry_run)
    elif strategy_lower == "vt_h264":
        ok = strategy_vt_h264(src, dst, streams, ffbin, dry_run)
    elif strategy_lower == "vt_hevc":
        ok = strategy_vt_hevc(src, dst, streams, ffbin, dry_run)
    else:
        log.error(f"Unknown strategy: {strategy}")
        return False

    if ok and not dry_run and not keep_orig:
        os.unlink(src)
        log.info(f"  Deleted original: {src_path.name}")

    return ok


# ── Batch from scan results ───────────────────────────────────────────────────

def batch_from_scan(scan_path: str, ffbin: str, strategy: str = "remux_mp4",
                    dry_run: bool = False, keep_orig: bool = True,
                    out_dir: Optional[str] = None) -> None:
    """Process all HIGH/MEDIUM severity files from scan_results.json."""
    with open(scan_path) as f:
        data = json.load(f)

    targets = [
        r for r in data["results"]
        if r.get("severity") in ("HIGH", "MEDIUM")
        and r.get("ext") == ".mkv"
    ]

    log.info(f"Batch processing {len(targets)} files with strategy: {strategy}")

    for i, record in enumerate(targets):
        log.info(f"\n[{i+1}/{len(targets)}]")
        convert_file(record["path"], ffbin, strategy=strategy,
                     dry_run=dry_run, keep_orig=keep_orig, out_dir=out_dir)

    _print_stats()


def _print_stats() -> None:
    saved = STATS["orig_bytes"] - STATS["new_bytes"]
    log.info(f"\n{'='*50}")
    log.info(f"  DONE")
    log.info(f"  Processed:  {STATS['processed']}")
    log.info(f"  Converted:  {STATS['converted']}")
    log.info(f"  Skipped:    {STATS['skipped']}")
    log.info(f"  Errors:     {STATS['errors']}")
    if STATS["orig_bytes"]:
        log.info(f"  Space saved: {saved // 1024 // 1024:,}MB")
    log.info(f"{'='*50}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert/repackage video files for Plex + Apple TV 4K"
    )
    parser.add_argument("--test", action="store_true",
                        help="Benchmark all strategies on sample files")
    parser.add_argument("--file", type=str,
                        help="Convert a single file")
    parser.add_argument("--input", type=str,
                        help="Batch process from scan_results.json")
    parser.add_argument("--strategy", default="remux_mp4",
                        choices=["remux_mp4", "vt_h264", "vt_hevc"],
                        help="Conversion strategy (default: remux_mp4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without converting")
    parser.add_argument("--no-keep-orig", action="store_true",
                        help="Delete original after successful conversion")
    parser.add_argument("--out-dir", type=str,
                        help="Output directory (default: same as input)")
    args = parser.parse_args()

    ffbin = get_ffmpeg()
    log.info(f"ffmpeg: {ffbin}")

    vt = check_videotoolbox(ffbin)
    log.info(f"VideoToolbox h264: {'✅' if vt['h264_videotoolbox'] else '❌ (will use software)'}")
    log.info(f"VideoToolbox hevc: {'✅' if vt['hevc_videotoolbox'] else '❌ (will use software)'}")

    if args.dry_run:
        log.info("*** DRY RUN MODE ***")

    if args.test:
        run_benchmark(ffbin)
    elif args.file:
        convert_file(args.file, ffbin,
                     strategy=args.strategy,
                     dry_run=args.dry_run,
                     keep_orig=not args.no_keep_orig,
                     out_dir=args.out_dir)
        _print_stats()
    elif args.input:
        batch_from_scan(args.input, ffbin,
                        strategy=args.strategy,
                        dry_run=args.dry_run,
                        keep_orig=not args.no_keep_orig,
                        out_dir=args.out_dir)
    else:
        parser.print_help()
        log.info("\nQuick start:")
        log.info("  python optimize_video.py --test            # benchmark all strategies")
        log.info("  python optimize_video.py --file ep.mkv     # convert one file")


if __name__ == "__main__":
    main()
