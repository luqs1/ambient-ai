#!/usr/bin/env bash
# Run the engine with live capture from a SNAPSHOT of the repo, plus a network watchdog.
#   scripts/live.sh start|stop|restart|status|ui   (ui = copy UI files into the snapshot, no restart)
# Why a snapshot: mitmproxy hot-reloads engine/addon.py when it changes, which kills the UI
# server. Developing in the repo while capture runs from a copy avoids that.
# Watchdog (the escape hatch): if the internet stops working through the capture, or the
# engine pegs the CPU for 2 minutes, it stops the engine so the Mac's network goes back to normal.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SUP="$HOME/Library/Application Support/Ambient"
SNAP="$SUP/live"
LOG="$HOME/Library/Logs/Ambient"; mkdir -p "$LOG" "$SUP"
PIDF="$SUP/engine.pid"; WPIDF="$SUP/watchdog.pid"
EXCL='!io.tailscale.ipn.macsys.network-extension,!Tailscale,!ch.protonvpn.mac.WireGuard-Extension,!ch.protonvpn.mac.Transparent-Proxy,!ProtonVPN,!zoom.us,!CptHost,!avconferenced,!FaceTime,!Microsoft Teams,!MSTeams,!Webex,!Around,!Tuple,!Claude,!claude,!1Password,!Bitwarden,!Monzo'

epid() { pgrep -f -- "-m engine --mode local" | head -1; }

stop_engine() {
  pkill -INT -f -- "-m engine --mode local" 2>/dev/null
  for _ in $(seq 1 12); do [ -z "$(epid)" ] && return 0; sleep 1; done
  pkill -KILL -f -- "-m engine --mode local" 2>/dev/null; sleep 1
}

watchdog() {
  local fails=0 hot=0
  echo "$(date '+%F %T') watchdog up" >> "$LOG/watchdog.log"
  while true; do
    sleep 20
    local pid; pid="$(epid)"
    [ -z "$pid" ] && { echo "$(date '+%F %T') engine gone, watchdog exits" >> "$LOG/watchdog.log"; exit 0; }
    if curl -s -o /dev/null -m 8 https://example.com || curl -s -o /dev/null -m 8 https://www.apple.com/library/test/success.html; then fails=0; else fails=$((fails+1)); fi
    local cpu; cpu=$(ps -o %cpu= -p "$(pgrep -f -- 'python3 -u -m engine --mode local' | head -1)" 2>/dev/null | awk '{print int($1)}')
    if [ "${cpu:-0}" -gt 250 ]; then hot=$((hot+1)); else hot=0; fi
    if [ $fails -ge 3 ] || [ $hot -ge 6 ]; then
      echo "$(date '+%F %T') TRIPPED fails=$fails hot=$hot cpu=${cpu:-?} -> stopping engine" >> "$LOG/watchdog.log"
      stop_engine; exit 1
    fi
  done
}

case "${1:-status}" in
  start)
    [ -n "$(epid)" ] && { echo "already running (pid $(epid))"; exit 0; }
    # A packaged engine that is capturing means the app is running: never start a second capture.
    # (A packaged engine in proxy test mode, `--mode regular@…`, doesn't capture and is fine.)
    pgrep -fl "ambient-engine" | grep -v -- "--mode regular" | grep -q . && { echo "a packaged ambient-engine is capturing; not starting a second capture"; exit 1; }
    mkdir -p "$SNAP"
    rsync -a --delete --exclude __pycache__ "$REPO/engine" "$REPO/ui" "$SNAP/"
    # Triggers run a stand-in agent unless AMBIENT_REAL_AGENT=1, so tests never send real messages to a model.
    [ "${AMBIENT_REAL_AGENT:-0}" = "1" ] || export AMBIENT_AGENT_CMD="$REPO/.venv/bin/python3 $REPO/scripts/standin_agent.py"
    export HF_HUB_OFFLINE=1
    # Fully detached (own stdio, exec'd): nothing here may keep a caller's pipe open.
    ( cd "$SNAP" && AMBIENT_UI_DIR="$SNAP/ui" exec nohup "$REPO/.venv/bin/python3" -u -m engine --mode "local:$EXCL" ${AMBIENT_ARGS:-} >> "$LOG/engine-dev.log" 2>&1 ) </dev/null >/dev/null 2>&1 &
    echo $! > "$PIDF"
    for _ in $(seq 1 30); do curl -s -o /dev/null -m 2 http://127.0.0.1:8787/api/setup && break; sleep 1; done
    curl -s -o /dev/null -m 2 http://127.0.0.1:8787/api/setup || { echo "engine did not come up; see $LOG/engine-dev.log"; stop_engine; exit 1; }
    ( watchdog ) </dev/null >/dev/null 2>&1 &
    echo $! > "$WPIDF"; disown -a 2>/dev/null || true
    echo "started (pid $(epid)), snapshot at $SNAP"
    ;;
  stop)
    [ -f "$WPIDF" ] && kill "$(cat "$WPIDF")" 2>/dev/null; rm -f "$WPIDF"
    stop_engine; echo "stopped"
    ;;
  restart)
    "$0" stop
    for _ in $(seq 1 10); do lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 1; done   # wait for the port
    "$0" start || { sleep 3; "$0" start; } ;;
  ui) rsync -a --delete "$REPO/ui" "$SNAP/" && echo "ui synced (no restart needed)" ;;
  watchdog) watchdog ;;
  status)
    if [ -n "$(epid)" ]; then echo "engine pid $(epid)"; else echo "engine not running"; fi
    curl -s -m 3 http://127.0.0.1:8787/api/status || echo "(ui not answering)"; echo
    curl -s -o /dev/null -m 8 -w "internet %{http_code}\n" https://example.com
    tail -3 "$LOG/watchdog.log" 2>/dev/null
    ;;
esac
