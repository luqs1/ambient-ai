"""OS abstraction: everything that differs between macOS, Windows and Linux lives here.

The rest of the engine asks this module for paths, process info, certificate status,
app icons and how to open things, and never branches on the OS itself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = "darwin" if sys.platform == "darwin" else "windows" if sys.platform.startswith("win") else "linux"
HOME = Path.home()


def run(cmd: list[str], timeout: float = 5) -> tuple[int, str]:
    try:
        kw = {}
        if SYSTEM == "windows":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash from a tray app
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace", **kw)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, repr(e)


# ---------- where things live ----------
def support_dir() -> Path:
    if SYSTEM == "darwin":
        p = HOME / "Library" / "Application Support" / "Ambient"
    elif SYSTEM == "windows":
        p = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local") / "Ambient"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "ambient"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    if SYSTEM == "darwin":
        p = HOME / "Library" / "Caches" / "Ambient"
    elif SYSTEM == "windows":
        p = support_dir() / "cache"
    else:
        p = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache") / "ambient"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ca_path() -> Path:
    return HOME / ".mitmproxy" / "mitmproxy-ca-cert.pem"


# ---------- local capture ----------
def default_excludes() -> str:
    """Processes local capture must leave alone: VPN tunnels (they'd loop) and call apps
    (WebRTC over UDP breaks under capture)."""
    if SYSTEM == "darwin":
        vpn = ["io.tailscale.ipn.macsys.network-extension", "Tailscale", "ch.protonvpn.mac.WireGuard-Extension",
               "ch.protonvpn.mac.Transparent-Proxy", "ProtonVPN"]
        calls = ["zoom.us", "CptHost", "avconferenced", "FaceTime", "Microsoft Teams", "MSTeams", "Webex",
                 "Around", "Tuple"]
    elif SYSTEM == "windows":
        # sshd.exe: Windows capture also picks up connections a local server accepts; a remote shell must
        # not ride through (and die with) the engine.
        vpn = ["tailscaled.exe", "tailscale-ipn.exe", "ProtonVPN.exe", "ProtonVPN.WireGuardService.exe",
               "wireguard.exe", "sshd.exe"]
        calls = ["Zoom.exe", "CptHost.exe", "ms-teams.exe", "Teams.exe", "webex.exe"]
    else:
        return ",".join("!" + x for x in _LINUX_EXCLUDES)
    return ",".join("!" + x for x in vpn + calls)


def capture_spec() -> str | None:
    """What follows `local:` in mitmproxy's mode when nothing was given on the command line.
    macOS and Windows: every app except default_excludes(). Linux: see _linux_capture_spec().
    None: capture cannot start here right now (capture_status() says why), so the engine comes up
    without it rather than not at all."""
    if SYSTEM == "linux":
        return _linux_capture_spec()
    return default_excludes()


def prepare_capture() -> None:
    """Called once by the engine before mitmproxy starts. Only Linux has anything to prepare."""
    if SYSTEM == "linux":
        _linux_prepare_capture()


def capture_status() -> dict:
    """Can mitmproxy's local capture run here, and if not, what's missing (in plain words)."""
    if SYSTEM == "darwin":
        _, out = run(["systemextensionsctl", "list"])
        line = next((ln for ln in out.splitlines() if "mitmproxy" in ln.lower()), "")
        on = "activated enabled" in line
        return {"ok": on, "seen": bool(line),
                "fix": "" if on else "Allow “Mitmproxy Redirector” in System Settings → General → Login Items & Extensions."}
    if SYSTEM == "windows":
        return _windows_capture_status()
    return _linux_capture_status()


def ca_trusted() -> bool:
    ca = ca_path()
    if not ca.exists():
        return False
    if SYSTEM == "darwin":
        return run(["security", "verify-cert", "-c", str(ca)])[0] == 0
    if SYSTEM == "windows":
        return _windows_ca_in_root(ca)
    return _linux_ca_in_system_store(ca)


def trust_ca_hint() -> str:
    if SYSTEM == "darwin":
        return "Open it in Keychain Access and set it to “Always Trust”, so Ambient can read your apps."
    if SYSTEM == "windows":
        return ("Double-click it, choose Install Certificate → Current User → “Place all certificates in the following "
                "store” → Trusted Root Certification Authorities, then answer Yes, so Ambient can read your apps.")
    return ("Copy it to /usr/local/share/ca-certificates/ambient.crt and run sudo update-ca-certificates, "
            "so Ambient can read your apps. Firefox and Chrome keep their own lists: import it there too "
            "(Settings → Certificates → Authorities).")


def trust_ca() -> dict:
    """Ask the OS to trust Ambient's certificate, with the OS's own confirmation prompt.
    Returns {"ok": bool, "note": what the user should expect or do next}."""
    ca = ca_path()
    if not ca.exists():
        return {"ok": False, "note": "The certificate doesn't exist yet. Start Ambient once first."}
    if SYSTEM == "darwin":
        keychain = HOME / "Library" / "Keychains" / "login.keychain-db"
        code, out = run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-k", str(keychain), str(ca)], 120)
        return {"ok": code == 0, "note": "Trusted in your login keychain." if code == 0 else
                "macOS didn't confirm. Open the certificate in Keychain Access and set it to Always Trust."}
    if SYSTEM == "windows":
        return _windows_trust_ca(ca)
    return _linux_trust_ca(ca)


def open_path(p: Path | str) -> None:
    p = str(p)
    if SYSTEM == "darwin":
        run(["open", p])
    elif SYSTEM == "windows":
        try:
            os.startfile(p)  # type: ignore[attr-defined]
        except Exception:
            pass
    else:
        run(["xdg-open", p])


# ---------- agents trusting our CA ----------
def shell_profile_target() -> str:
    return {"darwin": "~/.zshrc", "linux": "~/.bashrc", "windows": "your user environment variables"}[SYSTEM]


def protect_agents_installed() -> bool:
    if SYSTEM == "windows":
        code, out = run(["reg", "query", r"HKCU\Environment", "/v", "NODE_EXTRA_CA_CERTS"])
        return code == 0 and "mitmproxy" in out
    for name in (".zshrc", ".bashrc", ".profile"):
        f = HOME / name
        if f.exists() and "NODE_EXTRA_CA_CERTS" in f.read_text(errors="replace"):
            return True
    return False


def protect_agents_install() -> str:
    """Make new agent CLI sessions (Node/Bun) trust Ambient's CA. Returns a note for the user."""
    ca = ca_path()
    if SYSTEM == "windows":
        run(["setx", "NODE_EXTRA_CA_CERTS", str(ca)])
        return "Open a new terminal and start your agent again."
    line = ("\n# Ambient Veil: let Claude Code and other Node agents trust Ambient's certificate\n"
            f'export NODE_EXTRA_CA_CERTS="{ca}"\n')
    targets = [HOME / ".zshrc"] if SYSTEM == "darwin" else [HOME / ".bashrc"] + (
        [HOME / ".zshrc"] if (HOME / ".zshrc").exists() else [])
    for f in targets:
        text = f.read_text(errors="replace") if f.exists() else ""
        if "NODE_EXTRA_CA_CERTS" not in text:
            with open(f, "a") as fh:
                fh.write(line)
    return "Open a new terminal (or source your shell profile) and start your agent again."


# ---------- processes ----------
def app_name(path: str) -> str:
    """The name a person would recognise for an executable path."""
    if not path:
        return "unknown"
    if SYSTEM == "darwin":
        m = re.search(r"/([^/]+)\.app/", path + "/")   # outermost .app bundle: "Google Chrome"
        if m:
            return m.group(1)
        return _cli_name(path) or os.path.basename(path) or path
    base = re.split(r"[\\/]", path)[-1]
    if SYSTEM == "windows":
        base = re.sub(r"\.exe$", "", base, flags=re.I)
        return _WIN_NAMES.get(base.lower(), base)
    return _linux_app_name(path, base)


# Command-line tools named after something other than themselves: Claude Code installs each version as
# ~/.local/share/claude/versions/2.1.322, so the process is called "2.1.322".
_CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "opencode"}


