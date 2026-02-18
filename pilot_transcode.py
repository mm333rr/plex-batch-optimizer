#!/usr/bin/env python3
"""
pilot_transcode.py — Pilot batch: 7 files, 5 different treatment methods.

METHODS USED:
  1. AV1 → H.264 via h264_videotoolbox (GPU) — North of North
  2. DTS audio → EAC3, copy video (CPU audio only) — Star Trek
  3. Container remux AVI → MKV, -c copy (I/O only) — King of Queens
  4. MJPEG attached picture strip, -c copy (I/O only) — The Game
  5. PGS subtitle strip to enable direct play — GoT extras x2
  6. PGS burn-in via hevc_videotoolbox (GPU) — GoT Trial by Combat
  7. HandBrakeCLI "HQ 1080p30 Surround" preset — GoT S00E120 (comparison)

GPU STRATEGY:
  Jobs are grouped so GPU-heavy (VT encode) and CPU/IO-only jobs run in parallel.
  Group A (GPU): AV1 encode → PGS burn-in (sequential, single VT pipeline)
  Group B (CPU/IO): DTS transcode + AVI remux + MJPEG strip (parallel)
  Both groups run simultaneously — max hardware utilization.

Usage:
  python3 pilot_transcode.py [--dry-run]
"""

import os, sys, subprocess, threading, time, logging, signal, argparse
from datetime import datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
OUT_BASE = Path("/Volumes/6tb-R1/PlexOptimized")
LOG_DIR  = Path("/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results")
LOG_FILE = LOG_DIR / f"pilot_transcode_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
PID_FILE = LOG_DIR / "pilot_transcode.pid"

FFMPEG  = "/usr/local/bin/ffmpeg"
HBCLI   = "/Applications/Handbrake/HandBrakeCLI"

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

_stop = False
def _sigint(sig, frame):
    global _stop
    log.warning("SIGINT received — stopping after current jobs finish")
    _stop = True
signal.signal(signal.SIGINT, _sigint)

# ── Job Definitions ───────────────────────────────────────────────────────────

def out(subdir: str, fname: str) -> str:
    """Build output path, creating dirs as needed."""
    p = OUT_BASE / subdir
    p.mkdir(parents=True, exist_ok=True)
    return str(p / fname)

TV = "/Volumes/tv"

