# Packaging

`packaging/build-mac.sh` → `dist/Ambient.dmg` (arm64, ad-hoc signed, not notarized).

1. PyInstaller freezes `engine/` (+ `ui/`, mitmproxy, the macOS redirector tarball, onnxruntime,
   and the two local models) into one binary, `ambient-engine` (`packaging/ambient-engine.spec`,
   entry `packaging/entry.py`).
2. Tauri v2 (`app/`) bundles it as a sidecar next to the shell in `Contents/MacOS/`.
3. `codesign --deep -s -`, then `hdiutil` makes the DMG (with an /Applications link).

`SKIP_ENGINE=1` reuses the last frozen engine. Test without touching local capture:
`AMBIENT_ENGINE_ARGS="--mode regular@18081 --set ambient_ui=18788 --set listen_host=127.0.0.1" Ambient.app/Contents/MacOS/ambient`

Models: the spec takes the files `engine/decide.py` names (`JUDGE_REPO`, `EMBED_REPO` × `MODEL_FILES`,
~130 MB) from the Hugging Face cache (downloading them at build time if absent) and ships them as
`models/<repo>/<file>`. When frozen, `engine/__main__.py` sets `AMBIENT_MODELS_DIR` to that folder and
`decide.py` then never fetches anything; in dev it is unset and the Hugging Face cache is used.

Test the frozen engine on its own (a plain proxy on a spare port; an empty `HF_HOME` proves the models
come from the binary). Then try `/api/setup`, `/panel`, `/assets/app.js`, and a POST to `/api/triggers/suggest`
(`"model":"local"`):
`AMBIENT_AGENT_CMD=cat HF_HUB_OFFLINE=1 HF_HOME=$(mktemp -d) build/engine-dist/ambient-engine --mode regular@18093 --set listen_host=127.0.0.1 --set ambient_ui=18799 --set ambient_db=$(mktemp -d)/t.db`
While another engine is capturing this Mac, add `--set ssl_verify_upstream_trusted_ca=$HOME/.mitmproxy/mitmproxy-ca-cert.pem`:
the test engine's own upstream connections are intercepted too, and it only trusts public CAs.

Runtime: engine log `~/Library/Logs/Ambient/engine.log`, DB `~/Library/Application Support/Ambient/ambient.db`.
The engine exits on its own if the app dies (AMBIENT_PARENT_PID watchdog).
Unsigned: on recent macOS, first launch is blocked; allow it in System Settings → Privacy & Security → "Open Anyway".

Windows: `packaging/windows/build-windows.ps1` → `dist\Ambient_<version>_x64-setup.exe` and `.msi`; see `docs/windows.md`.
