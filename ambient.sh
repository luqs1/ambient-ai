#!/usr/bin/env bash
# ambient launcher.
#   ./ambient.sh setup               trust the ambient CA in the System keychain (asks for your password)
#   ./ambient.sh start [apps]        capture every app on this Mac in the background (local mode)
#   ./ambient.sh stop | status       stop / check the background capture
#   ./ambient.sh local [apps]        same as start, in the foreground. apps: 'curl,node' or '!Safari'
#   ./ambient.sh proxy [port]        explicit proxy on 127.0.0.1:8080, no system changes (testing)
#   ./ambient.sh wireguard           be a WireGuard VPN server; point a phone at it
#   ./ambient.sh env                 env vars so Node/Python/curl also trust the CA
#   ./ambient.sh uninstall           remove the CA from the keychain
# Extra mitmproxy options go after `--`. UI: http://127.0.0.1:8787
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
BUNDLE="$PWD/data/ca-bundle.pem"
PIDF=data/ambient.pid
# Never intercept VPN tunnels themselves (they carry everyone else's traffic).
VPN_EXCLUDES='!io.tailscale.ipn.macsys.network-extension,!Tailscale,!ch.protonvpn.mac.WireGuard-Extension,!ch.protonvpn.mac.Transparent-Proxy,!ProtonVPN'

cmd="${1:-help}"; shift || true
arg=""; if [[ $# -gt 0 && "$1" != "--" ]]; then arg="$1"; shift; fi
[[ "${1:-}" == "--" ]] && shift

local_spec() {  # user's app filter (if any) plus the VPN exclusions
  if [[ -z "$arg" ]]; then echo "local:$VPN_EXCLUDES"
  elif [[ "$arg" == !* ]]; then echo "local:$arg,$VPN_EXCLUDES"
  else echo "local:$arg"; fi
}
run() { exec uv run mitmdump -s engine/addon.py --mode "$1" --set flow_detail=0 "${@:2}"; }

case "$cmd" in
  setup)
    uv run python -c 'import mitmproxy.certs'  # make sure the CA exists
    [[ -f "$CA" ]] || uv run mitmdump --version >/dev/null
    echo "macOS will ask for your password to trust the ambient CA (user keychain)..."
    security add-trusted-cert -r trustRoot -k "$HOME/Library/Keychains/login.keychain-db" "$CA"
    security verify-cert -c "$CA" >/dev/null 2>&1 && echo "CA trusted." || echo "CA not trusted yet (check Keychain Access)."
    ;;
  uninstall)
    security remove-trusted-cert "$CA" 2>/dev/null || true
    security delete-certificate -c mitmproxy "$HOME/Library/Keychains/login.keychain-db" && echo "CA removed." ;;
  env)
    PYB=$(uv run python -c 'import certifi;print(certifi.where())' 2>/dev/null || echo /etc/ssl/cert.pem)
    cat "$PYB" "$CA" > "$BUNDLE"
    cat <<E
# Apps that ignore the macOS keychain. Bundle = normal public CAs + ambient CA,
# so passed-through connections still verify.
export NODE_EXTRA_CA_CERTS="$CA"
export REQUESTS_CA_BUNDLE="$BUNDLE"
export SSL_CERT_FILE="$BUNDLE"
export CURL_CA_BUNDLE="$BUNDLE"
E
    ;;
  proxy)     run "regular@${arg:-8080}" --set listen_host=127.0.0.1 "$@" ;;
  wireguard) run wireguard "$@" ;;
  local)     run "$(local_spec)" "$@" ;;
  start)
    if [[ -f $PIDF ]] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "already running (pid $(cat $PIDF))"; exit 0; fi
    nohup uv run mitmdump -s engine/addon.py --mode "$(local_spec)" --set flow_detail=0 --set termlog_verbosity=warn "$@" \
      > data/ambient.log 2>&1 &
    echo $! > $PIDF; sleep 4
    if kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "ambient capturing (pid $(cat $PIDF)) → http://127.0.0.1:8787"
    else echo "failed to start:"; tail -20 data/ambient.log; exit 1; fi ;;
  stop)
    if [[ -f $PIDF ]]; then
      pid=$(cat $PIDF); pkill -INT -P "$pid" 2>/dev/null || true; kill -INT "$pid" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$pid" 2>/dev/null && kill "$pid"; rm -f $PIDF; echo stopped
    else echo "not running"; fi ;;
  status)
    if [[ -f $PIDF ]] && kill -0 "$(cat $PIDF)" 2>/dev/null; then echo "running (pid $(cat $PIDF))"; tail -5 data/ambient.log
    else echo "not running"; fi ;;
  *) sed -n '2,12p' "$0" ;;
esac