def _cli_name(path: str) -> str:
    parts = [p for p in re.split(r"[\\/]", path) if p]
    base = parts[-1] if parts else ""
    if re.fullmatch(r"v?\d+(\.\d+)+([-+.\w]*)?", base):          # a version as the file name: use the folder it's in
        for p in reversed(parts[:-1]):
            if p not in ("versions", "bin", "libexec", "share", "local", ".local"):
                return _CLI_NAMES.get(p.lower(), p)
    return _CLI_NAMES.get(base.lower(), "")


_WIN_NAMES = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox", "slack": "Slack",
              "discord": "Discord", "code": "Visual Studio Code", "whatsapp": "WhatsApp", "zen": "Zen",
              "brave": "Brave", "notion": "Notion", "ms-teams": "Microsoft Teams", "outlook": "Outlook",
              "msedgewebview2": "Microsoft Edge WebView2", "opera": "Opera", "vivaldi": "Vivaldi",
              "librewolf": "LibreWolf", "floorp": "Floorp", "arc": "Arc", "chromium": "Chromium",
              "olk": "Outlook", "teams": "Microsoft Teams", "code - insiders": "Visual Studio Code",
              "cursor": "Cursor", "spotify": "Spotify", "telegram": "Telegram", "signal": "Signal",
              "onedrive": "OneDrive", "explorer": "File Explorer", "ambient": "Ambient"}
_LINUX_NAMES = {"chrome": "Google Chrome", "google-chrome": "Google Chrome", "chromium": "Chromium",
                "firefox": "Firefox", "firefox-bin": "Firefox", "slack": "Slack", "discord": "Discord",
                "code": "Visual Studio Code", "zen": "Zen", "zen-bin": "Zen", "brave": "Brave"}


def describe_pid(pid: int) -> dict:
    path = ""
    try:
        import psutil
        path = psutil.Process(pid).exe()
    except Exception:
        if SYSTEM != "windows":
            path = run(["ps", "-o", "comm=", "-p", str(pid)], 2)[1].strip()
    return {"pid": pid, "path": path, "app": app_name(path) if path else f"pid {pid}"}


def pid_environ(pid: int) -> dict:
    """Environment of one of the user's own processes ({} if we may not look)."""
    try:
        import psutil
        return dict(psutil.Process(pid).environ())
    except Exception:
        pass
    if SYSTEM == "darwin":
        out = run(["ps", "eww", "-o", "command=", "-p", str(pid)], 2)[1]
        env = {}
        for tok in out.split():
            k, sep, v = tok.partition("=")
            if sep and k.isupper():
                env[k] = v
        return env
    return {}


def pid_for_port(port: int, proto: str = "TCP") -> int | None:
    """Which local process owns the socket whose local port is `port` (regular-proxy mode only)."""
    if SYSTEM != "windows":
        code, out = run(["lsof", "-nP", "-F", "pn", f"-i{proto}:{port}"], 3)
        pid = None
        for line in out.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid and pid != os.getpid():
                if line[1:].split("->")[0].endswith(f":{port}"):
                    return pid
        if code == 0 or SYSTEM == "darwin":
            return None
    try:
        import psutil
        kind = "udp" if proto == "UDP" else "tcp"
        for c in psutil.net_connections(kind=kind):
            if c.laddr and c.laddr.port == port and c.pid and c.pid != os.getpid():
                return c.pid
    except Exception:
        pass
    return None


def is_user_app(path: str) -> bool:
    """An app a person installed and uses, as opposed to a system daemon."""
    if not path:
        return False
    if SYSTEM == "darwin":
        return ".app/" in path + "/" and not path.startswith(("/System/", "/usr/", "/Library/Apple/"))
    if SYSTEM == "windows":
        low = path.lower().replace("/", "\\")
        return not any(d in low for d in _WIN_SYSTEM_DIRS)
    return not path.startswith(("/usr/lib/systemd", "/usr/libexec", "/sbin", "/usr/sbin"))


# ---------- app icons ----------
def app_icon_png(app: str) -> bytes | None:
    """The app's own icon as a PNG of about 128px, or None. Cached on disk."""
    safe = re.sub(r"[^A-Za-z0-9 ._-]", "", app)[:60]
    if not safe:
        return None
    out = cache_dir() / "icons" / f"{safe}.png"
    if out.exists():
        return out.read_bytes() or None
    out.parent.mkdir(parents=True, exist_ok=True)
    data = None
    try:
        if SYSTEM == "darwin":
            data = _mac_icon(safe, out)
        elif SYSTEM == "linux":
            data = _linux_icon(safe)
        else:
            data = _windows_icon(safe, out)
    except Exception:
        data = None
    out.write_bytes(data or b"")       # empty file = "looked, none found"
    return data


