#!/bin/bash
# Offload /tank/holding personal video to local disks, then verify (count+bytes).
# personal-video       -> /Volumes/4tb-R1/tank-offload  (472742973386 bytes / 2706 files)
# personal-video-joined-> /Volumes/6tb-R1/tank-offload  (148329960000 bytes / 80 files)
# Does NOT delete anything from tank. Writes PASS/FAIL to the log.
set -u
RSYNC=/usr/local/bin/rsync   # Homebrew rsync 3.4.1
LOG=/Volumes/4tb-R1/tank-offload/offload.log
: > "$LOG"
exec >>"$LOG" 2>&1
echo "==== OFFLOAD START $(date) ===="
"$RSYNC" --version | head -1

run_rsync () { # src dst
  echo "--- rsync $1 -> $2 ---"
  caffeinate -i nice -n 5 "$RSYNC" -rltD --info=stats2 "$1/" "$2/"
  echo "rsync rc=$?"
}

verify () { # dst expFiles expBytes label  (count + byte-size match; rsync block-checksums during transfer)
  local dst="$1" ec="$2" eb="$3" label="$4" c b
  c=$(find "$dst" -type f | wc -l | tr -d ' ')
  b=$(find "$dst" -type f -print0 | xargs -0 stat -f '%z' | awk '{s+=$1} END{print s+0}')
  echo "VERIFY $label: dst files=$c bytes=$b | expect files=$ec bytes=$eb"
  if [ "$c" = "$ec" ] && [ "$b" = "$eb" ]; then echo "VERIFY $label: PASS"; return 0
  else echo "VERIFY $label: FAIL"; return 1; fi
}

run_rsync /Volumes/holding/personal-video        /Volumes/4tb-R1/tank-offload/personal-video
run_rsync /Volumes/holding/personal-video-joined /Volumes/6tb-R1/tank-offload/personal-video-joined

P=1
verify /Volumes/4tb-R1/tank-offload/personal-video        2706 472742973386 personal-video        || P=0
verify /Volumes/6tb-R1/tank-offload/personal-video-joined 80   148329960000 personal-video-joined || P=0

if [ "$P" = 1 ]; then echo "OFFLOAD RESULT: PASS"; else echo "OFFLOAD RESULT: FAIL"; fi
echo "==== OFFLOAD END $(date) ===="