JOBS = [
    # ── GROUP A: GPU (VideoToolbox encode) — run sequentially ─────────────────
    {
        "id": "A1",
        "group": "GPU",
        "name": "AV1→H.264 VT | North of North S01E06",
        "desc": "Re-encode AV1 video to H.264 via h264_videotoolbox. Copy EAC3 audio + ASS subs. AV1 is unsupported by ATV4K → becomes full direct play.",
        "input":  f"{TV}/North of North (2025) {{tmdb-249023}}/Season 01/North of North - S01E06 - Carnivores.mkv",
        "output": out("tv/North of North (2025) {tmdb-249023}/Season 01",
                      "North of North - S01E06 - Carnivores.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            "-map", "0:v:0", "-map", "0:a", "-map", "0:s?",
            "-c:v", "h264_videotoolbox",
            "-profile:v", "high", "-level", "4.1",
            "-b:v", "3500k",           # ~3.5 Mbps — solid 1080p quality
            "-maxrate", "5000k",
            "-bufsize", "8000k",
            "-c:a", "copy",            # EAC3 6ch stays untouched
            "-c:s", "copy",            # ASS text subs copy fine
            "-movflags", "+faststart",
            o
        ],
    },
    {
        "id": "A2",
        "group": "GPU",
        "name": "PGS burn-in VT | GoT Trial by Combat",
        "desc": "PGS image subs burned into HEVC stream via hevc_videotoolbox. Audio/video re-encode via GPU. Subs become permanent — direct play.",
        "input":  f"{TV}/Game of Thrones (2011) {{tmdb-1399}}/Season 04/Extras/Trial by Combat.mkv",
        "output": out("tv/Game of Thrones (2011) {tmdb-1399}/Season 04/Extras",
                      "Trial by Combat.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            # Burn subtitle stream 0:s:0 onto the video
            "-filter_complex", "[0:v][0:s:0]overlay",
            "-c:v", "hevc_videotoolbox",
            "-b:v", "2500k",
            "-c:a", "copy",
            # No subtitle output stream — it's burned in
            o
        ],
    },

    # ── GROUP B: CPU/IO — run in parallel with Group A ─────────────────────────
    {
        "id": "B1",
        "group": "CPU",
        "name": "DTS→EAC3 audio | Star Trek Animated S02E05",
        "desc": "H.264 video is perfect — copy untouched. Transcode DTS 6ch → EAC3 6ch (Dolby Digital Plus). ATV4K decodes EAC3 natively. CPU audio encode only.",
        "input":  f"{TV}/Star Trek (1973) {{tmdb-1992}}/Season 02/Star Trek - The Animated Series - S02E05 - How Sharper Than a Serpent's Tooth.mkv",
        "output": out("tv/Star Trek (1973) {tmdb-1992}/Season 02",
                      "Star Trek - The Animated Series - S02E05 - How Sharper Than a Serpent's Tooth.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            "-map", "0:v", "-map", "0:a", "-map", "0:s?",
            "-c:v", "copy",              # H.264 untouched — no GPU needed
            "-c:a", "eac3",              # DTS 6ch → EAC3 6ch
            "-b:a", "448k",              # Standard Dolby Digital Plus bitrate
            "-c:s", "copy",
            o
        ],
    },
    {
        "id": "B2",
        "group": "IO",
        "name": "AVI→MKV remux | King of Queens S09E08",
        "desc": "MPEG4+MP3 in AVI container. Remux to MKV losslessly (-c copy). Plex can direct play MPEG4/MP3 in MKV. No quality loss, typically completes in <30s.",
        "input":  f"{TV}/The King of Queens (1998) {{tmdb-4238}}/Season 09/The King of Queens - S09E08 - Offensive Fowl.avi",
        "output": out("tv/The King of Queens (1998) {tmdb-4238}/Season 09",
                      "The King of Queens - S09E08 - Offensive Fowl.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            "-c", "copy",               # Full lossless remux
            o
        ],
    },
    {
        "id": "B3",
        "group": "IO",
        "name": "MJPEG attached strip | The Game S01E01",
        "desc": "HEVC+AAC already direct-play. Strip the embedded MJPEG thumbnail (attached_pic) that confuses Plex into thinking there's a second video stream.",
        "input":  f"{TV}/The Game (2021) {{tmdb-138233}}/Season 01/The Game - S01E01 - A Taste of Vegas, Part 1.mkv",
        "output": out("tv/The Game (2021) {tmdb-138233}/Season 01",
                      "The Game - S01E01 - A Taste of Vegas, Part 1.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            "-map", "0:v:0",            # Only the primary video stream (not attached MJPEG)
            "-map", "0:a",
            "-map", "0:s?",
            "-c", "copy",               # Everything copied losslessly
            o
        ],
    },
    {
        "id": "B4",
        "group": "IO",
        "name": "PGS strip | GoT S00E119 Bastards of Westeros",
        "desc": "GoT Histories & Lore extra. English narration — subs not critical. Strip PGS image subs entirely → HEVC+AAC becomes direct play with zero encoding.",
        "input":  f"{TV}/Game of Thrones (2011) {{tmdb-1399}}/Specials/Game of Thrones - S00E119 - Histories & Lore The Bastards of Westeros.mkv",
        "output": out("tv/Game of Thrones (2011) {tmdb-1399}/Specials",
                      "Game of Thrones - S00E119 - Histories & Lore The Bastards of Westeros.mkv"),
        "cmd": lambda i, o: [
            FFMPEG, "-y", "-i", i,
            "-map", "0:v",
            "-map", "0:a",
            # Intentionally no -map 0:s — drops all subtitle streams
            "-c", "copy",
            o
        ],
    },

    # ── COMPARISON: HandBrakeCLI ───────────────────────────────────────────────
    {
        "id": "C1",
        "group": "HB",
        "name": "HandBrakeCLI | GoT S00E120 Iron Bank",
        "desc": "Same PGS strip scenario but using HandBrakeCLI 'HQ 1080p30 Surround' preset for comparison. Shows HB overhead vs ffmpeg direct.",
        "input":  f"{TV}/Game of Thrones (2011) {{tmdb-1399}}/Specials/Game of Thrones - S00E120 - Histories & Lore The Iron Bank of Braavos.mkv",
        "output": out("tv/Game of Thrones (2011) {tmdb-1399}/Specials",
                      "Game of Thrones - S00E120 - Histories & Lore The Iron Bank of Braavos.mkv"),
        "cmd": lambda i, o: [
            HBCLI,
            "-i", i,
            "-o", o,
            "--preset", "HQ 1080p30 Surround",
            "--subtitle-lang-list", "und,eng",
            "--all-subtitles",
        ],
    },
]