def _mac_icon(app: str, out: Path) -> bytes | None:
    import plistlib
    for base in (Path("/Applications"), HOME / "Applications", Path("/System/Applications"),
                 Path("/Applications/Utilities")):
        bundle = base / f"{app}.app"
        if not bundle.exists():
            continue
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
        name = info.get("CFBundleIconFile") or ""
        icns = bundle / "Contents" / "Resources" / (name if name.endswith(".icns") else name + ".icns")
        if name and icns.exists() and run(["sips", "-s", "format", "png", "-Z", "128", str(icns),
                                           "--out", str(out)])[0] == 0:
            return out.read_bytes()
    return None


def _linux_icon(app: str) -> bytes | None:
    """Icon from the app's .desktop entry (deb, snap and Flatpak exports), else by name in the icon folders."""
    key = app.lower().replace(" ", "-")
    names = [key, key.replace("-", ""), _LINUX_ICON_ALIASES.get(key, key)]
    for d in _linux_desktop_dirs():
        try:
            entries = sorted(d.glob("*.desktop"))
        except OSError:
            continue
        for f in entries:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            name = re.search(r"^Name=(.+)$", text, re.M)
            icon = re.search(r"^Icon=(.+)$", text, re.M)
            if not (name and icon):
                continue
            stem = f.stem.lower()
            if name.group(1).strip().lower() == app.lower() or stem in names or stem.split("_")[0] in names \
                    or stem.split(".")[-1] in names:
                names.insert(0, icon.group(1).strip())
    seen: set[str] = set()
    for n in [x for x in names if not (x in seen or seen.add(x))]:
        if n.startswith("/"):
            data = _linux_icon_file(Path(n))
            if data:
                return data
            continue
        for root in _linux_icon_roots():
            for size in ("128x128", "256x256", "96x96", "64x64", "48x48", "scalable", ""):
                base = root / size / "apps" if size else root
                for ext in (".png", ".svg"):
                    data = _linux_icon_file(base / f"{n}{ext}")
                    if data:
                        return data
    return None


def _windows_exe_for(app: str) -> str | None:
    """The executable behind an app name: a running process, else Windows' App Paths registry, else PATH."""
    try:
        import psutil
        for p in psutil.process_iter(["exe"]):
            if p.info["exe"] and app_name(p.info["exe"]) == app:
                return p.info["exe"]
    except Exception:
        pass
    names = [k for k, v in _WIN_NAMES.items() if v == app] + [app]
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for name in names:
                try:
                    exe = winreg.QueryValue(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}.exe")
                except OSError:
                    continue
                exe = exe.strip().strip('"')
                if exe and Path(exe).exists():
                    return exe
    except Exception:
        pass
    for name in names:
        exe = shutil.which(name) or shutil.which(name + ".exe")
        if exe:
            return exe
    return None


def _windows_icon(app: str, out: Path) -> bytes | None:
    exe = _windows_exe_for(app)
    if not exe:
        return None
    q = lambda v: str(v).replace("'", "''")                 # inside a PowerShell single-quoted string
    # PrivateExtractIcons returns the executable's icon at the size asked for (ExtractAssociatedIcon is 32px only).
    ps = ("Add-Type -AssemblyName System.Drawing; "
          "Add-Type -Namespace Amb -Name Ico -MemberDefinition '[DllImport(\"user32.dll\", CharSet=CharSet.Unicode)] "
          "public static extern uint PrivateExtractIcons(string f, int i, int cx, int cy, IntPtr[] h, uint[] id, uint n, uint fl);'; "
          "$h = New-Object IntPtr[] 1; $id = New-Object uint32[] 1; "
          f"$n = [Amb.Ico]::PrivateExtractIcons('{q(exe)}', 0, 128, 128, $h, $id, 1, 0); "
          "if ($n -ge 1 -and $n -ne [uint32]::MaxValue -and $h[0] -ne [IntPtr]::Zero) { $b = [System.Drawing.Icon]::FromHandle($h[0]).ToBitmap() } "
          f"else {{ $b = [System.Drawing.Icon]::ExtractAssociatedIcon('{q(exe)}').ToBitmap() }}; "
          f"$b.Save('{q(out)}', [System.Drawing.Imaging.ImageFormat]::Png)")
    if run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 20)[0] == 0 and out.exists():
        return out.read_bytes()
    return None


# ---------- website icons, from the browsers already on this device ----------
def _browser_icon_stores() -> list[tuple[str, Path]]:
    """(family, path) of favicon databases kept by installed browsers. Read-only, never fetched."""
    out: list[tuple[str, Path]] = []
    if SYSTEM == "darwin":
        base = HOME / "Library" / "Application Support"
        ff = [base / "zen" / "Profiles", base / "Firefox" / "Profiles", base / "LibreWolf" / "Profiles"]
        cr = [base / "Google" / "Chrome", base / "Arc" / "User Data", base / "BraveSoftware" / "Brave-Browser",
              base / "Microsoft Edge", base / "Chromium"]
    elif SYSTEM == "windows":
        roam = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
        ff = [roam / "zen" / "Profiles", roam / "Mozilla" / "Firefox" / "Profiles"]
        cr = [local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
              local / "BraveSoftware" / "Brave-Browser" / "User Data"]
    else:
        ff = [HOME / ".zen", HOME / ".mozilla" / "firefox", HOME / "snap" / "firefox" / "common" / ".mozilla" / "firefox"]
        cr = [HOME / ".config" / "google-chrome", HOME / ".config" / "chromium", HOME / ".config" / "microsoft-edge",
              HOME / ".config" / "BraveSoftware" / "Brave-Browser"]
    for root in ff:
        if root.exists():
            out += [("firefox", f) for f in root.glob("*/favicons.sqlite")]
    for root in cr:
        if root.exists():
            out += [("chrome", f) for f in list(root.glob("*/Favicons"))[:6]]
    return out


