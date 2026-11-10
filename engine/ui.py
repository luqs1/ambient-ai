"""Local UI + JSON API + MCP endpoint on 127.0.0.1. The same JSON is what an agent would read.

Only this machine's own pages and tools may talk to it. Every request must be addressed to the server by a
local name (Host is 127.0.0.1, localhost or [::1] on our port), which stops a web page that re-points its own
domain at 127.0.0.1 (DNS rebinding) from reading anything. A request that says where it comes from (Origin,
Sec-Fetch-Site) must come from the UI's own pages. Writes (POST) also need X-Ambient or a JSON content type,
which a cross-site page can't send without a CORS preflight, and we never answer one.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import osal, triggers_api
from engine.mcp_app import engine_handle as mcp_handle      # trigger tools + MCP App view, then the moment tools

UI_DIR = Path(os.environ.get("AMBIENT_UI_DIR") or Path(__file__).resolve().parent.parent / "ui")
CA = osal.ca_path()
# The names this server answers to. The desktop shell's windows load http://127.0.0.1:<port> directly (its
# bundled loading page at tauri://localhost never calls the API), so no other origin is needed.
LOCAL_NAMES = ("127.0.0.1", "localhost", "[::1]")
ASSET_TYPES = {".woff2": "font/woff2", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
               ".js": "text/javascript"}


def watch_list(moments, events) -> list[dict]:
    """Apps for the header: the ones Ambient has seen you use, most recent first."""
    from engine.moments import WATCHABLE
    out, seen = [], set()
    cat = events.catalog() if events else []
    for a in cat[:14]:
        kinds = [k["label"] for k in a["kinds"][:3]]
        out.append({"app": a["app"], "what": WATCHABLE.get(a["app"]) or ", ".join(kinds), "on": moments.watching(a["app"]),
                    "via": a["via"], "site": bool(a["via"])})
        seen.add(a["app"])
    for app in sorted(events.muted_apps if events else ()):
        if app not in seen:
            out.append({"app": app, "what": "paused", "on": False, "via": [], "site": False})
    return out


def unreadable_apps(store, events) -> list[dict]:
    """Apps whose traffic we see but can't read, and whether local storage covers them."""
    rows = store.q("""SELECT app, MAX(process_path) path, SUM(decrypted = 0) opq, SUM(decrypted = 1) dec, MAX(ts) last
                      FROM flows WHERE kind IN ('opaque','tls-rejected','http','ws') AND ts > ? AND app != 'unknown'
                      GROUP BY app HAVING opq >= 3 AND dec * 5 < opq ORDER BY last DESC LIMIT 40""",
                   (time.time() - 7 * 86400,))
    local = {t["app"] for t in events.types.values() if (t.get("source") or "network") == "local"}
    return [{"app": r["app"], "n": r["opq"], "last": r["last"], "local": r["app"] in local}
            for r in rows if osal.is_user_app(r["path"] or "")]


# ---------- Apps tab: a stable list of connected apps, and what Ambient can see from one ----------
# Read-only views over the catalog. The catalog's fields may grow; read them with .get() and pass extras through.
KIND_FIELDS = ("sig", "label", "direction", "klass", "n", "n_text", "last_ts", "example", "source", "curated")


def _app_shapes(events) -> dict[str, dict]:
    """Per app: the browsers it was seen in, and whether it is only ever a website (every kind came via a browser)."""
    out: dict[str, dict] = {}
    for t in list(events.types.values()) if events else []:
        app = t.get("app")
        if not app:
            continue
        a = out.setdefault(app, {"via": set(), "native": False, "sources": set()})
        if t.get("via"):
            a["via"].add(t["via"])
        a["native"] = a["native"] or not t.get("via")
        a["sources"].add(t.get("source") or "network")
    return out


def _status(app: str, paused: set, unreadable: dict, local_apps: set) -> str:
    if app in paused:
        return "paused"
    if app in local_apps:
        return "local"
    if app in unreadable:
        return "unreadable"
    return "connected"


