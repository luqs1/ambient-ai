#!/usr/bin/env bash
# Build Ambient.app and dist/Ambient.dmg (unsigned: ad-hoc signature only).
#   packaging/build-mac.sh            full build
#   SKIP_ENGINE=1 packaging/build-mac.sh   reuse the last frozen engine
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TRIPLE="$(rustc -vV | sed -n 's/^host: //p')"   # aarch64-apple-darwin
VERSION="$(sed -n 's/.*"version": "\(.*\)".*/\1/p' app/src-tauri/tauri.conf.json | head -1)"

echo "==> 1/4 freezing the engine (PyInstaller)"
if [[ -z "${SKIP_ENGINE:-}" || ! -f build/engine-dist/ambient-engine ]]; then
  uv run --with pyinstaller pyinstaller --noconfirm --log-level WARN \
    --distpath build/engine-dist --workpath build/engine-work packaging/ambient-engine.spec
fi
mkdir -p app/src-tauri/binaries
cp build/engine-dist/ambient-engine "app/src-tauri/binaries/ambient-engine-$TRIPLE"

echo "==> 2/4 building Ambient.app (Tauri)"
cd app
[[ -d node_modules ]] || bun install
bunx tauri build --bundles app
APP="$ROOT/app/src-tauri/target/release/bundle/macos/Ambient.app"
cd "$ROOT"

echo "==> 3/4 ad-hoc signing"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP" && echo "signature ok (ad-hoc)"

echo "==> 4/4 making dist/Ambient.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
mkdir -p dist
rm -f dist/Ambient.dmg
hdiutil create -quiet -volname "Ambient $VERSION" -srcfolder "$STAGE" -ov -format UDZO dist/Ambient.dmg
rm -rf "$STAGE"
ls -lh dist/Ambient.dmg