def site_icon(host: str) -> tuple[bytes, str] | None:
    """The icon a browser on this device already has for `host`: (bytes, content type). Cached."""
    host = re.sub(r"[^a-z0-9.-]", "", (host or "").lower())
    if not host or "." not in host:
        return None
    cache = cache_dir() / "icons" / f"site-{host}.bin"
    if cache.exists() and (cache.stat().st_size > 0 or cache.stat().st_mtime > __import__("time").time() - 3600):
        data = cache.read_bytes()
        return (data, _image_type(data)) if data else None
    cache.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    best: bytes | None = None
    parts = host.split(".")
    hosts = [host] + ([".".join(parts[-2:])] if len(parts) > 2 else []) + (["www." + host] if not host.startswith("www.") else [])
    for family, path in _browser_icon_stores():
        try:
            db = sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=0.3)
            for h in hosts:
                like = f"https://{h}/%"
                if family == "firefox":
                    row = db.execute(
                        "SELECT i.data FROM moz_icons i JOIN moz_icons_to_pages ip ON ip.icon_id = i.id "
                        "JOIN moz_pages_w_icons p ON p.id = ip.page_id WHERE p.page_url LIKE ? AND i.data IS NOT NULL "
                        "ORDER BY i.width DESC LIMIT 1", (like,)).fetchone()
                else:
                    row = db.execute(
                        "SELECT b.image_data FROM favicon_bitmaps b JOIN icon_mapping m ON m.icon_id = b.icon_id "
                        "WHERE m.page_url LIKE ? AND b.image_data IS NOT NULL ORDER BY b.width DESC LIMIT 1",
                        (like,)).fetchone()
                if row and row[0] and len(row[0]) < 400_000:
                    best = bytes(row[0])
                    break
            db.close()
        except Exception:
            continue
        if best:
            break
    cache.write_bytes(best or b"")
    return (best, _image_type(best)) if best else None


def _image_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/svg+xml" if b"<svg" in data[:2000] else "application/octet-stream"


# ---------- where apps keep their data (local-storage fallback) ----------
def app_data_roots(app: str, exe_path: str = "") -> list[Path]:
    """Folders where `app` is likely to keep local databases."""
    roots: list[Path] = []
    low = app.lower().replace(" ", "")
    if SYSTEM == "darwin":
        lib = HOME / "Library"
        roots += [lib / "Application Support" / app]
        for parent in (lib / "Group Containers", lib / "Containers"):
            if parent.exists():
                try:
                    roots += [p for p in parent.iterdir() if low in p.name.lower().replace(" ", "")]
                except PermissionError:
                    pass
    elif SYSTEM == "windows":
        for env in ("APPDATA", "LOCALAPPDATA"):
            base = Path(os.environ.get(env, ""))
            if base.exists():
                roots += [base / app]
                pk = base / "Packages"
                if pk.exists():
                    roots += [p / "LocalState" for p in pk.iterdir() if low in p.name.lower()]
    else:
        roots += [HOME / ".config" / app, HOME / ".config" / app.lower(), HOME / ".local/share" / app.lower(),
                  HOME / "snap" / app.lower()]
        flatpak = HOME / ".var" / "app"                   # one folder per Flatpak app id: only this app's
        if flatpak.is_dir():
            try:
                roots += [p for p in flatpak.iterdir() if low in p.name.lower().replace("-", "").replace("_", "")]
            except OSError:
                pass
    return [r for r in roots if r.exists()]


# ---------- process lifetime (the app shell stops the engine; the engine notices the shell dying) ----------
def pid_alive(pid: int) -> bool:
    if SYSTEM == "windows":
        # Not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT, so that call would interrupt the process.
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def interrupt_self() -> None:
    """Shut down the way Ctrl-C would, so mitmproxy stops capture cleanly."""
    import signal
    if SYSTEM == "windows":
        # os.kill(self, SIGINT) is TerminateProcess on Windows; raising the signal runs mitmproxy's handler.
        signal.raise_signal(signal.SIGINT)
    else:
        os.kill(os.getpid(), signal.SIGINT)


def wait_stdin_closed() -> bool:
    """Block until the other end of our stdin pipe is closed. False = stdin isn't something we can watch."""
    if SYSTEM != "windows":
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            return False
        return True
    # Windows: never sit in a blocking read on fd 0. The C runtime holds that descriptor's lock for the whole
    # read, and anything else that touches stdin (isatty(), starting a subprocess) then hangs behind it.
    # Peeking at the pipe doesn't block and fails with ERROR_BROKEN_PIPE once the writer has gone.
    try:
        import ctypes
        import msvcrt
        import time
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD,
                                      wintypes.LPDWORD, wintypes.LPDWORD]
        k32.PeekNamedPipe.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(0)
        while True:
            if not k32.PeekNamedPipe(handle, None, 0, None, None, None):
                return ctypes.get_last_error() == 109           # ERROR_BROKEN_PIPE; anything else: not a pipe
            time.sleep(0.25)
    except Exception:
        return False


def stdio_encoding() -> str | None:
    """Encoding to force on the engine's stdout/stderr (None = leave alone). Windows consoles and
    redirected files default to a legacy code page that can't print “ ” … and would raise mid-log."""
    return "utf-8" if SYSTEM == "windows" else None


def bare_name(path: str) -> str:
    """Name for a process that is not a macOS app bundle: what ps calls it ("claude", "curl").
    Windows has no bundles, so every executable comes here: "…\\msedge.exe" is "Microsoft Edge"."""
    if SYSTEM == "windows":
        return app_name(path) if path else ""
    return os.path.basename(path)


def split_cmd(s: str) -> list[str]:
    """Split a command line the way the OS's own shell would. POSIX rules eat the backslashes in
    C:\\Tools\\agent.exe, so Windows keeps them and only strips the quotes around an argument."""
    import shlex
    if SYSTEM != "windows":
        return shlex.split(s)
    return [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a for a in shlex.split(s, posix=False)]


# Where agent CLIs usually live. An app opened from Finder or a desktop launcher gets the system's bare
# PATH (/usr/bin:/bin:/usr/sbin:/sbin on macOS), which has none of these, so `claude` in ~/.local/bin
# would look "not installed".
USER_BIN_DIRS = ("~/.local/bin", "~/.claude/local", "~/.bun/bin", "~/.npm-global/bin", "~/.volta/bin",
                 "~/.cargo/bin", "~/.deno/bin", "~/go/bin", "~/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
                 "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin", "/snap/bin")
