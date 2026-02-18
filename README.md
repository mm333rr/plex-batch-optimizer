# plex-batch-optimizer

Batch transcode/remux pipeline that makes a Plex library fully direct-play compatible on **Apple TV 4K** over a 600 Mbps WiFi link. Scans, classifies, validates, and processes every incompatible file — preserving quality and stream metadata throughout.

---

## System Requirements

| Component | Version / Spec |
|---|---|
| macOS | 12.x (Monterey) or later |
| Python | 3.9+ (`python3` on PATH) |
| ffmpeg | 8.x with libx264, libdav1d, eac3 encoder |
| HandBrakeCLI | 1.x (optional — used in pilot tests) |
| RAM | 8 GB min; 16 GB recommended for parallel AV1 encodes |
| CPU | 8+ cores recommended (AV1 decode + libx264 encode is CPU-intensive) |

**GPU note:** AMD FirePro D700 (Mac Pro 2013) supports VideoToolbox *decode* only — no hardware encode. All encoding runs on the CPU via libx264/libx265.

---

## Architecture

```
media_scan.py          →  scan_results.json          (classify every file)
        ↓
safety_test.py         →  safety_test_result.json    (validate 1 file per type)
        ↓
batch_optimize.py      →  NAS files replaced in-place (original kept as .bak)
```

### Problem Types & Fix Methods (all validated by safety_test.py)

| Type | Count* | Primary Issue | Method |
|---|---|---|---|
| `pgs_vobsub` | 1,622 | PGS/VobSub image subtitles | `-c copy`, drop image subs via probe-based index mapping |
| `av1` | 427 | AV1 video (not ATV4K direct-play) | `libx264 -vf format=yuv420p -crf 20` (10→8 bit) |
| `dts` | 325 | DTS/TrueHD audio | `-c:v copy -c:a eac3 -b:a 448k` |
| `avi` | 243 | AVI container | `→ .mp4 -fflags +genpts -c:v copy -c:a aac` |
| `mjpeg` | 223 | MJPEG attached thumbnail | `-map 0:v:0 -c copy` |
| `ts` | 5 | MPEG-TS container | `→ .mkv -c copy` (drops timed_id3 data stream) |

*from initial library scan of 14,379 files*

### Key Engineering Decision: Probe-Based Subtitle Mapping

**Problem:** `ffmpeg -map -0:s:m:codec_name:hdmv_pgs_subtitle` doesn't work to exclude image subtitles. The `:m:` selector matches against user metadata *tags* (language, title), not codec properties.

**Solution:** Pre-probe every file with ffprobe, extract the stream indices of text-only subtitle tracks (ASS/SRT/WebVTT), and build explicit positive `-map 0:N` arguments. Image subs are excluded by omission — no negative maps needed. This also correctly preserves ASS/SRT text subs in files that have both image and text subtitle tracks (e.g., Boruto, Naruto, Demon Slayer).

---

## Quick Start

```bash
# 1. Scan the library
python3 media_scan.py --paths /Volumes/tv /Volumes/movies

# 2. Safety test — one file per problem type, verify output is ATV4K direct-play
python3 safety_test.py --dry-run   # preview
python3 safety_test.py             # run

# 3. Full batch — dry run first
python3 batch_optimize.py --dry-run
python3 batch_optimize.py
```

---

## CLI Reference

### media_scan.py

```
python3 media_scan.py [OPTIONS]

  --paths PATH [PATH ...]   Directories to scan (default: /Volumes/tv /Volumes/movies)
  --output FILE             Output JSON path (default: results/scan_results.json)
  --workers N               Parallel ffprobe workers (default: 16)
```

### safety_test.py

```
python3 safety_test.py [OPTIONS]

  --dry-run                 Preview commands without encoding
  --only T1,T3              Run only specified job IDs (T1–T7)
```

### batch_optimize.py

```
python3 batch_optimize.py [OPTIONS]

  --dry-run                 Preview only — no encoding, no file changes
  --type TYPE[,TYPE...]     Process only these types:
                              av1, dts, pgs_vobsub, mjpeg, avi, ts
  --limit N                 Process at most N files per type (for testing)
  --no-replace              Stage outputs only — do not overwrite NAS originals
  --workers-io N            I/O-only parallel workers (default: 12)
  --workers-light N         Light CPU workers for DTS transcode (default: 6)
  --workers-heavy N         Heavy CPU workers for AV1 encode (default: 2)
```

---

## Watch / Stop / Restart

```bash
# Live log tail
tail -f "/Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/batch_current.log"

# Progress count
python3 -c "
import json
d = json.load(open('results/batch_completed.json'))
f = json.load(open('results/batch_failed.json'))
print(f'Done: {len(d)}  Failed: {len(f)}')
"

# Active ffmpeg jobs
ps aux | grep '[f]fmpeg' | awk '{print $2, $3"%CPU", $11}'

# Stop gracefully (Ctrl+C once in the terminal running batch_optimize.py)
# Or from another shell:
kill -INT $(pgrep -f batch_optimize.py)

# Restart — completed files are skipped automatically
python3 batch_optimize.py

# Retry only previously failed files
python3 batch_optimize.py --type av1   # re-classifies; completed skipped, failed retried
```

---

## Output & Backup Strategy

For each processed file:
1. Encode to `filename.atv_tmp.ext` alongside the original
2. Verify output (codec check, duration within 3%, no image subs, no MJPEG attached pic)
3. Rename original → `filename.mkv.bak`
4. Rename tmp → final filename (extension may change for AVI→MP4, TS→MKV)

**`.bak` files** are kept until you verify Plex plays the new version correctly. To clean up:
```bash
# Preview what would be deleted
find /Volumes/tv /Volumes/movies -name "*.bak" | wc -l

# Delete all .bak files (run after confirming Plex is happy)
find /Volumes/tv /Volumes/movies -name "*.bak" -delete
```

---

## Resume Safety

`results/batch_completed.json` — set of completed input paths  
`results/batch_failed.json` — map of path → failure reason  

Re-running `batch_optimize.py` at any time safely skips completed files. If a file has a `.bak` sibling it is also treated as already processed.

---

## Known Quirks

- **AV1 10-bit:** Must use `-vf format=yuv420p` to convert from `yuv420p10le` before libx264. Without it, ffmpeg silently produces a 0-byte output file and exits 187.
- **AVI + VBR MP3:** Requires `-fflags +genpts` to fix timestamp discontinuities before remuxing. Without it, muxer rejects the stream.
- **Multi-MJPEG streams:** Only the first video stream (`0:v:0`) is the real video; subsequent MJPEG streams are attached thumbnails regardless of how many there are (tested: NOVA with 4 MJPEG streams).
- **Late Show anomaly:** One file (`2025-10-28 Colin Farrell.mkv`) reports 1,274 min duration due to corrupted moov atom. Re-download recommended.
- **Duplicate files:** ~200 GB of exact duplicate movies identified. Pattern: keep `{tmdb-XXXXX}` named file, delete bare-name copy.
