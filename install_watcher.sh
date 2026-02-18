#!/bin/bash
# install_watcher.sh — Install or uninstall the plexwatcher launchd agent
#
# USAGE:
#   ./install_watcher.sh            # install and start
#   ./install_watcher.sh uninstall  # stop and remove
#   ./install_watcher.sh status     # show current status
#   ./install_watcher.sh restart    # reload agent

set -euo pipefail

LABEL="com.mproadmin.plexwatcher"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/com.mproadmin.plexwatcher.plist"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$(cd "$(dirname "$0")" && pwd)/results"

case "${1:-install}" in

  install)
    echo "=== Installing plexwatcher launchd agent ==="
    echo "  Plist source : $PLIST_SRC"
    echo "  Plist dest   : $PLIST_DST"

    # Ensure results dir exists for log files
    mkdir -p "$LOG_DIR"

    # Unload existing agent if present
    if launchctl list | grep -q "$LABEL" 2>/dev/null; then
        echo "  Unloading existing agent..."
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi

    # Copy plist to LaunchAgents
    cp "$PLIST_SRC" "$PLIST_DST"
    echo "  Copied plist to LaunchAgents"

    # Validate plist syntax
    plutil -lint "$PLIST_DST" && echo "  Plist syntax: OK"

    # Load and start
    launchctl load "$PLIST_DST"
    echo "  Agent loaded. First run will start within seconds."
    echo ""
    echo "  launchctl list | grep plexwatcher"
    launchctl list | grep "$LABEL" || echo "  (not listed yet — give it a moment)"
    echo ""
    echo "  Watch live output:"
    echo "    tail -f \"$LOG_DIR/watcher_launchd_stdout.log\""
    ;;

  uninstall)
    echo "=== Uninstalling plexwatcher ==="
    launchctl unload "$PLIST_DST" 2>/dev/null && echo "  Agent unloaded" || echo "  Agent was not loaded"
    rm -f "$PLIST_DST" && echo "  Plist removed" || echo "  Plist not found"
    ;;

  restart)
    echo "=== Restarting plexwatcher ==="
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    sleep 1
    launchctl load "$PLIST_DST"
    echo "  Reloaded."
    ;;

  status)
    echo "=== plexwatcher status ==="
    launchctl list | grep "$LABEL" || echo "  Not loaded"
    echo ""
    echo "  Last 20 log lines:"
    tail -20 "$LOG_DIR/watcher_launchd_stdout.log" 2>/dev/null || echo "  (no log yet)"
    echo ""
    echo "  Stderr (errors):"
    tail -5 "$LOG_DIR/watcher_launchd_stderr.log" 2>/dev/null || echo "  (no errors)"
    ;;

  run-now)
    echo "=== Triggering immediate run ==="
    launchctl start "$LABEL"
    sleep 2
    tail -20 "$LOG_DIR/watcher_launchd_stdout.log" 2>/dev/null
    ;;

  *)
    echo "Usage: $0 [install|uninstall|restart|status|run-now]"
    exit 1
    ;;
esac
