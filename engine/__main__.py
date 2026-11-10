"""Ambient engine entry point: runs mitmdump in-process with the Ambient addon.

    python -m engine [extra mitmdump args]      # dev
    ambient-engine [extra mitmdump args]        # frozen (PyInstaller) sidecar

Defaults: capture every app via mitmproxy's local redirector (VPN tunnels and call apps
excluded; see osal.capture_spec), UI on 127.0.0.1:8787, DB in the per-OS support folder. Anything
passed on the command line wins (e.g. --mode regular@18081, --set ambient_ui=18788).

Env:
  AMBIENT_LOG_FILE    redirect stdout/stderr (incl. native logs) to this file
  AMBIENT_PARENT_PID  exit cleanly (SIGINT to self) when this process dies
  AMBIENT_STOP_ON_STDIN_EOF  exit cleanly when stdin closes (how the Windows app asks the engine to stop)
  AMBIENT_UI_DIR      where ui/index.html lives (set automatically when frozen)
  AMBIENT_MODELS_DIR  where the local models live as <repo>/<file> (set automatically when frozen;
                      unset in dev, where decide.py uses the Hugging Face cache)
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

def _root() -> Path:
    # Frozen: PyInstaller unpacks data files under sys._MEIPASS. Dev: the repo root.
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def _redirect_logs(path: str) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    sys.stdout = os.fdopen(1, "w", buffering=1, **_stdio_kw())
    sys.stderr = os.fdopen(2, "w", buffering=1, **_stdio_kw())


def _stdio_kw() -> dict:
    from engine import osal
    enc = osal.stdio_encoding()
    return {"encoding": enc, "errors": "replace"} if enc else {}


def _watch_parent(pid: int) -> None:
    from engine import osal

    def loop():
        while True:
            time.sleep(1)
            if not osal.pid_alive(pid):
                # Parent app is gone: shut down like Ctrl-C so the redirector stops intercepting.
                osal.interrupt_self()
                time.sleep(5)
                os._exit(0)
    threading.Thread(target=loop, daemon=True).start()


def _watch_stdin() -> None:
    from engine import osal

    def loop():
        if not osal.wait_stdin_closed():
            return                      # no usable stdin: nothing to watch
        osal.interrupt_self()           # stdin closed: the app asked us to stop (or died)
        time.sleep(5)
        os._exit(0)
    threading.Thread(target=loop, daemon=True).start()


def build_args(argv: list[str]) -> list[str]:
    root = _root()
    from engine import osal
    support = osal.support_dir()

    args = ["-s", str(root / "engine" / "addon.py")]
    has_mode = any(a == "--mode" or a.startswith("--mode=") or a == "-m" for a in argv)
    if not has_mode:
        spec = osal.capture_spec()
        # None: capture can't start here yet (Linux before setup). Come up anyway, so the UI can say what to do.
        args += ["--mode", f"local:{spec}"] if spec is not None else ["--set", "server=false"]

    given = set()
    for i, a in enumerate(argv):
        if a == "--set" and i + 1 < len(argv):
            given.add(argv[i + 1].split("=", 1)[0])
        elif a.startswith("--set="):
            given.add(a[6:].split("=", 1)[0])
    defaults = {
        "flow_detail": "0",
        "termlog_verbosity": "warn",
        "ambient_ui": "8787",
        "ambient_db": str(support / "ambient.db"),
    }
    ca_bundle = osal.upstream_ca_bundle()       # Windows: also trust what the PC trusts (None elsewhere)
    if ca_bundle:
        defaults["ssl_verify_upstream_trusted_ca"] = ca_bundle
    for k, v in defaults.items():
        if k not in given:
            args += ["--set", f"{k}={v}"]
    return args + argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("AMBIENT_LOG_FILE"):
        _redirect_logs(os.environ["AMBIENT_LOG_FILE"])
    elif _stdio_kw():
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.reconfigure(**_stdio_kw())
    if os.environ.get("AMBIENT_STOP_ON_STDIN_EOF"):
        _watch_stdin()
    if os.environ.get("AMBIENT_PARENT_PID", "").isdigit():
        _watch_parent(int(os.environ["AMBIENT_PARENT_PID"]))
    os.environ.setdefault("AMBIENT_UI_DIR", str(_root() / "ui"))
    if getattr(sys, "frozen", False):
        # Packaged app: the local models ship inside it; with this set, decide.py never fetches one.
        os.environ.setdefault("AMBIENT_MODELS_DIR", str(_root() / "models"))

    from engine import osal
    osal.adopt_user_path()          # opened from Finder / a launcher: find agents in ~/.local/bin, Homebrew…
    osal.prepare_capture()          # Linux: start the installed capture helper without sudo (no-op elsewhere)

    from mitmproxy.tools.main import mitmdump
    return mitmdump(build_args(argv)) or 0


if __name__ == "__main__":
    sys.exit(main())
