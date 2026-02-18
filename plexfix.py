#!/usr/bin/env python3
"""
plexfix.py — Shared core library for plex-batch-optimizer.

Provides: probe(), classify(), build_cmd(), verify(), text_sub_maps(),
          attachment_maps(), output_ext().

Imported by batch_optimize.py and watcher.py.
"""

import os, json, subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FFMPEG  = '/usr/local/bin/ffmpeg'
FFPROBE = 'ffprobe'

# ATV4K-compatible codec sets
ATV_VIDEO_OK = {'h264', 'hevc', 'mpeg4', 'vp9'}
ATV_AUDIO_OK = {'aac', 'eac3', 'ac3', 'mp3', 'alac', 'flac', 'opus',
                'pcm_s16le', 'pcm_s24le'}
ATV_CONT_OK  = {'.mkv', '.mp4', '.mov', '.m4v'}

# Image-based subtitle codecs — not compatible with ATV4K direct play
IMAGE_SUBS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'pgssub'}

VIDEO_EXTS = {
    '.mkv', '.mp4', '.avi', '.mov', '.m4v', '.ts', '.m2ts',
    '.wmv', '.flv', '.rm', '.rmvb', '.mpg', '.mpeg', '.divx',
}

# ── ffprobe ────────────────────────────────────────────────────────────────────

