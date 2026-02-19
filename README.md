# PlexOptimizer

Scans your Plex libraries and fixes files that crash or force a full CPU transcode when streaming to Apple TV 4K. Optimized specifically for the Mac Pro 6,1 (dual AMD FirePro GPUs, macOS 12.7.6) → Plex 1.43 → Apple TV 4K 3rd gen (tvOS 18.6).

## The Problem

The Apple TV 4K can natively play H.264 and HEVC video — but it **cannot render ASS/SSA or PGS (Blu-ray image) subtitle tracks**. When Plex sees these, it forces a full video transcode to burn-in the subtitles, even if the video itself would direct-play. On a Mac Pro 6,1 with hardware acceleration disabled, this causes server crashes.

**Three-tool solution:**
1. `scan_library.py` — find all problematic files across your TV + movie libraries
2. `fix_subs.py` — convert ASS→SRT, drop PGS/image subs (no video re-encode)
3. `optimize_video.py` — optionally repackage MKV→MP4, with GPU-accelerated re-encode options

---

## Architecture

```
PlexOptimizer/
├── scan_library.py      # Library-wide scanner, outputs scan_results.json
├── fix_subs.py          # Subtitle fixer (remux only, no video re-encode)
├── optimize_video.py    # Video converter with 3 strategies
├── logs/                # Per-run timestamped log files
├── work/                # Temp outputs during benchmark (safe to delete)
└── venv/                # Python virtualenv
```

**External dependencies:**
- `ffmpeg` — `brew install ffmpeg` (required for all operations)
- `mkvtoolnix` — `brew install mkvtoolnix` (optional, used for some remux ops)

**Python deps (venv):** `tqdm`, `rich`

---

## Quick Start

```bash
cd ~/Claude\ Scripts\ and\ Venvs/PlexOptimizer
source venv/bin/activate

# 1. Full library scan (takes ~2-4 hours for large libraries)
python scan_library.py

# 2. Fix subtitles on all HIGH severity files (dry run first)
python fix_subs.py --dry-run
python fix_subs.py

# 3. Benchmark video conversion strategies on sample files
python optimize_video.py --test

# 4. Convert a single problem file
python optimize_video.py --file "/Volumes/tv/Show/ep.mkv" --strategy vt_h264
```

---

## CLI Reference

### scan_library.py

| Flag | Default | Description |
|------|---------|-------------|
| `--tv` | `/Volumes/tv` | TV library path |
| `--movies` | `/Volumes/movies` | Movies library path |
| `--output` | `scan_results.json` | Output JSON path |
| `--verbose` | off | Log every file (not just every 50th) |

**Output severity levels:**
- 🔴 `HIGH` — ASS/PGS subs present → will force full transcode + crash
- 🟠 `MEDIUM` — incompatible audio codec
- 🟡 `LOW` — MKV container (remux only, no crash)
- ✅ `OK` — no issues found

### fix_subs.py

| Flag | Default | Description |
|------|---------|-------------|
| `--dry-run` | off | Preview without modifying files |
| `--file FILE` | — | Process a single MKV |
| `--input FILE` | `scan_results.json` | Batch from scan results |
| `--severity` | `HIGH` | Minimum severity: HIGH/MEDIUM/LOW |
| `--no-backup` | off | Delete `.orig` backup after success |

**What it does:**
- `ASS/SSA` → converts to SRT text (full text preserved, styling lost)
- `PGS/VOBSUB` → drops from container if SRT track exists; flags if not
- Video and audio are **never re-encoded** (stream copy only)
- Originals backed up as `filename.mkv.orig` unless `--no-backup`

### optimize_video.py

| Flag | Default | Description |
|------|---------|-------------|
| `--test` | — | Benchmark all 3 strategies on small sample files |
| `--file FILE` | — | Convert a single file |
| `--input FILE` | — | Batch from scan_results.json |
| `--strategy` | `remux_mp4` | `remux_mp4`, `vt_h264`, or `vt_hevc` |
| `--dry-run` | off | Preview without converting |
| `--no-keep-orig` | off | Delete original after success |
| `--out-dir DIR` | same dir | Write output files to this directory |

**Strategies:**

| Strategy | Speed | Quality | Use Case |
|----------|-------|---------|----------|
| `remux_mp4` | Fastest (no encode) | Lossless | H.264/HEVC MKV → MP4, fix text subs |
| `vt_h264` | Fast (GPU) | Good | HEVC files, maximum ATV4K compatibility |
| `vt_hevc` | Medium (GPU) | Best | Quality-critical content, smaller files |

---

## Watch / Stop / Restart

### Watch a running job
```bash
# Follow the latest log file in real time
tail -f ~/Claude\ Scripts\ and\ Venvs/PlexOptimizer/logs/$(ls -t ~/Claude\ Scripts\ and\ Venvs/PlexOptimizer/logs/ | head -1)

# Or shorter:
cd ~/Claude\ Scripts\ and\ Venvs/PlexOptimizer && tail -f logs/$(ls -t logs/ | head -1)
```

### Stop a running job
```bash
# Find the PID
ps aux | grep -E "fix_subs|optimize_video|scan_library" | grep -v grep

# Graceful stop (SIGTERM — lets ffmpeg finish current segment)
kill -15 <PID>

# Hard stop (SIGKILL — immediate)
kill -9 <PID>
```

**Note:** If you kill during an ffmpeg encode, the `.tmp` output file is deleted automatically. The original `.orig` backup is untouched. Safe to restart.

### Restart a job
```bash
cd ~/Claude\ Scripts\ and\ Venvs/PlexOptimizer
source venv/bin/activate

# fix_subs is idempotent — files already fixed won't be re-processed
# (the bad subtitle codec won't be in them anymore)
python fix_subs.py

# optimize_video skips files if output already exists
python optimize_video.py --file /path/to/file.mkv --strategy vt_h264
```

---

## GPU Notes — Mac Pro 6,1 / macOS 12

The Mac Pro 6,1 has dual AMD FirePro GPUs. On macOS 12 (Monterey), these expose the **VideoToolbox** framework for hardware-accelerated encode/decode:

- `h264_videotoolbox` — H.264 encode via GPU ✅ Available
- `hevc_videotoolbox` — HEVC encode via GPU (may fall back to software on older AMD) ⚠️

VideoToolbox quality scale: `0` = worst, `100` = best lossless. Default `65` ≈ visually transparent.

**To enable Plex hardware acceleration:**
Plex Web → Settings → Transcoder → Enable "Use hardware acceleration when available"

---

## Known Quirks

- `PGS/VOBSUB` subs cannot be converted to text without OCR. They are dropped. If a file only has PGS subs and no SRT, subtitles will be lost — fix_subs will warn you.
- `remux_mp4` with ASS subs converts to `mov_text` (MP4's subtitle format). ASS text is preserved but styling (fonts, colours, positioning) is lost.
- NFS latency: your media is on NFS (`192.168.168.109`). ffmpeg reads the source over NFS; for very large files this can be slower. Consider running jobs overnight.
- After converting, trigger a Plex library scan to pick up the new MP4 files.
