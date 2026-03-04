#!/usr/bin/env python3
"""
test_v102.py — Regression tests for plexfix.py v1.0.2 bug fixes.

Tests 4 specific fix scenarios:
  T1: eia_608 MP4 (RuPaul) — mjpeg+eia_608 → strip both, no exit 234
  T2: DTS multi-track MKV (Seventh Seal) — ALL DTS tracks → eac3, none remain
  T3: DTS+MJPEG attached_pic MKV (Broker) — dts fix uses 0:v:0, no attached_pic in output
  T4: pcm_bluray M2TS (9 Songs) — pcm_bluray → flac, MKV mux succeeds
"""
import sys, os, json, subprocess, time
from pathlib import Path

sys.path.insert(0, '/Users/mProAdmin/Claude Scripts and Venvs/MediaScan')
from plexfix import probe, classify_probe, build_cmd, verify, text_sub_maps

FFMPEG  = '/usr/local/bin/ffmpeg'
OUTDIR  = Path('/Volumes/6tb-R1/PlexOptimized/test_v102')
OUTDIR.mkdir(parents=True, exist_ok=True)

TESTS = [
    {
        'id': 'T1',
        'desc': 'eia_608 MP4 + MJPEG attached_pic — RuPaul S08E03',
        'src':  "/Volumes/tv/RuPaul's Drag Race (2009) {tmdb-8514}/Season 08/RuPaul's Drag Race - S08E03 - RuCo's Empire.mp4",
        'expect_issue': 'mjpeg',
        'expect_no_codec': ['eia_608', 'mjpeg'],
    },
    {
        'id': 'T2',
        'desc': 'PGS subs + DTS audio MKV — DTS must be transcoded, PGS dropped',
        'src':  '/Volumes/movies/Belladonna of Sadness (1973) {tmdb-64847}/Belladonna of Sadness (1973).mkv',
        'expect_issue': 'pgs_vobsub',
        'expect_no_codec': ['dts', 'hdmv_pgs_subtitle'],
    },
    {
        'id': 'T3',
        'desc': 'DTS + MJPEG attached_pic MKV — -map 0:v:0 strips thumbnail',
        'src':  '/Volumes/movies/Broker (2022) {tmdb-736732}/Broker (2022).mkv',
        'expect_issue': 'dts',
        'expect_no_codec': ['dts', 'mjpeg'],
        'expect_no_attached_pic': True,
    },
    {
        'id': 'T4',
        'desc': 'pcm_bluray M2TS → MKV, pcm_bluray transcoded to flac',
        'src':  '/Volumes/movies/9 Songs (2004) {tmdb-27}/9 Songs (2004).m2ts',
        'expect_issue': 'bad_container_ts',
        'expect_no_codec': ['pcm_bluray'],
        'expect_has_codec': ['flac'],
    },
]

PASS = FAIL = 0

for t in TESTS:
    src = t['src']
    tid = t['id']
    dst = str(OUTDIR / f"{tid}_out{Path(src).suffix if t.get('expect_issue') not in ('bad_container_avi','bad_container_ts','bad_container_m4v') else '.mkv'}")
    # fix extension for container changes
    if t.get('expect_issue') == 'bad_container_ts':
        dst = str(OUTDIR / f"{tid}_out.mkv")
    elif t.get('expect_issue') == 'bad_container_avi':
        dst = str(OUTDIR / f"{tid}_out.mp4")
    else:
        dst = str(OUTDIR / f"{tid}_out{Path(src).suffix}")

    print(f"\n{'='*60}")
    print(f"[{tid}] {t['desc']}")
    print(f"  src: {Path(src).name}")

    if not os.path.exists(src):
        print(f"  ❌ SKIP — source file not found")
        FAIL += 1
        continue

    # Classify
    pd = probe(src)
    if not pd:
        print(f"  ❌ FAIL — ffprobe failed")
        FAIL += 1
        continue

    issue = classify_probe(pd)
    print(f"  classified: {issue}  (expected: {t['expect_issue']})")
    if issue != t['expect_issue']:
        print(f"  ❌ FAIL — wrong classification")
        FAIL += 1
        continue

    # Show what text_sub_maps produces (should NOT include eia_608 etc)
    tsm = text_sub_maps(pd)
    print(f"  text_sub_maps: {tsm}")

    # Build command
    cmd = build_cmd(issue, src, dst, probe_data=pd)
    print(f"  ffmpeg cmd: {' '.join(cmd[8:])[:120]}...")  # skip base prefix

    # Clean stale output
    if Path(dst).exists():
        Path(dst).unlink()

    # Run
    t0 = time.time()
    print(f"  running ffmpeg...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    elapsed = time.time() - t0
    print(f"  exit={proc.returncode}  elapsed={elapsed:.0f}s")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '')[-300:].strip()
        print(f"  ❌ FAIL — ffmpeg error: {err}")
        FAIL += 1
        continue

    # Verify with plexfix.verify
    ok, msg = verify(src, dst)
    print(f"  verify: {ok} — {msg}")

    # Additional codec checks
    out_pd = probe(dst)
    if not out_pd:
        print(f"  ❌ FAIL — can't probe output")
        FAIL += 1
        continue

    fail_reasons = []
    out_codecs = [s.get('codec_name','').lower() for s in out_pd.get('streams',[])]
    out_attached = [s for s in out_pd.get('streams',[])
                    if s.get('disposition',{}).get('attached_pic')]

    for bad in t.get('expect_no_codec', []):
        if bad in out_codecs:
            fail_reasons.append(f"  codec '{bad}' still present in output")

    for want in t.get('expect_has_codec', []):
        if want not in out_codecs:
            fail_reasons.append(f"  expected codec '{want}' not found in output")

    if t.get('expect_no_attached_pic') and out_attached:
        fail_reasons.append(f"  attached_pic streams still present: {[s.get('codec_name') for s in out_attached]}")

    if not ok:
        fail_reasons.append(f"  verify failed: {msg}")

    if fail_reasons:
        for r in fail_reasons:
            print(f"  ❌ {r}")
        FAIL += 1
    else:
        print(f"  ✅ PASS")
        PASS += 1

print(f"\n{'='*60}")
print(f"RESULTS: {PASS} passed, {FAIL} failed out of {PASS+FAIL} tests")
sys.exit(0 if FAIL == 0 else 1)
