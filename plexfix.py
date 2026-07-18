#!/usr/bin/env python3
"""
plexfix.py — Shared core library for plex-batch-optimizer.

Provides: probe(), classify(), build_cmd(), verify(), text_sub_maps(),
          attachment_maps(), output_ext().

Imported by batch_optimize.py and watcher.py.

v1.0.2 (2026-03-02) fixes:
  - text_sub_maps: exclude eia_608 / data-type subtitle codecs with no
    valid MP4/MKV container tag (caused exit 234 on RuPaul MP4s).
  - build_cmd/dts: '-map 0:v:0' to skip MJPEG attached_pic thumbnails.
  - build_cmd/dts: transcode ALL audio streams, not just stream 0.
  - build_cmd/bad_container_ts: transcode pcm_bluray → flac for MKV compat.
"""

import os, json, subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FFMPEG  = '/usr/local/bin/ffmpeg'
FFPROBE = '/usr/local/bin/ffprobe'

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
    try:
        r = subprocess.run(
            [FFPROBE, '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return None  # hung probe (NFS stall / corrupt file) - treat as probe failure
    try:
        return json.loads(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None

# ── Stream helpers ─────────────────────────────────────────────────────────────

def text_sub_maps(probe_data: Dict) -> List[str]:
    """Explicit -map 0:N args for text-only subtitle streams.

    Excluded codecs:
      - IMAGE_SUBS (PGS/VobSub): not ATV4K direct-play compatible.
      - UNSUPPORTED_SUB_CODECS: subtitle-typed streams with no valid codec
        tag in MP4 or MKV containers. eia_608 (CEA-608 closed captions
        embedded in broadcast recordings) causes ffmpeg exit 234 with
        "Could not find tag for codec eia_608 in stream" when any copy
        remux is attempted. bin_data and timed_id3 are data blobs that
        land in the subtitle codec_type bucket on some demuxers.

    IMPORTANT: ffmpeg negative maps (-map -0:s:m:codec_name:X) match user
    metadata TAGS, not codec properties — they are unreliable for this purpose.
    Explicit positive index mapping is the only safe approach.
    """
    UNSUPPORTED_SUB_CODECS = {'eia_608', 'eia_708', 'bin_data', 'timed_id3'}
    maps: List[str] = []
    for s in probe_data.get('streams', []):
        if s.get('codec_type') != 'subtitle':
            continue
        cn = s.get('codec_name', '').lower()
        if cn in IMAGE_SUBS or cn in UNSUPPORTED_SUB_CODECS:
            continue
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

    if issue == 'faststart':
        # Otherwise ATV4K-clean but moov atom is at the back; remux to move it
        # to the front (QuickLook + streaming). Map real streams only so a stray
        # attached_pic / image sub does not trip verify().
        return base + ['-map', '0:v:0', '-map', '0:a?', *text_sub_maps(probe_data),
                       '-c', 'copy', '-movflags', '+faststart', dst]

    if issue == 'pgs_vobsub':
        # Drop image subs (PGS/VobSub), preserve text subs (ASS/SRT) + fonts.
        # Some PGS files also have DTS/TrueHD audio — transcode those to EAC3
        # in the same pass. Using '-map 0:a -c copy' would copy DTS through and
        # fail verify(). Use per-stream audio maps identical to the dts handler.
        DTS_CODECS = {'dts', 'truehd', 'mlp'}
        aud_streams = [s for s in probe_data.get('streams', [])
                       if s.get('codec_type') == 'audio']
        audio_maps: List[str] = []
        audio_codec_args: List[str] = []
        out_idx = 0
        for s in aud_streams:
            cn = s.get('codec_name', '').lower()
            audio_maps += ['-map', f"0:{s['index']}"]
            if cn in DTS_CODECS:
                audio_codec_args += [f'-c:a:{out_idx}', 'eac3',
                                     f'-b:a:{out_idx}', '448k']
            else:
                audio_codec_args += [f'-c:a:{out_idx}', 'copy']
            out_idx += 1
        return base + [
            '-map', '0:v',
            *audio_maps,
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c:v', 'copy',
            *audio_codec_args,
            '-c:s', 'copy', dst,
        ]

    if issue == 'mjpeg':
        # 0:v:0 = primary video only (skips MJPEG attached_pic).
        # Preserve text subs (excluding eia_608 via text_sub_maps); drop PGS.
        # Also handle any DTS/TrueHD audio in the same pass — a file can have
        # an MJPEG attached_pic AND DTS audio simultaneously.
        DTS_CODECS = {'dts', 'truehd', 'mlp'}
        aud_streams = [s for s in probe_data.get('streams', [])
                       if s.get('codec_type') == 'audio']
        audio_maps: List[str] = []
        audio_codec_args: List[str] = []
        out_idx = 0
        for s in aud_streams:
            cn = s.get('codec_name', '').lower()
            audio_maps += ['-map', f"0:{s['index']}"]
            if cn in DTS_CODECS:
                audio_codec_args += [f'-c:a:{out_idx}', 'eac3',
                                     f'-b:a:{out_idx}', '448k']
            else:
                audio_codec_args += [f'-c:a:{out_idx}', 'copy']
            out_idx += 1
        return base + [
            '-map', '0:v:0',
            *audio_maps,
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c:v', 'copy',
            *audio_codec_args,
            '-c:s', 'copy', dst,
        ]

    if issue == 'dts':
        # Copy video (primary stream only — skip MJPEG attached_pic thumbnails
        # which some encodes embed as a second video stream). Previous '-map 0:v'
        # copied ALL video streams including MJPEG, causing verify failure
        # "MJPEG attached_pic still present" for files like Broker (2022).
        #
        # Transcode ALL audio streams to eac3, not just stream 0. Files with
        # multiple audio tracks (e.g. The Seventh Seal with 12 audio streams)
        # had '-map 0:a -c:a eac3' but ffmpeg only applies the codec to the
        # first stream by default when used without per-stream specifiers.
        # Using '-c:a:0', '-c:a:1' etc. is fragile — the correct idiom is
        # '-c:a eac3' paired with explicit per-stream mapping that excludes
        # DTS tracks entirely, keeping only compatible audio.
        #
        # Strategy: map only non-DTS audio streams explicitly (copy them as-is),
        # then map DTS streams explicitly and transcode those to eac3.
        DTS_CODECS = {'dts', 'truehd', 'mlp'}
        aud_streams = [s for s in probe_data.get('streams', [])
                       if s.get('codec_type') == 'audio']
        audio_maps: List[str] = []
        audio_codec_args: List[str] = []
        out_idx = 0
        for s in aud_streams:
            cn = s.get('codec_name', '').lower()
            audio_maps += ['-map', f"0:{s['index']}"]
            if cn in DTS_CODECS:
                audio_codec_args += [f'-c:a:{out_idx}', 'eac3',
                                     f'-b:a:{out_idx}', '448k']
            else:
                audio_codec_args += [f'-c:a:{out_idx}', 'copy']
            out_idx += 1
        return base + [
            '-map', '0:v:0',
            *audio_maps,
            *text_sub_maps(probe_data),
            *attachment_maps(probe_data),
            '-c:v', 'copy',
            *audio_codec_args,
            '-c:s', 'copy', dst,
        ]

    if issue == 'bad_container_avi':
        # MPEG4+MP3 in AVI → MP4.
        # -fflags +genpts fixes VBR MP3 timestamp discontinuities.
        # Some AVI files (Disney classics, DivX encodes) use mpeg1video,
        # mpeg2video, or non-standard MPEG4 variants that cannot be
        # stream-copied into MP4 — the muxer rejects them. Detect the
        # video codec from probe_data and use libx264 re-encode as a
        # fallback for any codec that isn't a clean copy-safe MPEG4.
        vid_streams = [s for s in probe_data.get('streams', [])
                       if s.get('codec_type') == 'video'
                       and not s.get('disposition', {}).get('attached_pic')]
        avi_vcodec = vid_streams[0].get('codec_name', '').lower() if vid_streams else ''
        # These codecs are copy-safe into MP4 container
        AVI_COPY_SAFE = {'mpeg4', 'h264', 'hevc'}
        if avi_vcodec in AVI_COPY_SAFE:
            video_args = ['-c:v', 'copy']
        else:
            # mpeg1video, mpeg2video, divx, msmpeg4v2, msmpeg4v3, wmv*, etc.
            # must be re-encoded — libx264 is the universal fallback.
            video_args = [
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '20',
                '-profile:v', 'high', '-level', '4.1',
                '-vf', 'format=yuv420p',
                '-threads', '12',
            ]
        return [FFMPEG, '-y', '-hide_banner', '-loglevel', 'warning',
                '-stats', '-fflags', '+genpts', '-i', src,
                *video_args, '-c:a', 'aac', '-b:a', '192k',
                '-movflags', '+faststart', dst]

    if issue == 'bad_container_ts':
        # Remux MPEG-TS / M2TS → MKV. Drop timed_id3 / data streams.
        # Two audio codec families need transcoding in the TS remux path:
        #   pcm_bluray / pcm_dvd → FLAC (lossless, MKV-compatible):
        #     MKV has no valid wav codec tag for pcm_bluray; ffmpeg exits
        #     with "No wav codec tag found for codec pcm_bluray".
        #   dts / truehd / mlp → EAC3 448k:
        #     DTS is not ATV4K direct-play compatible. verify() catches it.
        #     Must be transcoded here even though the primary issue is TS
        #     container, not audio — 9 Songs (2004) has both pcm_bluray AND
        #     a DTS track that must both be handled in the same pass.
        # All other MKV-native codecs (AC3, EAC3, AAC) are copied as-is.
        PCM_BLURAY_CODECS = {'pcm_bluray', 'pcm_dvd'}
        DTS_CODECS = {'dts', 'truehd', 'mlp'}
        aud_streams = [s for s in probe_data.get('streams', [])
                       if s.get('codec_type') == 'audio']
        audio_maps: List[str] = []
        audio_codec_args: List[str] = []
        out_idx = 0
        for s in aud_streams:
            cn = s.get('codec_name', '').lower()
            audio_maps += ['-map', f"0:{s['index']}"]
            if cn in PCM_BLURAY_CODECS:
                audio_codec_args += [f'-c:a:{out_idx}', 'flac']
            elif cn in DTS_CODECS:
                audio_codec_args += [f'-c:a:{out_idx}', 'eac3',
                                     f'-b:a:{out_idx}', '448k']
            else:
                audio_codec_args += [f'-c:a:{out_idx}', 'copy']
            out_idx += 1
        return base + [
            '-map', '0:v:0',
            *audio_maps,
            *text_sub_maps(probe_data),
            '-map', '-0:d',   # drop timed_id3 / bin_data / data streams
            '-c:v', 'copy',
            *audio_codec_args,
            '-c:s', 'copy', dst,
        ]

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
    UNSUPPORTED_SUB_CODECS = {'eia_608', 'eia_708', 'bin_data', 'timed_id3'}
    for s in out.get('streams', []):
        ct = s.get('codec_type', '')
        cn = s.get('codec_name', '').lower()
        dp = s.get('disposition', {})

        if ct == 'video':
            if dp.get('attached_pic'):
                return False, f'attached_pic ({cn}) still present — thumbnail not stripped'
            has_video = True
            if cn not in ATV_VIDEO_OK:
                return False, f'video codec {cn} not ATV4K-compatible'
        elif ct == 'audio':
            if cn == 'dts':
                return False, 'DTS audio still present'
        elif ct == 'subtitle':
            if cn in IMAGE_SUBS:
                return False, f'image subtitle {cn} still present'
            if cn in UNSUPPORTED_SUB_CODECS:
                return False, f'unsupported subtitle codec {cn} still present'

    if not has_video:
        return False, 'no video stream in output'

    in_mb  = os.path.getsize(src)  / 1024 / 1024
    out_mb = os.path.getsize(dst) / 1024 / 1024
    return True, f'{in_mb:.0f}MB → {out_mb:.0f}MB'