def probe(path: str) -> Optional[Dict]:
    """Run ffprobe on path, return parsed JSON or None on failure."""
    r = subprocess.run(
        [FFPROBE, '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True, timeout=30
    )
    try:
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None

# ── Stream helpers ─────────────────────────────────────────────────────────────

def text_sub_maps(probe_data: Dict) -> List[str]:
    """Explicit -map 0:N args for text-only subtitle streams.

    Image subs (PGS/VobSub) are excluded by omission.

    IMPORTANT: ffmpeg negative maps (-map -0:s:m:codec_name:X) match user
    metadata TAGS, not codec properties — they are unreliable for this purpose.
    Explicit positive index mapping is the only safe approach.
    """
    maps: List[str] = []
    for s in probe_data.get('streams', []):
        if (s.get('codec_type') == 'subtitle' and
                s.get('codec_name', '').lower() not in IMAGE_SUBS):
            maps += ['-map', f"0:{s['index']}"]
    return maps


def attachment_maps(probe_data: Dict) -> List[str]:
    """Return -map args for font/attachment streams (required for ASS rendering)."""
    maps: List[str] = []
    for s in probe_data.get('streams', []):
        if s.get('codec_type') == 'attachment':
            maps += ['-map', f"0:{s['index']}"]
    return maps

# ── Classifier ─────────────────────────────────────────────────────────────────

def classify_probe(probe_data: Dict) -> Optional[str]:
    """Classify a file from live probe data. Returns issue type or None if clean."""
    streams   = probe_data.get('streams', [])
    fmt       = probe_data.get('format', {})
    cont      = '.' + fmt.get('format_name', '').lower().split(',')[0]

    vid_strs  = [s for s in streams if s.get('codec_type') == 'video'
                 and not s.get('disposition', {}).get('attached_pic')]
    aud_strs  = [s for s in streams if s.get('codec_type') == 'audio']
    sub_strs  = [s for s in streams if s.get('codec_type') == 'subtitle']

    vcodec    = vid_strs[0].get('codec_name', '').lower() if vid_strs else ''
    acodec    = aud_strs[0].get('codec_name', '').lower() if aud_strs else ''
    sub_codes = [s.get('codec_name', '').lower() for s in sub_strs]

    # Check for MJPEG in secondary video streams
    has_mjpeg   = any(v.get('codec_name', '').lower() == 'mjpeg'
                      for v in streams
                      if v.get('codec_type') == 'video'
                      and v.get('disposition', {}).get('attached_pic'))
    has_bad_sub = any(c in IMAGE_SUBS for c in sub_codes)

    # Container sniff from format_name
    fname_norm = fmt.get('filename', '')
    ext = Path(fname_norm).suffix.lower() if fname_norm else cont

    if vcodec == 'av1':                                    return 'av1'
    if acodec in ('dts', 'truehd', 'mlp'):                return 'dts'
    if ext in ('.avi', '.wmv', '.rm', '.rmvb', '.flv'):   return 'bad_container_avi'
    if ext in ('.ts', '.m2ts'):                            return 'bad_container_ts'
    if ext == '.m4v':                                       return 'bad_container_m4v'
    if has_mjpeg:                                          return 'mjpeg'
    if has_bad_sub:                                        return 'pgs_vobsub'
    return None


def classify_scan_record(r: Dict) -> Optional[str]:
    """Classify from a media_scan.py record (pre-computed fields)."""
    vid    = r.get('video', [{}])
    aud    = r.get('audio', [])
    subs   = r.get('subtitles', [])
    vd     = vid[0] if vid else {}
    vcodec = vd.get('codec', '').lower()
    acodec = aud[0].get('codec', '').lower() if aud else ''
    cont   = r.get('container', '').lower()
    sub_cc = [s.get('codec', '').lower() for s in subs]
    has_bad_sub = any(c in IMAGE_SUBS for c in sub_cc)
    has_mjpeg   = any(v.get('codec', '').lower() == 'mjpeg' for v in vid[1:])

    if vcodec == 'av1':                                     return 'av1'
    if acodec in ('dts', 'truehd', 'mlp'):                 return 'dts'
    if cont in ('.avi', '.wmv', '.rm', '.rmvb', '.flv'):   return 'bad_container_avi'
    if cont in ('.ts', '.m2ts'):                            return 'bad_container_ts'
    if cont == '.m4v':                                       return 'bad_container_m4v'
    if has_mjpeg:                                           return 'mjpeg'
    if has_bad_sub:                                         return 'pgs_vobsub'
    return None

# ── Output extension ───────────────────────────────────────────────────────────

def output_ext(issue: str, src: str) -> str:
    """Return the correct output file extension for this issue + source."""
    if issue == 'bad_container_avi':  return '.mp4'
    if issue in ('bad_container_ts', 'bad_container_m4v'): return '.mkv'
    # .m4v is iPod/iTunes container — HEVC is not a valid codec tag for it.
    # Any fix applied to a .m4v must output to .mkv to avoid container mismatch.
    if Path(src).suffix.lower() == '.m4v': return '.mkv'
    return Path(src).suffix.lower()

# ── ffmpeg command builder ─────────────────────────────────────────────────────

def build_cmd(issue: str, src: str, dst: str,
              probe_data: Optional[Dict] = None) -> List[str]:
    """Build and return the ffmpeg command list for this issue type.

    probe_data: live ffprobe result for src. Fetched automatically if None.

    All commands that touch subtitle streams use probe-based explicit index
    mapping (text_sub_maps) rather than negative maps. See text_sub_maps()
    docstring for the reason.
    """
    if probe_data is None:
        probe_data = probe(src) or {'streams': []}

    base = [FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning',
            '-stats', '-i', src]

    if issue == 'pgs_vobsub':
        # Drop image subs, preserve text subs (ASS/SRT) + font attachments.
        return base + [
            '-map', '0:v', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c', 'copy', dst,
        ]

    if issue == 'mjpeg':
        # 0:v:0 = primary video only (skips MJPEG attached_pic).
        # Preserve text subs; silently drop co-present PGS.
        return base + [
            '-map', '0:v:0', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c', 'copy', dst,
        ]

    if issue == 'dts':
        # Copy HEVC/H264 video, transcode DTS/TrueHD → EAC3.
        # ~97% of DTS files also have PGS — dropped via text_sub_maps.
        return base + [
            '-map', '0:v', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c:v', 'copy',
            '-c:a', 'eac3', '-b:a', '448k',
            '-c:s', 'copy', dst,
        ]

    if issue == 'bad_container_avi':
        # MPEG4+MP3 in AVI → MP4.
        # -fflags +genpts fixes VBR MP3 timestamp discontinuities.
        return [FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning',
                '-stats', '-fflags', '+genpts', '-i', src,
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                '-movflags', '+faststart', dst]

    if issue == 'bad_container_ts':
        # Drop timed_id3 data streams, copy A/V to MKV.
        return base + ['-map', '0:v', '-map', '0:a', '-c', 'copy', dst]

    if issue == 'bad_container_m4v':
        # .m4v (iPod/iTunes container) cannot hold HEVC with a valid codec tag.
        # Remux to MKV: copy all A/V, preserve text subs, strip data streams
        # (eia_608 closed-caption, bin_data, etc. have no MKV equivalent tag).
        return base + [
            '-map', '0:v:0', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-map', '-0:d',          # strip data/eia_608/bin_data streams
            '-c', 'copy', dst,
        ]

    if issue == 'av1':
        # AV1 10-bit (yuv420p10le) → H.264 8-bit via libx264.
        # format=yuv420p: mandatory 10→8 bit conversion.
        # Without it ffmpeg exits 187 and produces a 0-byte file.
        return base + [
            '-map', '0:v:0', '-map', '0:a',
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-vf', 'format=yuv420p',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
            '-profile:v', 'high', '-level', '4.1',
            '-threads', '12',
            '-c:a', 'copy', '-c:s', 'copy',
            '-movflags', '+faststart', dst,
        ]

    raise ValueError(f'Unknown issue type: {issue}')

# ── Output verifier ────────────────────────────────────────────────────────────

def verify(src: str, dst: str, label: str = '') -> Tuple[bool, str]:
    """Verify dst is ATV4K direct-play compatible. Returns (ok, message)."""
    if not os.path.exists(dst) or os.path.getsize(dst) < 1024:
        return False, 'missing or empty output'

    inp = probe(src)
    out = probe(dst)
    if not inp or not out:
        return False, 'ffprobe failed on output'

    # Duration within 3%
    in_dur  = float(inp.get('format', {}).get('duration', 0))
    out_dur = float(out.get('format', {}).get('duration', 0))
    if in_dur > 5 and abs(in_dur - out_dur) / in_dur > 0.03:
        return False, f'duration drift {in_dur:.1f}s → {out_dur:.1f}s'

    has_video = False
    for s in out.get('streams', []):
        ct = s.get('codec_type', '')
        cn = s.get('codec_name', '').lower()
        dp = s.get('disposition', {})

        if ct == 'video':
            if dp.get('attached_pic'):
                return False, 'MJPEG attached_pic still present'
            has_video = True
            if cn not in ATV_VIDEO_OK:
                return False, f'video codec {cn} not ATV4K-compatible'
        elif ct == 'audio':
            if cn == 'dts':
                return False, 'DTS audio still present'
        elif ct == 'subtitle':
            if cn in IMAGE_SUBS:
                return False, f'image subtitle {cn} still present'

    if not has_video:
        return False, 'no video stream in output'

    in_mb  = os.path.getsize(src)  / 1024 / 1024
    out_mb = os.path.getsize(dst) / 1024 / 1024
    return True, f'{in_mb:.0f}MB → {out_mb:.0f}MB'