def _context(store, events, local):
    try:
        un = {u["app"]: u for u in unreadable_apps(store, events)}
    except Exception:
        un = {}
    shapes = _app_shapes(events)
    local_apps = {a for a, s in shapes.items() if "local" in s["sources"]}
    for l in (local.status() if local else []):
        if l.get("events"):
            local_apps.add(l.get("app"))
    un = {a: u for a, u in un.items() if not u.get("local") or a in local_apps}
    return un, shapes, local_apps, set(events.muted_apps if events else ())


def connected_apps(store, events, local=None) -> list[dict]:
    """Every app Ambient knows, alphabetical, one plain status each. The order never depends on activity."""
    un, shapes, local_apps, paused = _context(store, events, local)
    cat = {a["app"]: a for a in (events.catalog() if events else [])}
    names = set(cat) | paused | set(un) | (local_apps & set(shapes))
    out = []
    for app in names:
        a, s = cat.get(app) or {}, shapes.get(app) or {"via": set(), "native": True}
        out.append({"app": app, "website": bool(s["via"]) and not s["native"], "via": sorted(s["via"]),
                    "status": _status(app, paused, un, local_apps), "kinds": len(a.get("kinds") or []),
                    "last_ts": a.get("last_ts") or 0})
    out.sort(key=lambda x: (x["app"].casefold(), x["app"]))
    return out


def connected_app(store, events, app: str, local=None) -> dict | None:
    """One app: its kinds of activity in a stable order (by name), with counts and last seen."""
    un, shapes, local_apps, paused = _context(store, events, local)
    kinds, quiet = [], 0
    for t in list(events.types.values()) if events else []:
        if t.get("app") != app:
            continue
        if t.get("klass") in ("action", "incoming"):
            kinds.append({k: t.get(k) for k in KIND_FIELDS} | {"via": t.get("via") or ""})
        else:
            quiet += 1
    if not kinds and not quiet and app not in paused and app not in un:
        return None
    s = shapes.get(app) or {"via": set(), "native": True}
    kinds.sort(key=lambda k: ((k["label"] or "").casefold(), k["sig"]))
    return {"app": app, "website": bool(s["via"]) and not s["native"], "via": sorted(s["via"]),
            "status": _status(app, paused, un, local_apps), "kinds": kinds, "quiet": quiet,
            "local": app in local_apps}


def setup_status(mode: str = "") -> dict:
    cap = osal.capture_status()
    return {
        "mode": mode, "os": osal.SYSTEM,
        "ca_trusted": osal.ca_trusted(), "ca_hint": osal.trust_ca_hint(),
        "extension_on": cap["ok"], "extension_seen": cap["seen"], "capture_fix": cap["fix"],
        "claude_protected": osal.protect_agents_installed(), "profile": osal.shell_profile_target(),
        "ca_path": str(CA),
    }


def local_hosts(port: int) -> set[str]:
    """Host header values that mean "this server". The bare name is what a client sends for port 80, and what
    the desktop shell's own readiness probe sends; a browser on another site can produce neither for our port."""
    return {f"{h}:{port}" for h in LOCAL_NAMES} | set(LOCAL_NAMES)


def local_origins(port: int) -> set[str]:
    return {f"http://{h}:{port}" for h in LOCAL_NAMES} | ({f"http://{h}" for h in LOCAL_NAMES} if port == 80 else set())


def refusal(headers, path: str, port: int) -> str | None:
    """Why this request is not from this machine's own UI or tools (None = let it in). For every route."""
    hosts = headers.get_all("Host") or []
    if len(hosts) != 1 or hosts[0].strip().lower() not in local_hosts(port):
        return "this address is not Ambient's"
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower() not in local_origins(port):
        return "requests from other sites are not allowed"
    if path.startswith(("/api/", "/mcp")) and (headers.get("Sec-Fetch-Site") or "").strip().lower() in (
            "cross-site", "same-site"):
        return "requests from other sites are not allowed"
    return None


