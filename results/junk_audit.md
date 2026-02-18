# Junk File Audit Report — mMacPro Plex Library
**Generated:** 2026-02-17

---

## Summary

22,267 files flagged as "junk extensions" — but they are NOT all junk.
Here's exactly what each type is, its value to Plex, and what to do with it.

---

## File Type Verdicts

### ✅ KEEP — `.idx` + `.sub` pairs (18 pairs)
**These are actual subtitles. Do not delete.**
VobSub format — a `.idx` index file paired with a `.sub` binary subtitle file.
These are external subtitles for movies that don't have embedded subs.
Plex reads and serves them correctly. All 18 have confirmed paired `.sub` files.

Examples: Alien Covenant, Gone Girl, Dunkirk, The Deer Hunter, The Matrix Revolutions, Bicycle Thieves, etc.

**Action: Leave alone.**

---

### ✅ KEEP (Plex uses these) — `poster.jpg`, `backdrop.jpg`, `folder.jpg`, `fanart.jpg` (~4,205 files)
Plex's Local Media Assets agent reads these as local artwork overrides.
When present, Plex prefers local files over what it fetches online — useful for
rare/niche titles that have poor TMDB/TheTVDB artwork.

**Action: Leave alone. They save Plex API calls and provide stable artwork.**

---

### ⚠️ OPTIONAL — `.nfo` files (2,697 files)
Kodi XML metadata files. Well-formed with TMDB IDs, plots, ratings, cast, genres.

**Plex ignores these by default.** Plex uses its own online agents (TMDB/TheTVDB).
However, if you ever enable "Local Media Assets" + XBMCnfoTVImporter/XBMCnfoMovieImporter
agents in Plex, these become the primary metadata source.

Given your library is already well-named with TMDB IDs in folder names
(e.g., `{tmdb-231001}`), Plex's agent works great without them.

**Action: Safe to delete if disk space is needed. Harmless if left.**
**Recommended: Leave them — they're small and provide a metadata backup.**

---

### ⚠️ MARGINAL — Kodi-only artwork (3,882 files: `clearart`, `disc`, `logo`, `landscape`, `clearlogo`)
These are artwork types used by Kodi and Emby but **not standard Plex themes**.
However, some Plex home theater themes and the Plex HTPC app do use `logo.png`
and `clearart.png` for overlay graphics on the detail screen.

**Action: Low priority. Leave if disk space is not a concern (NAS has 13TB free).
Can delete `disc.png` and `landscape.png` safely — Plex never uses these.**

---

### 🗑️ DELETE — `.url` files (2,680 files)
Windows Internet Shortcut files (e.g., `tmdb.url`, `tvdb.url`, `episode.url`).
Contain nothing but a URL to TMDB/TVDB pages. On macOS they open in a browser
but serve zero purpose in a Plex library. Plex ignores them entirely.

**Action: DELETE ALL. Zero value on macOS/Plex.**

---

### 🗑️ DELETE — `.txt` files (696 files)
100% torrent attribution/advertising files:
- "Torrent Downloaded From UIndex.org"
- "[TGx]Downloaded from torrentgalaxy.to"
- "NEW upcoming releases by Xclusive.txt"
- "Downloaded from thepirateheaven.org"
- "YIFYStatus.com.txt"

Zero informational value. Plex ignores them. Pure clutter.

**Action: DELETE ALL.**

---

### 🗑️ DELETE — YTS/watermark/piracy-brand images (455 files)
Images named after torrent sites or release groups:
- `www.YTS.MX.jpg` (watermarked movie poster ads)
- `www.yify-torrents.com.jpg`
- `maximersk's TeamHD.jpg`
- `Jolly Roger.jpg`
- `www.yts.am.jpg`, `www.yts.lt.jpg`, etc.

These are torrent site advertisement images embedded by release groups.
Plex may accidentally display them as artwork. Delete immediately.

**Action: DELETE ALL.**

---

### 🗑️ DELETE — `.sfv` and `.srr` files (5 files)
SFV = Simple File Verification checksums. Used during Usenet/torrent download
to verify file integrity. Once downloaded, permanently useless.
SRR = ReScene recovery file. Same story.

**Action: DELETE ALL.**

---

## Safe-to-Delete Summary

| Type | Count | Why Delete |
|------|-------|------------|
| `.url` files | 2,680 | Windows-only, zero macOS/Plex value |
| `.txt` torrent ads | 696 | Torrent site attribution clutter |
| YTS/branding images | 455 | Torrent watermark ads, may pollute artwork |
| `.sfv` / `.srr` | 5 | Post-download checksums, no further use |
| **Total safe-delete** | **3,836** | |

## Keep Summary

| Type | Count | Why Keep |
|------|-------|----------|
| `.idx`+`.sub` subtitle pairs | 18+18 | Actual subtitle files Plex serves |
| `poster/backdrop/folder.jpg` | 4,205 | Plex local artwork — used actively |
| `.nfo` metadata | 2,697 | Harmless metadata backup, small size |
| Kodi artwork (clearart etc.) | 3,882 | Marginal Plex use, NAS space not a concern |

---

## Cleanup Commands

```bash
# ── DRY RUN first — see what would be deleted ─────────────────────────────

# .url files
find /Volumes/tv /Volumes/movies -name "*.url" | wc -l

# .txt torrent files (careful — this matches ALL .txt, verify sample first)
find /Volumes/tv /Volumes/movies -name "*.txt" | head -20

# YTS watermark images
find /Volumes/tv /Volumes/movies -iname "www.yts*.jpg" -o -iname "www.yify*.jpg" \
  -o -iname "*teamhd*.jpg" -o -iname "jolly roger*.jpg" | wc -l

# .sfv / .srr
find /Volumes/tv /Volumes/movies -name "*.sfv" -o -name "*.srr" | wc -l


# ── ACTUAL DELETES (run after verifying dry run) ───────────────────────────

# Delete all .url files
find /Volumes/tv /Volumes/movies -name "*.url" -delete

# Delete torrent .txt files (YTS status files by name pattern)
find /Volumes/tv /Volumes/movies -name "*.txt" \
  \( -iname "*torrent*" -o -iname "*yts*" -o -iname "*yify*" \
     -o -iname "*tgx*" -o -iname "*uindex*" -o -iname "*pirate*" \
     -o -iname "*xclusive*" \) -delete

# Delete YTS/watermark images
find /Volumes/tv /Volumes/movies \
  \( -iname "www.yts*.jpg" -o -iname "www.yify*.jpg" \
     -o -iname "www.yts*.png" -o -iname "*teamhd*.jpg" \
     -o -iname "jolly roger*.jpg" -o -iname "*yfyistatus*" \) -delete

# Delete .sfv and .srr
find /Volumes/tv /Volumes/movies \( -name "*.sfv" -o -name "*.srr" \) -delete
```

---

*Report: /Users/mProAdmin/Claude Scripts and Venvs/MediaScan/results/junk_audit.md*
