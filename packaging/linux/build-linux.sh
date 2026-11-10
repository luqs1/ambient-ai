#!/usr/bin/env bash
# Build Ambient for Linux: dist/ambient_<version>_<arch>.deb
#   packaging/linux/build-linux.sh              full build
#   SKIP_ENGINE=1 packaging/linux/build-linux.sh   reuse the last frozen engine
#
# Runs on Ubuntu 24.04 (or any Debian-family box) with: uv, Rust, bun, and Tauri v2's Linux build
# packages (libwebkit2gtk-4.1-dev build-essential curl wget file libxdo-dev libssl-dev
# libayatana-appindicator3-dev librsvg2-dev). docs/linux.md says what the package does when installed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/packaging/linux"
cd "$ROOT"
[[ "$(uname -s)" == Linux ]] || { echo "build-linux.sh runs on Linux" >&2; exit 1; }
TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"            # x86_64-unknown-linux-gnu
VERSION="$(sed -n 's/.*"version": "\(.*\)".*/\1/p' app/src-tauri/tauri.conf.json | head -1)"
OUT="$ROOT/build/linux"
mkdir -p "$OUT" dist

echo "==> 1/4 freezing the engine (PyInstaller)"
uv sync --quiet
if [[ -z "${SKIP_ENGINE:-}" || ! -f build/engine-dist/ambient-engine ]]; then
  # PyInstaller goes into the project's own environment (not `uv run --with`): mitmproxy_linux's PyInstaller
  # hook looks for the capture helper in the environment's bin folder.
  uv pip install --quiet pyinstaller
  .venv/bin/python -m PyInstaller --noconfirm --log-level WARN \
    --distpath build/engine-dist --workpath build/engine-work "$HERE/ambient-engine-linux.spec"
fi
mkdir -p app/src-tauri/binaries
cp build/engine-dist/ambient-engine "app/src-tauri/binaries/ambient-engine-$TRIPLE"

echo "==> 2/4 capture helper (mitmproxy's Linux redirector, from the mitmproxy_linux wheel)"
REDIRECTOR="$(.venv/bin/python -c 'import mitmproxy_linux; print(mitmproxy_linux.executable_path())')"
[[ -x "$REDIRECTOR" ]] || { echo "mitmproxy-linux-redirector not found at $REDIRECTOR" >&2; exit 1; }
install -m 0755 "$REDIRECTOR" "$OUT/ambient-redirector"
install -m 0755 "$HERE/ambient-setup" "$OUT/ambient-setup"

echo "==> 3/4 building the shell and the .deb (Tauri)"
# Linux-only bundle settings, merged over app/src-tauri/tauri.conf.json (which stays as the Mac build wants it).
cat > "$OUT/tauri.linux.conf.json" <<JSON
{
  "bundle": {
    "targets": ["deb"],
    "linux": {
      "deb": {
        "section": "net",
        "depends": ["libcap2-bin", "libnss3-tools", "pkexec | policykit-1", "ca-certificates", "openssl", "procps"],
        "files": {
          "/usr/lib/ambient/ambient-redirector": "$OUT/ambient-redirector",
          "/usr/lib/ambient/ambient-setup": "$OUT/ambient-setup",
          "/usr/share/polkit-1/actions/dev.ambient.desktop.policy": "$HERE/dev.ambient.desktop.policy"
        },
        "postInstallScript": "$HERE/postinst",
        "preRemoveScript": "$HERE/prerm",
        "postRemoveScript": "$HERE/postrm"
      }
    }
  }
}
JSON
cd app
[[ -d node_modules ]] || bun install
bunx tauri build --bundles deb --config "$OUT/tauri.linux.conf.json"
cd "$ROOT"

echo "==> 4/4 collecting"
DEB="$(ls -t app/src-tauri/target/release/bundle/deb/*.deb | head -1)"
cp "$DEB" dist/
ls -lh "dist/$(basename "$DEB")"
dpkg-deb --info "dist/$(basename "$DEB")" | sed -n '1,25p'