# ── Runner ────────────────────────────────────────────────────────────────────

def run_job(job: dict, dry_run: bool) -> bool:
    """Execute a single job. Returns True on success."""
    jid   = job["id"]
    name  = job["name"]
    inp   = job["input"]
    out_f = job["output"]
    cmd   = job["cmd"](inp, out_f)

    log.info(f"[{jid}] START: {name}")
    log.info(f"[{jid}] METHOD: {job['desc']}")
    log.info(f"[{jid}] IN:  {inp}")
    log.info(f"[{jid}] OUT: {out_f}")
    log.info(f"[{jid}] CMD: {' '.join(cmd)}")

    if not os.path.exists(inp):
        log.error(f"[{jid}] INPUT NOT FOUND — skipping")
        return False

    if dry_run:
        log.info(f"[{jid}] DRY RUN — skipping execution")
        return True

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        # Stream output to log
        progress_line = ""
        for line in proc.stdout:
            line = line.rstrip()
            if "frame=" in line or "size=" in line:
                # ffmpeg progress — overwrite same log line
                progress_line = line
            elif line:
                if progress_line:
                    log.info(f"[{jid}] {progress_line}")
                    progress_line = ""
                log.info(f"[{jid}] {line}")
        if progress_line:
            log.info(f"[{jid}] {progress_line}")
        proc.wait()
    except Exception as e:
        log.error(f"[{jid}] EXCEPTION: {e}")
        return False

    elapsed = time.time() - t0
    if proc.returncode == 0:
        out_size = os.path.getsize(out_f) / 1024 / 1024 if os.path.exists(out_f) else 0
        in_size  = os.path.getsize(inp) / 1024 / 1024
        log.info(f"[{jid}] ✅ DONE in {elapsed:.0f}s | {in_size:.0f}MB → {out_size:.0f}MB ({out_size/in_size*100:.0f}%)")
        return True
    else:
        log.error(f"[{jid}] ❌ FAILED (exit {proc.returncode}) after {elapsed:.0f}s")
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    log.info("=" * 70)
    log.info("PILOT TRANSCODE BATCH — mMacPro D700 GPU + Xeon CPU")
    log.info(f"Output base: {OUT_BASE}")
    log.info(f"Log: {LOG_FILE}")
    log.info(f"PID: {os.getpid()}")
    log.info("=" * 70)

    # Group A (GPU) runs in main thread
    # Group B (CPU/IO) runs in parallel threads
    # Group C (HandBrake) runs after both complete

    group_a = [j for j in JOBS if j["group"] == "GPU"]
    group_b = [j for j in JOBS if j["group"] in ("CPU", "IO")]
    group_c = [j for j in JOBS if j["group"] == "HB"]

    results = {}
    b_threads = []

    log.info("🚀 Launching GPU jobs (Group A) + CPU/IO jobs (Group B) in parallel")

    # Start Group B threads
    def run_b(job):
        results[job["id"]] = run_job(job, args.dry_run)
    for job in group_b:
        t = threading.Thread(target=run_b, args=(job,), daemon=True)
        t.start()
        b_threads.append(t)

    # Run Group A sequentially in main thread (single VT pipeline)
    for job in group_a:
        if _stop: break
        results[job["id"]] = run_job(job, args.dry_run)

    # Wait for Group B
    log.info("⏳ Waiting for CPU/IO jobs to finish...")
    for t in b_threads:
        t.join()

    # Group C (HandBrake comparison) — after GPU is free
    log.info("🎬 Running HandBrakeCLI comparison job (Group C)")
    for job in group_c:
        if _stop: break
        results[job["id"]] = run_job(job, args.dry_run)

    # Summary
    log.info("=" * 70)
    log.info("PILOT BATCH SUMMARY")
    for jid, ok in results.items():
        job = next(j for j in JOBS if j["id"] == jid)
        status = "✅ OK" if ok else "❌ FAIL"
        log.info(f"  [{jid}] {status}  {job['name']}")
    log.info("=" * 70)
    PID_FILE.unlink(missing_ok=True)

if __name__ == "__main__":
    main()