def serve(store, port: int, moments=None, mode_fn=lambda: "", events=None, local=None, on_trusted=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("cross-origin-resource-policy", "same-origin")
            self.send_header("x-frame-options", "DENY")                       # no other page may frame the UI
            self.send_header("content-security-policy", "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _file(self, name):
            p = UI_DIR / name
            if not p.exists():
                return self._json({"error": "not found"}, 404)
            self._send(200, p.read_bytes(), "text/html; charset=utf-8")

        def _asset(self, rel: str):
            p = (UI_DIR / "assets" / rel).resolve()
            if not str(p).startswith(str((UI_DIR / "assets").resolve())) or not p.is_file():
                return self._json({"error": "not found"}, 404)
            self._send(200, p.read_bytes(), ASSET_TYPES.get(p.suffix, "application/octet-stream"))

        def _foreign(self) -> bool:
            """Refuse (403) anything not addressed to, and not coming from, this server itself."""
            why = refusal(self.headers, urlparse(self.path).path, self.server.server_address[1])
            if why:
                self._json({"error": "forbidden", "why": why}, 403)
            return bool(why)

        def _body(self) -> dict:
            n = int(self.headers.get("content-length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                return {}

        def do_GET(self):
            if self._foreign():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                self._file("index.html")
            elif u.path == "/panel":
                self._file("panel.html")
            elif u.path == "/api/status" and moments:
                self._json(moments.status())
            elif u.path == "/api/notes" and moments:
                self._json(moments.notes_after(int(q.get("after", 0))))
            elif u.path == "/traffic":
                self._file("traffic.html")
            elif u.path.startswith("/assets/"):
                self._asset(u.path[len("/assets/"):])
            elif u.path.startswith("/api/icon/"):
                from urllib.parse import unquote
                name = unquote(u.path.rsplit("/", 1)[1]).removesuffix(".png")
                png = osal.app_icon_png(name)
                site = None
                if not png and events:                     # a website: use the icon a local browser already has
                    host = next((t["host"] for t in events.types.values() if t["app"] == name and t.get("via") and t["host"]), "")
                    site = osal.site_icon(host) if host else None
                if png:
                    self._send(200, png, "image/png")
                elif site:
                    self._send(200, site[0], site[1])
                else:
                    self._json({"error": "no icon"}, 404)
            elif u.path == "/api/triggers" and moments:
                self._json({"triggers": moments.triggers(), "agents": moments.agents()})
            elif u.path == "/api/activity" and events:
                self._json({"apps": events.catalog(include_quiet=q.get("quiet") == "1"),
                            "unreadable": unreadable_apps(store, events),
                            "local": local.status() if local else [],
                            "paused": sorted(events.muted_apps)})
            elif u.path == "/api/connected" and events:
                self._json(connected_apps(store, events, local))
            elif u.path.startswith("/api/connected/") and events:
                from urllib.parse import unquote
                one = connected_app(store, events, unquote(u.path[len("/api/connected/"):]), local)
                self._json(one or {"error": "not found"}, 200 if one else 404)
            elif u.path == "/api/events" and events:
                self._json(events.recent(sig=q.get("sig") or None, limit=min(int(q.get("limit", 30)), 200),
                                         after=int(q.get("after", 0))))
            elif u.path == "/api/checks" and moments:
                self._json(list(moments.checks)[:int(q.get("limit", 40))])
            elif u.path.startswith("/api/record/") and events:
                self._json(events.record_read(u.path.rsplit("/", 1)[1]))
            elif u.path == "/api/flows":
                self._json(store.list_flows(after=int(q.get("after", 0)), app=q.get("app") or None,
                                            only=q.get("only") or None, search=q.get("q") or None,
                                            limit=int(q.get("limit", 500))))
            elif u.path.startswith("/api/flow/"):
                r = store.get(int(u.path.rsplit("/", 1)[1]))
                self._json(r or {"error": "not found"}, 200 if r else 404)
            elif u.path == "/api/apps":
                self._json(store.apps())
            elif u.path == "/api/summary":
                self._json(store.summary())
            elif u.path == "/api/moments" and moments:
                self._json(moments.list(after=int(q.get("after", 0)), limit=int(q.get("limit", 50))))
            elif u.path == "/api/shield":
                day = time.time() - 86400 if q.get("range") != "all" else 0
                self._json(store.shield_stats(day))
            elif u.path == "/api/watch" and moments:
                self._json(watch_list(moments, events))
            elif u.path == "/api/setup":
                self._json(setup_status(mode_fn()))
            elif u.path == "/mcp":
                self._json({"error": "POST JSON-RPC here"}, 405)
            elif (r := triggers_api.get(u.path, q, moments, events)) is not None:     # trigger programs, deciders
                self._json(*r)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self._foreign():
                return
            u = urlparse(self.path)
            if u.path == "/mcp" and moments:
                if "json" not in self.headers.get_content_type():      # the type itself: "text/plain; x=json" is not JSON
                    return self._json({"error": "content-type must be application/json"}, 415)
                msg = self._body()
                if isinstance(msg, list):
                    out = [r for r in (mcp_handle(moments, m) for m in msg) if r]
                else:
                    out = mcp_handle(moments, msg)
                if not out:
                    self.send_response(202)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                return self._json(out)
            if self.headers.get("X-Ambient") != "1":
                return self._json({"error": "missing X-Ambient header"}, 403)
            b = self._body()
            parts = u.path.strip("/").split("/")
            if parts[:2] == ["api", "moments"] and len(parts) == 4 and moments:
                mid, verb = int(parts[2]), parts[3]
                if verb == "action":
                    if b.get("trigger"):
                        return self._json(moments.run_trigger(mid, int(b["trigger"])))
                    return self._json(moments.run_action(mid, b.get("action", "ask"), b.get("question", "")))
                if verb in ("dismiss", "done", "new"):
                    moments.set_status(mid, {"dismiss": "dismissed"}.get(verb, verb))
                    return self._json({"ok": True})
            if u.path == "/api/triggers/suggest" and moments:
                return self._json(moments.suggest(str(b.get("text") or "")))
            if u.path == "/api/triggers/preview" and moments:
                return self._json(moments.preview(b))
            if u.path == "/api/record/start" and events:
                return self._json({"token": events.record_start(float(b.get("seconds") or 90))})
            if parts[:2] == ["api", "record"] and len(parts) == 4 and parts[3] == "stop" and events:
                return self._json(events.record_read(parts[2], stop=True))
            if u.path == "/api/activity/mute" and events:
                events.mute(str(b.get("sig") or ""), bool(b.get("muted")))
                return self._json({"ok": True})
            if parts[:2] == ["api", "triggers"] and moments:
                if len(parts) == 4 and parts[3] == "delete":
                    moments.delete_trigger(int(parts[2]))
                    return self._json({"ok": True})
                if len(parts) == 2:
                    r = moments.save_trigger(b)
                    return self._json(r, 400 if "error" in r else 200)
            if u.path == "/api/clientlog":
                print(f"ambient ui [{str(b.get('page'))[:40]}]: {str(b.get('msg'))[:500]}", flush=True)
                return self._json({"ok": True})
            if u.path == "/api/watch" and moments:
                moments.set_watch(str(b.get("app") or ""), bool(b.get("on")))
                return self._json({"ok": True})
            if u.path == "/api/setup/protect-claude":
                if not b.get("confirm"):
                    return self._json({"error": "needs confirm"}, 400)
                return self._json({"ok": True, "note": osal.protect_agents_install()})
            if u.path == "/api/setup/trust-ca":
                r = osal.trust_ca()
                if r.get("ok") and on_trusted:
                    on_trusted()        # what apps refused before the certificate was trusted says nothing about now
                return self._json(r)
            if u.path == "/api/setup/open-ca":
                osal.open_path(CA)
                return self._json({"ok": True})
            if (r := triggers_api.post(u.path, b, moments, events)) is not None:       # trigger programs, deciders
                return self._json(*r)
            self._json({"error": "not found"}, 404)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