_PATH_MARK = "__AMBIENT_PATH__"


def login_shell_path(timeout: float = 5.0) -> list[str]:
    """The PATH the user's own shell builds (its profile and rc files: where fnm, nvm, asdf and installers
    put things). Empty if the shell can't be asked. The shell's output is never run, only read."""
    shell = os.environ.get("SHELL") or ("/bin/zsh" if SYSTEM == "darwin" else "/bin/bash")
    if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
        return []
    try:
        p = subprocess.run([shell, "-ilc", f'printf "\\n{_PATH_MARK}%s\\n" "$PATH"'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout, errors="replace",
                           env={**os.environ, "TERM": "dumb"}, start_new_session=True)
    except (OSError, subprocess.SubprocessError):
        return []
    for line in reversed(p.stdout.splitlines()):
        if line.startswith(_PATH_MARK):
            return [d for d in line[len(_PATH_MARK):].split(os.pathsep) if d]
    return []


def adopt_user_path() -> str:
    """Add the user's own program folders to PATH, so agents (and the programs they start, like node) are
    found when the app was opened from the desktop rather than a terminal. Keeps what is there, in order,
    and appends; nothing is removed. Windows apps already get the user's PATH. Returns the new PATH."""
    cur = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    if SYSTEM == "windows":
        return os.pathsep.join(cur)
    home = str(HOME)
    bare = not any(d.startswith(home) or d.startswith(("/opt/homebrew", "/usr/local")) for d in cur)
    extra = login_shell_path() if (bare or getattr(sys, "frozen", False)) else []
    extra += [os.path.expanduser(d) for d in USER_BIN_DIRS]
    seen = set(cur)
    for d in extra:
        if d not in seen and os.path.isabs(d) and os.path.isdir(d):
            cur.append(d)
            seen.add(d)
    os.environ["PATH"] = os.pathsep.join(cur)
    return os.environ["PATH"]


def pinned_hosts() -> list[str]:
    """Hosts this OS's own services pin certificates for: passed through up front, so they never fail once first."""
    if SYSTEM == "windows":
        # Push notifications; telemetry and settings sync (*.data.microsoft.com); Windows Update, Store and
        # Delivery Optimization (*.mp.microsoft.com); Defender cloud lookups; SmartScreen.
        return ["wns.windows.com", "data.microsoft.com", "windowsupdate.com", "update.microsoft.com",
                "mp.microsoft.com", "wdcp.microsoft.com", "wdcpalt.microsoft.com", "smartscreen.microsoft.com"]
    return []


def upstream_ca_bundle() -> str | None:
    """A CA file for checking the servers the engine connects to on apps' behalf, or None for mitmproxy's
    default (the Mozilla list only). On Windows that default breaks whatever Windows trusts and Mozilla does
    not: Microsoft's own services (some chain to Microsoft-only roots or leave out an intermediate) and
    company-internal sites behind a private CA. So: the Mozilla list plus this PC's Root and Intermediate
    stores, minus Ambient's own certificate."""
    if SYSTEM != "windows":
        return None
    try:
        import ssl
        import certifi
        ours = ssl.PEM_cert_to_DER_cert(ca_path().read_text()) if ca_path().exists() else None
        pems = [Path(certifi.where()).read_text(encoding="utf-8")]
        seen = set()
        for store in ("ROOT", "CA"):
            for der, encoding, _trust in ssl.enum_certificates(store):
                if encoding == "x509_asn" and der != ours and der not in seen:
                    seen.add(der)
                    pems.append(ssl.DER_cert_to_PEM_cert(der))
        out = cache_dir() / "upstream-roots.pem"
        out.write_text("\n".join(pems), encoding="utf-8")
        return str(out)
    except Exception:
        return None


def certs_need_crl() -> bool:
    """Should every certificate Ambient mints carry a CRL address? Windows' own TLS stack (Schannel: curl.exe,
    some .NET and WinHTTP apps) refuses a certificate it has no way to check for revocation, and mitmproxy only
    adds a CRL address when the real site's certificate has one (github.com's, for one, does not)."""
    return SYSTEM == "windows"


# ---------- Windows helpers ----------
WIN_REDIRECTOR = "windows-redirector.exe"
# Executables under these folders are Windows itself or its services, not apps a person installed.
_WIN_SYSTEM_DIRS = ("\\windows\\", "\\windows defender\\", "\\programdata\\microsoft\\", "\\microsoft\\edgeupdate\\",
                    "\\common files\\microsoft shared\\")


def _windows_redirector_running() -> bool:
    """mitmproxy starts windows-redirector.exe through UAC ("runas"); Windows keeps this process as its
    parent. It holds the WinDivert driver handle, so capture is on exactly while it is alive."""
    try:
        import psutil
        return any(k.name().lower() == WIN_REDIRECTOR for k in psutil.Process().children())
    except Exception:
        return False


def _windows_capture_status() -> dict:
    try:
        import mitmproxy_windows
        seen = mitmproxy_windows.executable_path().exists()
    except Exception:
        seen = False
    on = _windows_redirector_running()
    fix = ""
    if not seen:
        fix = "This copy of Ambient is missing its Windows capture helper. Reinstall Ambient."
    elif not on:
        fix = ("Ambient isn't seeing app traffic. Windows asks “Do you want to allow this app to make changes?” "
               "when Ambient starts watching: choose Yes. To be asked again, pick Pause watching and then "
               "Resume watching from the Ambient tray icon. If you did choose Yes, antivirus may be "
               "blocking the WinDivert driver.")
    return {"ok": on, "seen": seen, "fix": fix}


def _windows_ca_in_root(ca: Path) -> bool:
    """Is exactly this certificate in a Trusted Root store (the user's or the machine's)?"""
    try:
        import ssl
        der = ssl.PEM_cert_to_DER_cert(ca.read_text())
        return any(cert == der for cert, _enc, _trust in ssl.enum_certificates("ROOT"))
    except Exception:
        return False


def _windows_trust_ca(ca: Path) -> dict:
    """Current-user Trusted Root store: no administrator needed. Windows shows its own “Security Warning”
    dialog naming the certificate and adds it only if the person answers Yes."""
    if _windows_ca_in_root(ca):
        return {"ok": True, "note": "Already trusted."}
    run(["certutil", "-user", "-addstore", "Root", str(ca)], 120)
    if _windows_ca_in_root(ca):
        return {"ok": True, "note": "Trusted for your Windows account."}
    return {"ok": False, "note": "Windows didn't add the certificate. Try again and answer Yes when Windows asks."}


# ---------- Linux helpers ----------
# mitmproxy's Linux capture is a small helper (mitmproxy-linux-redirector) that makes a tun device and attaches an
# eBPF program to the root cgroup; the program binds every new socket of a matching process to that device.
# Three facts shape everything below (each checked on Ubuntu 24.04, kernel 6.8, mitmproxy_rs 0.12; see docs/linux.md):
#   1. Patterns are a process id, or the exact name of the *thread* that creates the socket (15 characters), and
#      there is room for 19 of them. "firefox" matches nothing Firefox sends: its sockets are made by "Socket Thread".
#   2. A socket bound to the tun device cannot be reached by anyone once it listens. So when everything is captured,
#      any server started meanwhile (a dev server, a container port, sshd after an upgrade) is dead until restarted.
#   3. mitmproxy starts the helper with `sudo`, which a desktop app has no terminal for.
LINUX_REDIRECTOR = Path("/usr/lib/ambient/ambient-redirector")   # root-owned copy with file capabilities (the package installs it)
LINUX_SETUP = Path("/usr/lib/ambient/ambient-setup")             # run as root through pkexec: certificate + capture group
LINUX_GROUP = "ambient-capture"                                  # who may run the capture helper
LINUX_CAPTURE_FILE = "capture"                                   # in ~/.config/ambient/: "all", or extra names to capture

# Default on Linux: capture the threads that do the networking of browsers and of everything built on them, plus a
# few command-line tools. Nothing here ever listens, so fact 2 can't bite.
_LINUX_APPS = [
    "Socket Thread",      # Gecko: Firefox, Zen, LibreWolf, Thunderbird
    "Chrome_ChildIOT",    # Chromium's network service: Chrome, Chromium, Brave, Edge and every Electron app (Slack, Discord, VS Code…)
    "curl", "wget", "git-remote-http", "gh",
    "claude", "codex",    # agent CLIs (only decrypted when started with Ambient's certificate, like everywhere else)
]
# "all" on Linux: everything except these. Network plumbing breaks under capture (systemd-resolved cannot pick its
# interface once its sockets are bound to the tun device, so every DNS lookup on the machine fails); remote access
# and container ports must never depend on Ambient; call apps as on the other systems.
_LINUX_EXCLUDES = ["tailscaled", "openvpn", "openconnect", "nordvpnd", "mullvad-daemon",
                   "systemd-resolve", "systemd-network", "NetworkManager", "avahi-daemon",
                   "sshd", "dockerd", "docker-proxy", "containerd",
                   "zoom", "teams", "webex"]
_LINUX_MAX_PATTERNS = 18      # the helper holds 20 slots: ours, mitmproxy's own "not me", and the end marker

_LINUX_MORE_NAMES = {"firefox-esr": "Firefox", "chromium-browser": "Chromium", "librewolf": "LibreWolf",
                     "vivaldi-bin": "Vivaldi", "vivaldi": "Vivaldi", "opera": "Opera", "msedge": "Microsoft Edge",
                     "brave-browser": "Brave", "thunderbird": "Thunderbird", "telegram-desktop": "Telegram",
                     "signal-desktop": "Signal", "floorp": "Floorp"}
_LINUX_ICON_ALIASES = {"visual-studio-code": "vscode", "brave": "brave-browser", "telegram": "telegram-desktop",
                       "signal": "signal-desktop", "microsoft-edge": "microsoft-edge"}
_linux_state: dict = {"wanted": 0.0, "sudo": (0.0, False)}


def _linux_app_name(path: str, base: str) -> str:
    low = base.lower()
    if low == "chrome" and "chromium" in path.lower():      # /snap/chromium/…/usr/lib/chromium-browser/chrome
        return "Chromium"
    return _LINUX_NAMES.get(low) or _LINUX_MORE_NAMES.get(low, base)


def _linux_capture_choice() -> str:
    """AMBIENT_CAPTURE, else the first line of ~/.config/ambient/capture, else ""."""
    v = os.environ.get("AMBIENT_CAPTURE", "").strip()
    if v:
        return v
    f = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "ambient" / LINUX_CAPTURE_FILE
    try:
        return next((ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")), "")
    except OSError:
        return ""


def _linux_capture_spec() -> str | None:
    import time
    state, fix = _linux_state.get("start") or _linux_capture_possible()
    _linux_state["start"] = (state, fix)
    if fix:
        return None                 # mitmproxy exits when its helper can't start; come up without capture instead
    _linux_state["wanted"] = time.time()
    choice = _linux_capture_choice()
    if choice.lower() == "all":
        return default_excludes()
    extra = [x.strip() for x in choice.split(",") if x.strip() and x.strip().lower() != "apps"]
    names = list(dict.fromkeys(_LINUX_APPS + extra))[:_LINUX_MAX_PATTERNS]
    return ",".join(names)


def _linux_capture_possible() -> tuple[str, str]:
    """(helper state, what's missing in plain words or "" when capture can start)."""
    import time
    release = os.uname().release
    try:
        major, minor = (int(re.match(r"\d+", x).group()) for x in release.split(".")[:2])
    except Exception:
        major, minor = 0, 0
    if (major, minor) < (6, 8):
        return "old-kernel", (f"Seeing app traffic on Linux needs kernel 6.8 or newer (Ubuntu 24.04 and later). "
                              f"This is {release}.")
    state = _linux_helper_state()
    if state in ("ready", "sg"):
        return state, ""
    at, can = _linux_state["sudo"]
    if time.time() - at > 60:       # without the helper, mitmproxy uses sudo: root, a cached/passwordless sudo, or a terminal to ask in
        can = os.geteuid() == 0 or run(["sudo", "-n", "true"], 5)[0] == 0
        try:
            can = can or (sys.stdin is not None and sys.stdin.isatty() and shutil.which("sudo") is not None)
        except Exception:
            pass
        _linux_state["sudo"] = (time.time(), can)
    if can:
        return state, ""
    if state == "no-caps":
        return state, "Ambient's capture helper has lost its permissions. Reinstall the Ambient package."
    if state == "no-group":
        how = "Run: " if _linux_ca_in_system_store(ca_path()) else "Use Trust above (it asks for an administrator once), or run: "
        return state, f"Your account isn't allowed to see app traffic yet. {how}sudo usermod -aG {LINUX_GROUP} $USER"
    return state, ("Seeing app traffic needs root on Linux. Install the Ambient package (it sets up a capture helper "
                   "that needs no password), or start the engine from a terminal where sudo works.")


def _linux_helper_state() -> str:
    """'ready': we may run the installed capture helper. 'sg': we may through `sg` (added to the group since this
    login). 'no-group': installed, but not for this account. 'no-caps': installed without its capabilities.
    'missing': not installed (running from source)."""
    h = LINUX_REDIRECTOR
    if not h.is_file():
        return "missing"
    try:
        os.getxattr(h, "security.capability")
    except OSError:
        return "no-caps"
    if os.access(h, os.X_OK):
        return "ready"
    try:
        import grp
        import pwd
        user = pwd.getpwuid(os.getuid()).pw_name
        if user in grp.getgrgid(h.stat().st_gid).gr_mem and shutil.which("sg"):
            return "sg"
    except Exception:
        pass
    return "no-group"


def _linux_prepare_capture() -> None:
    """Make mitmproxy start the installed, capability-carrying helper instead of `sudo <its bundled helper>`.
    mitmproxy_rs runs exactly `sudo echo -n` and `sudo --non-interactive --preserve-env <helper> <dir>`; for the
    seconds it takes to start, a stand-in `sudo` that understands those two lines goes first on our PATH."""
    import threading
    if getattr(sys, "frozen", False):
        # PyInstaller points LD_LIBRARY_PATH at its unpacked libraries. Programs we start (agents, certutil, pkexec,
        # xdg-open) must load the system's own, so give them the value from before (the loader keeps ours for us).
        orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        if orig:
            os.environ["LD_LIBRARY_PATH"] = orig
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    state, fix = _linux_state["start"] = _linux_capture_possible()

    def browsers():                 # once trusted, keep new browsers and new profiles trusting it too (no root involved)
        ca = ca_path()
        if ca.exists() and _linux_ca_in_system_store(ca):
            _linux_trust_browsers(ca)
    threading.Thread(target=browsers, daemon=True).start()
    if state not in ("ready", "sg"):
        return                      # from source in a terminal: mitmproxy's own `sudo` asks for the password there
    try:
        import mitmproxy_linux
        mitmproxy_linux.executable_path = lambda: LINUX_REDIRECTOR
    except Exception:
        return
    real = shutil.which("sudo") or "/usr/bin/sudo"
    start = 'exec "$@"' if state == "ready" else f"exec sg {LINUX_GROUP} -c \"exec '$1' '$2'\""
    shim = cache_dir() / "capture-shim"
    shim.mkdir(parents=True, exist_ok=True)
    script = shim / "sudo"
    script.write_text(f"""#!/bin/sh
# Written by Ambient (engine/osal.py) each time it starts. mitmproxy starts its Linux capture helper with
#   sudo echo -n      and then      sudo --non-interactive --preserve-env <helper> <socket folder>
# Ambient's installed helper carries file capabilities instead of needing root, so this stand-in runs it
# directly. Anything else is handed to the real sudo untouched.
if [ "$*" = "echo -n" ]; then exit 0; fi
if [ "$1" = "--non-interactive" ] && [ "$2" = "--preserve-env" ] && [ "$3" = "{LINUX_REDIRECTOR}" ]; then
  shift 2
  {start}
fi
exec {real} "$@"
""")
    script.chmod(0o700)
    # mitmproxy never tells the helper to stop: started by sudo from a terminal it gets Ctrl-C with everyone else,
    # but started by us it would stay behind, still attached, and swallow the next connection of a captured app.
    import atexit
    _linux_stop_helpers(orphans=True)        # one left by an engine that was killed outright
    atexit.register(_linux_stop_helpers)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{shim}{os.pathsep}{old}"

    def restore():                  # agents Ambient starts later must find the real sudo first
        import time
        for _ in range(90):
            time.sleep(0.5)
            if _linux_redirector_running():
                break
        if os.environ.get("PATH", "").startswith(f"{shim}{os.pathsep}"):
            os.environ["PATH"] = os.environ["PATH"][len(str(shim)) + 1:]

    threading.Thread(target=restore, daemon=True).start()


def _linux_stop_helpers(orphans: bool = False) -> None:
    """Ask our capture helper to stop (it removes its tun device and eBPF program as it goes).
    orphans=True: instead, any helper of this user that outlived its engine."""
    try:
        import psutil
        me = psutil.Process()
        if orphans:
            procs = [k for k in psutil.process_iter(["name", "ppid", "uids"])
                     if (k.info["name"] or "").startswith(LINUX_REDIRECTOR.name[:15]) and k.info["ppid"] == 1
                     and k.info["uids"] and k.info["uids"].real == os.getuid()]
        else:
            procs = [k for k in me.children(recursive=True) if k.name().startswith(LINUX_REDIRECTOR.name[:15])]
        for k in procs:
            k.terminate()
        import time
        for _ in range(30):         # wait for the tun device to be gone, without reaping (mitmproxy waits on its child itself)
            if all(not k.is_running() or k.status() == psutil.STATUS_ZOMBIE for k in procs):
                break
            time.sleep(0.1)
    except Exception:
        pass


def _linux_redirector_running() -> bool:
    """The helper is our child (or grandchild, behind sudo or sg) exactly while capture is on."""
    try:
        import psutil
        for k in psutil.Process().children(recursive=True):
            try:
                if "redirector" in k.name() or any("redirector" in a for a in k.cmdline()[:1]):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _linux_capture_status() -> dict:
    import time
    if _linux_redirector_running():
        return {"ok": True, "seen": True, "fix": ""}
    state, fix = _linux_capture_possible()
    start = _linux_state.get("start")           # how things stood when this engine started (None: mode given by hand)
    if not fix and start and start[1]:
        fix = ("Almost there: choose Pause watching and then Resume watching from the Ambient tray icon, "
               "so Ambient starts seeing app traffic with its new permission.")
    elif not fix and _linux_state["wanted"] and time.time() - _linux_state["wanted"] > 20:
        fix = "Capture didn't start. The reason is in Ambient's engine log (~/.local/state/ambient/engine.log)."
    return {"ok": not fix, "seen": state not in ("missing", "old-kernel"), "fix": fix}


def _linux_ca_in_system_store(ca: Path) -> bool:
    """Is exactly this certificate in the bundle that OpenSSL, GnuTLS, curl, git and Go read?"""
    try:
        body = "".join(ln.strip() for ln in ca.read_text().splitlines() if ln.strip() and not ln.startswith("-----"))
    except OSError:
        return False
    if not body:
        return False
    for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
                   "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        try:
            if body in re.sub(r"\s+", "", Path(bundle).read_text(errors="replace")):
                return True
        except OSError:
            continue
    return False


def _linux_nss_databases() -> list[tuple[str, Path, bool]]:
    """(who reads it, folder, create if missing) for every browser certificate database of this user.
    Chrome-family browsers read ~/.pki/nssdb when it exists and ~/.local/share/pki/nssdb otherwise; snap and
    Flatpak builds have the same pair inside their own home. Firefox-family browsers have one per profile."""
    out: list[tuple[str, Path, bool]] = []
    homes = [("Chrome-family browsers", HOME, ".local/share", True)]
    if (HOME / "snap" / "chromium").is_dir():
        homes.append(("Chromium (snap)", HOME / "snap" / "chromium" / "current", ".local/share", False))
    for name, appdir in (("Chrome (Flatpak)", "com.google.Chrome"), ("Chromium (Flatpak)", "org.chromium.Chromium"),
                         ("Brave (Flatpak)", "com.brave.Browser"), ("Edge (Flatpak)", "com.microsoft.Edge")):
        if (HOME / ".var" / "app" / appdir).is_dir():
            homes.append((name, HOME / ".var" / "app" / appdir, "data", True))
    for name, root, data, create in homes:
        pair = [root / ".pki" / "nssdb", root / data / "pki" / "nssdb"]
        have = [d for d in pair if (d / "cert9.db").is_file()]
        out += [(name, d, False) for d in have] or ([(name, pair[0], True)] if create else [])
    for name, base in (("Firefox", HOME / ".mozilla" / "firefox"),
                       ("Firefox (snap)", HOME / "snap" / "firefox" / "common" / ".mozilla" / "firefox"),
                       ("Firefox (Flatpak)", HOME / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox"),
                       ("LibreWolf", HOME / ".librewolf"), ("Zen", HOME / ".zen"),
                       ("Thunderbird", HOME / ".thunderbird")):
        try:
            out += [(name, p, False) for p in base.iterdir() if (p / "cert9.db").is_file()]
        except OSError:
            continue
    return out


def _linux_trust_browsers(ca: Path) -> tuple[list[str], str]:
    """Add the certificate to every browser database of this user (no root needed). Returns (who now trusts it, note)."""
    if not shutil.which("certutil"):
        return [], "Install libnss3-tools so Ambient can add its certificate to your browsers too."
    done: list[str] = []
    for name, folder, create in _linux_nss_databases():
        if not (folder / "cert9.db").is_file():
            if not create:
                continue
            try:
                folder.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
            run(["certutil", "-d", f"sql:{folder}", "-N", "--empty-password"], 20)
        if run(["certutil", "-d", f"sql:{folder}", "-A", "-t", "C,,", "-n", "Ambient", "-i", str(ca)], 20)[0] == 0 \
                and name not in done:
            done.append(name)
    return done, ""


def _linux_trust_ca(ca: Path) -> dict:
    """System store: one administrator prompt (polkit), through Ambient's installed setup helper when there is one;
    it also lets this account start capture and tells Firefox to read the certificate. Then, without root, every
    browser certificate database of this user."""
    notes = []
    if not _linux_ca_in_system_store(ca):
        if LINUX_SETUP.is_file():
            cmds = [["sudo", "-n", str(LINUX_SETUP), "trust"], ["pkexec", str(LINUX_SETUP), "trust"]]
        else:
            script = f"cp '{ca}' /usr/local/share/ca-certificates/ambient.crt && update-ca-certificates"
            if Path("/etc/pki/ca-trust/source/anchors").is_dir():       # Fedora / RHEL family
                script = f"cp '{ca}' /etc/pki/ca-trust/source/anchors/ambient.crt && update-ca-trust"
            cmds = [["sudo", "-n", "sh", "-c", script], ["pkexec", "sh", "-c", script]]
        for cmd in cmds:
            if not shutil.which(cmd[0]):
                continue
            code = run(cmd, 300)[0]
            if code == 0:
                break
            if cmd[0] == "pkexec" and code == 126:      # the person closed the prompt: that is a no to all of it
                return {"ok": False, "note": "Nothing was changed: the administrator prompt was closed."}
    system = _linux_ca_in_system_store(ca)
    notes.append("Trusted on this computer." if system else
                 "The system list wasn't changed (the password prompt was cancelled or isn't available).")
    browsers, hint = _linux_trust_browsers(ca)
    if browsers:
        notes.append("Added for " + ", ".join(browsers) + ". Restart a browser that is open.")
    if hint:
        notes.append(hint)
    if not system and not browsers:
        notes.append(trust_ca_hint())
    return {"ok": system or bool(browsers), "note": " ".join(notes)}


def _linux_desktop_dirs() -> list[Path]:
    return [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            HOME / ".local/share/applications", Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications"), HOME / ".local/share/flatpak/exports/share/applications"]


def _linux_icon_roots() -> list[Path]:
    return [Path("/usr/share/icons/hicolor"), HOME / ".local/share/icons/hicolor", Path("/usr/share/pixmaps"),
            Path("/var/lib/snapd/desktop/icons"), Path("/var/lib/flatpak/exports/share/icons/hicolor"),
            HOME / ".local/share/flatpak/exports/share/icons/hicolor"]


def _linux_icon_file(p: Path) -> bytes | None:
    try:
        if not p.is_file():
            return None
        if p.suffix == ".png":
            return p.read_bytes()
        if p.suffix == ".svg" and shutil.which("rsvg-convert"):
            r = subprocess.run(["rsvg-convert", "-w", "128", "-h", "128", str(p)], capture_output=True, timeout=10)
            return r.stdout if r.returncode == 0 and r.stdout[:4] == b"\x89PNG" else None
    except Exception:
        pass
    return None
