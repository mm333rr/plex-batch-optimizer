# Changelog — PlexOptimizer

## [1.0.0] — 2026-02-17

### Initial release

**Context:** Mac Pro 6,1 running Plex 1.43.0.10492 was crashing when streaming
Celebrity Traitors (2025) to Apple TV 4K 3rd gen (tvOS 18.6). Root cause: all
episodes contain ASS subtitle tracks in MKV containers. ATV4K cannot render ASS
natively, forcing Plex to burn-in subs via full CPU software transcode. With
hardware acceleration disabled, this caused sustained crashes during active
transcode sessions.

### Added
- `scan_library.py` — full library scanner, classifies all video files by
  compatibility severity (HIGH/MEDIUM/LOW/OK), outputs JSON
- `fix_subs.py` — remux-only subtitle fixer: converts ASS/SSA → SRT,
  drops image-based PGS/VOBSUB subs, backs up originals
- `optimize_video.py` — three video conversion strategies using VideoToolbox
  GPU encoders: remux_mp4, vt_h264, vt_hevc; includes benchmark mode
- `README.md` — full documentation with watch/stop/restart commands
- `CHANGELOG.md` — this file
- `.gitignore` — excludes venv, logs, work dir, tmp files

### Known issues
- ffmpeg/mkvtoolnix install via brew was slow (building from source on Intel Mac);
  install separately if not present before first run
- hevc_videotoolbox may fall back to software on AMD FirePro D500/D700 under
  macOS 12; h264_videotoolbox is the reliable GPU path
- PGS/image subs cannot be losslessly converted to text (OCR required);
  they are dropped from output files
