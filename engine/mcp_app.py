"""Ambient beside Claude and Codex: an MCP server whose tools define and show triggers, with an MCP App view.

Four tools: list_triggers, create_trigger, toggle_trigger, recent_moments. The first three point at one UI
resource (`ui://ambient/triggers`, MCP Apps extension `io.modelcontextprotocol/ui`, spec 2026-01-26): a host
that renders MCP Apps (Claude desktop and web, ChatGPT, VS Code, Codex desktop) draws it inline; a text-only
host (Claude Code in a terminal, Codex CLI) reads the same result as text. Nothing is saved by create_trigger
until the person presses Save in the view or the model calls it again with confirm=true.

The tools talk to Ambient through `Client`, one small interface over today's trigger API (list, suggest,
preview, save). Two implementations: `HttpClient` speaks to a running engine's local API (the stdio server in
integrations/mcp/ uses it), `LocalClient` calls a Moments object in-process (the engine's own /mcp endpoint).
When the trigger engine changes shape, only these two classes change.

Standard library only: integrations/mcp/ambient_mcp.py runs this file with any python3, no venv needed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8787"
UI_URI = "ui://ambient/triggers"
UI_MIME = "text/html;profile=mcp-app"
EXT = "io.modelcontextprotocol/ui"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_THEN = "Tell me in one line what happened and the smallest next step."
ASK_FIRST, AUTOMATIC = "ask first", "automatic"


# ---------- the one client module: today's trigger API ----------
class Client:
    """What the tools need from Ambient. Subclasses talk to it over HTTP or in-process."""

    def triggers(self) -> list[dict]: raise NotImplementedError
    def suggest(self, text: str) -> dict: raise NotImplementedError
    def preview(self, draft: dict) -> dict: raise NotImplementedError
    def save(self, trigger: dict) -> dict: raise NotImplementedError
    def moments(self, limit: int) -> list[dict]: raise NotImplementedError

    def set_enabled(self, tid: int, on: bool | None) -> dict:
        t = next((t for t in self.triggers() if int(t["id"]) == int(tid)), None)
        if t is None:
            return {"error": f"no trigger with id {tid}"}
        on = (not t.get("enabled")) if on is None else bool(on)
        keep = ("id", "name", "text", "sigs", "action", "instruction", "auto", "condition", "threshold",
                "semantic", "agent")
        r = self.save({**{k: t.get(k) for k in keep}, "enabled": on})
        return r if r.get("error") else {"ok": True, "id": int(tid), "enabled": on}


class HttpClient(Client):
    """Ambient's local API. Writes carry X-Ambient and a JSON type; no Origin is sent, Host is the URL's."""

    def __init__(self, base: str | None = None, timeout: float = 20):
        self.base = (base or os.environ.get("AMBIENT_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST")
        if body is not None:
            req.add_header("content-type", "application/json")
            req.add_header("X-Ambient", "1")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:            # the API answers errors as JSON too (400 on a bad save)
            try:
                return json.loads(e.read() or b"null")
            except Exception:
                return {"error": f"Ambient answered {e.code}"}
        except (urllib.error.URLError, OSError) as e:
            raise ConnectionError(f"Ambient isn't answering at {self.base} ({getattr(e, 'reason', e)}). "
                                  "Is the app running?") from None

    def triggers(self): return (self._call("/api/triggers") or {}).get("triggers", [])
    def suggest(self, text): return self._call("/api/triggers/suggest", {"text": text}) or {}
    def preview(self, draft): return self._call("/api/triggers/preview", draft) or {}
    def save(self, trigger): return self._call("/api/triggers", trigger) or {}
    def moments(self, limit): return self._call(f"/api/moments?limit={int(limit)}") or []


class LocalClient(Client):
    """The same, in the engine's own process (its /mcp endpoint)."""

    def __init__(self, moments):
        self.m = moments

    def triggers(self): return self.m.triggers()
    def suggest(self, text): return self.m.suggest(text)
    def preview(self, draft): return self.m.preview(draft)
    def save(self, trigger): return self.m.save_trigger(trigger)
    def moments(self, limit): return self.m.list(limit=limit)


# ---------- what the tools return ----------
def slim_trigger(t: dict) -> dict:
    """One trigger as the view and the model read it."""
    kinds = t.get("kinds") or [{"sig": s, "label": s, "app": ""} for s in t.get("sigs") or []]
    return {"id": t["id"], "when": t.get("text") or " or ".join(k.get("label", "") for k in kinds),
            "name": t.get("name") or "", "then": t.get("instruction") or ("Draft a reply" if t.get("action") == "draft" else ""),
            "watches": [{"app": k.get("app") or "", "label": k.get("label") or k.get("sig", "")} for k in kinds],
            "checked": "on this device, only when the content matches" if t.get("semantic") else "every time it happens",
            "content_check": bool(t.get("semantic")),
            "mode": AUTOMATIC if t.get("auto") else ASK_FIRST, "on": bool(t.get("enabled")),
            "fired": int(t.get("fired") or 0), "last_fired": t.get("last_fired")}


def triggers_view(client: Client, note: str = "") -> dict:
    rows = sorted((slim_trigger(t) for t in client.triggers()), key=lambda t: (not t["on"], t["id"]))
    return {"view": "triggers", "triggers": rows, "active": sum(t["on"] for t in rows),
            "automatic": sum(t["on"] and t["mode"] == AUTOMATIC for t in rows), "note": note}


def triggers_text(v: dict) -> str:
    if not v["triggers"]:
        return "No triggers yet. Create one with create_trigger and a sentence like 'someone asks me for a file'."
    lines = [f"{v['active']} of {len(v['triggers'])} triggers on, {v['automatic']} automatic."]
    for t in v["triggers"]:
        apps = ", ".join(f"{w['app']} · {w['label']}" for w in t["watches"][:4])
        lines.append(f"#{t['id']} [{'on' if t['on'] else 'off'}] When {t['when']} -> {t['then'] or t['name']} "
                     f"| watches {apps} | checked {t['checked']} | {t['mode']} | fired {t['fired']}x")
    return "\n".join(lines)


def _kind(k: dict) -> dict:
    return {"sig": k["sig"], "app": k.get("app") or "", "label": k.get("label") or k["sig"],
            "seen": k.get("n"), "example": k.get("example"), "picked": bool(k.get("picked"))}


def draft_trigger(client: Client, a: dict) -> dict:
    """From one sentence: which kinds of event it means, a backtest on recent events, and the trigger to save."""
    intent = " ".join(str(a.get("intent") or "").split())[:300]
    if len(intent) < 4:
        return {"view": "create", "error": "Say what to watch for in a few words, e.g. 'someone asks me for a file'."}
    s = client.suggest(intent)
    cands = [_kind(k) for k in s.get("kinds") or []]
    chosen = [x for x in (a.get("kinds") or []) if isinstance(x, str) and x][:40]
    if chosen:                                     # the person (or model) picked; keep their picks visible
        known = {c["sig"] for c in cands}
        for c in cands:
            c["picked"] = c["sig"] in chosen
        cands += [{"sig": x, "app": "", "label": x, "seen": None, "example": None, "picked": True}
                  for x in chosen if x not in known]
    sigs = [c["sig"] for c in cands if c["picked"]]
    content = bool(a["content_check"]) if isinstance(a.get("content_check"), bool) else bool(s.get("about_content"))
    then = " ".join(str(a.get("then") or "").split())[:2000] or DEFAULT_THEN
    draft = {"text": intent, "sigs": sigs, "semantic": content, "threshold": 0.5, "action": "ask",
             "instruction": then, "name": then.split(".")[0][:60] if a.get("then") else intent[:60],
             "auto": bool(a.get("automatic")), "enabled": True}
    pv = client.preview(draft) if sigs else {}
    return {"view": "create", "intent": intent, "kinds": cands, "sure": bool(s.get("sure")) or bool(chosen),
            "model": s.get("model"), "content_check": content, "then": then,
            "mode": AUTOMATIC if draft["auto"] else ASK_FIRST,
            "backtest": {"checked": pv.get("checked", 0), "matched": pv.get("matched", 0),
                         "span_hours": pv.get("span_hours", 0),
                         "samples": [{"who": x.get("who"), "app": x.get("app"), "text": x.get("text") or x.get("label") or "",
                                      "passed": bool(x.get("passed")), "p": x.get("p")} for x in (pv.get("samples") or [])[:6]]},
            "draft": draft, "saved": None}


def create_text(v: dict) -> str:
    if v.get("error"):
        return v["error"]
    picked = [f"{k['app']} · {k['label']} ({k['sig']})" for k in v["kinds"] if k["picked"]]
    others = [f"{k['app']} · {k['label']} ({k['sig']})" for k in v["kinds"] if not k["picked"]]
    b = v["backtest"]
    out = [f"Trigger: when {v['intent']} -> {v['then']} ({v['mode']})."]
    if v.get("saved"):
        out.insert(0, f"Saved as trigger #{v['saved']}. It is on.")
        return "\n".join(out)
    out.append("Would watch: " + ("; ".join(picked) if picked else "nothing yet (Ambient isn't sure which kind you mean)"))
    if others:
        out.append("Other candidates: " + "; ".join(others[:5]))
    out.append("Content check on this device: " + ("yes" if v["content_check"] else "no, fires every time"))
    if b["checked"]:
        out.append(f"Backtest: of the last {b['checked']} events of these kinds, it would have fired {b['matched']}.")
        out += [f"  {'✓' if x['passed'] else '–'} {x['text'][:100]}" for x in b["samples"][:4]]
    elif picked:
        out.append("Backtest: no past events of these kinds yet.")
    out.append("NOT SAVED. Show the person this, and only if they agree call create_trigger again with the same "
               "intent, kinds=[the sigs above] and confirm=true." if picked else
               "NOT SAVED. Ask the person which of the candidates they mean, then call again with kinds=[sig].")
    return "\n".join(out)


def save_draft(client: Client, v: dict) -> dict:
    if not v["draft"]["sigs"]:
        return {**v, "error": "Ambient isn't sure which kind of event that means. Pick one of the candidates first."}
    r = client.save(v["draft"])
    if r.get("error"):
        return {**v, "error": r["error"][:1].upper() + r["error"][1:] + "."}
    return {**v, "saved": r.get("id")}


def moments_text(rows: list[dict]) -> str:
    if not rows:
        return "No moments yet: nothing has matched a trigger."
    return "\n".join(f"#{m['id']} {m.get('app')} · {m.get('title') or m.get('kind')}"
                     f"{' · ' + m['who'] if m.get('who') and m.get('who') != 'you' else ''}: "
                     f"{(m.get('text') or '')[:140]} [{m.get('status')}]" for m in rows)


# ---------- MCP ----------
UI_META = {"ui": {"resourceUri": UI_URI}, "ui/resourceUri": UI_URI}
TOOLS = [
    {"name": "list_triggers", "title": "Ambient triggers",
     "description": "Show the user's Ambient triggers: the sentence, the kinds of app activity each one watches, "
                    "whether content is checked on this device, ask-first or automatic, on/off, and times fired.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}, "_meta": UI_META},
    {"name": "create_trigger", "title": "New Ambient trigger",
     "description": "Draft an Ambient trigger from one sentence about what to watch for in the user's apps "
                    "(e.g. 'someone asks me for a file', 'I create an issue in Linear'). Returns which kinds of "
                    "event it would match and a backtest on recent activity. Saves NOTHING unless confirm=true; "
                    "only pass confirm=true after the user has seen the draft and agreed (the inline view has "
                    "its own Save button).",
     "inputSchema": {"type": "object", "required": ["intent"], "properties": {
         "intent": {"type": "string", "description": "When this happens, in the user's words."},
         "then": {"type": "string", "description": "What the user's agent should do when it fires (optional)."},
         "kinds": {"type": "array", "items": {"type": "string"},
                   "description": "Event kinds (sig values from a previous draft) to use instead of Ambient's guess."},
         "content_check": {"type": "boolean", "description": "Also judge each event's content on this device. "
                                                             "Default: Ambient decides from the sentence."},
         "automatic": {"type": "boolean", "default": False,
                       "description": "Run without asking. Default false: ask first."},
         "confirm": {"type": "boolean", "default": False, "description": "Save it. Only after the user agreed."}}},
     "_meta": UI_META},
    {"name": "toggle_trigger", "title": "Switch an Ambient trigger on or off",
     "description": "Turn one of the user's Ambient triggers on or off (by id from list_triggers).",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "integer"}, "on": {"type": "boolean", "description": "Omit to flip it."}}},
     "annotations": {"idempotentHint": True}, "_meta": UI_META},
    {"name": "recent_moments", "title": "Recent Ambient moments",
     "description": "The last few moments: events in the user's apps that matched one of their triggers.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 5, "maximum": 20}}},
     "annotations": {"readOnlyHint": True}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def call_tool(client: Client, name: str, a: dict) -> dict:
    """One tool call -> an MCP CallToolResult (text for every host, structuredContent for the view)."""
    def result(text, data=None, error=False):
        out = {"content": [{"type": "text", "text": text}], "isError": error}
        if data is not None:
            out["structuredContent"] = data
        return out
    try:
        if name == "list_triggers":
            v = triggers_view(client)
            return result(triggers_text(v), v)
        if name == "toggle_trigger":
            if a.get("id") is None:
                return result("toggle_trigger needs the trigger's id.", error=True)
            r = client.set_enabled(int(a["id"]), a.get("on") if isinstance(a.get("on"), bool) else None)
            if r.get("error"):
                return result(r["error"], triggers_view(client, r["error"]), error=True)
            v = triggers_view(client, f"Trigger #{r['id']} is {'on' if r['enabled'] else 'off'}.")
            return result(v["note"] + "\n" + triggers_text(v), v)
        if name == "create_trigger":
            v = draft_trigger(client, a)
            if not v.get("error") and a.get("confirm") is True:
                v = save_draft(client, v)
            return result(create_text(v), v, error=bool(v.get("error")))
        if name == "recent_moments":
            rows = client.moments(min(max(int(a.get("limit") or 5), 1), 20))
            slim = [{k: m.get(k) for k in ("id", "ts", "app", "kind", "who", "place", "title", "text", "status")}
                    for m in rows]
            return result(moments_text(slim), {"view": "moments", "moments": slim})
    except ConnectionError as e:
        return result(str(e), error=True)
    return result(f"unknown tool {name}", error=True)


def ui_html() -> str:
    return VIEW_HTML


def handle(client: Client, msg: dict, fallback=None, extra_tools: list | None = None) -> dict | None:
    """One JSON-RPC message -> its answer (None for a notification). `fallback(msg)` answers tool calls this
    module doesn't own (the engine's older list_moments / wait_for_moment)."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    ok = lambda r: {"jsonrpc": "2.0", "id": mid, "result": r}  # noqa: E731
    err = lambda c, m: {"jsonrpc": "2.0", "id": mid, "error": {"code": c, "message": m}}  # noqa: E731
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                   "capabilities": {"tools": {}, "resources": {}, "extensions": {EXT: {}}},
                   "serverInfo": {"name": "ambient", "title": "Ambient", "version": "0.3"},
                   "instructions": "Ambient watches the user's apps on their device and fires their triggers. "
                                   "Use list_triggers to show them, create_trigger to draft one from a sentence "
                                   "(it saves only with confirm=true, after the user agreed), toggle_trigger to "
                                   "switch one on or off."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS + list(extra_tools or [])})
    if method == "resources/list":
        return ok({"resources": [{"uri": UI_URI, "name": "Ambient triggers", "mimeType": UI_MIME,
                                  "description": "Ambient's triggers and the one-sentence trigger builder."}]})
    if method == "resources/templates/list":
        return ok({"resourceTemplates": []})
    if method == "resources/read":
        if params.get("uri") != UI_URI:
            return err(-32002, f"no resource {params.get('uri')}")
        return ok({"contents": [{"uri": UI_URI, "mimeType": UI_MIME, "text": ui_html(),
                                 "_meta": {"ui": {"prefersBorder": True, "csp": {}}}}]})
    if method == "tools/call":
        name = params.get("name")
        if name in TOOL_NAMES:
            return ok(call_tool(client, name, params.get("arguments") or {}))
        if fallback:
            return fallback(msg)
        return err(-32602, f"unknown tool {name}")
    if fallback and method not in ("resources/subscribe",):
        return fallback(msg)
    return err(-32601, f"unknown method {method}")


def engine_handle(moments, msg):
    """The engine's /mcp: these tools in-process, plus the older moment tools from engine.moments."""
    from engine.moments import TOOLS as MOMENT_TOOLS, mcp_handle
    return handle(LocalClient(moments), msg, fallback=lambda m: mcp_handle(moments, m), extra_tools=MOMENT_TOOLS)


def serve_stdio(client: Client | None = None, inp=None, out=None):
    """MCP over stdio: one JSON-RPC message per line in, one per line out. Logs go to stderr."""
    client = client or HttpClient()
    inp, out = inp or sys.stdin, out or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(msg, list):
                reply = [r for r in (handle(client, m) for m in msg) if r] or None
            else:
                reply = handle(client, msg)
        if reply:
            out.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            out.flush()


VIEW_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ambient triggers</title>
<style>
:root{--paper:#f7f0dc;--card:#fffaee;--chip:#ece4cb;--line:#e2d8bc;--ink:#1b1747;--ink-2:#4a4577;--ink-3:#7a76a3;
 --ultra:#2b27c9;--violet-ink:#5638c4;--lime:#c9ec3f;--coral:#ff6f59;--coral-ink:#c23a26;--teal-ink:#0e6b66;
 --chip-lime:#e6f6a3;--chip-lime-ink:#2c3a07;--chip-aqua:#c9f6ef;--shadow:#1b1747;
 --display:"Young Serif","Iowan Old Style",Georgia,serif;--body:"Schibsted Grotesk",ui-sans-serif,-apple-system,"Segoe UI",sans-serif;
 --mono:"Spline Sans Mono",ui-monospace,Menlo,monospace;color-scheme:light}
:root[data-theme=dark]{--paper:#13113a;--card:#1e1a55;--chip:#28236a;--line:#2e2876;--ink:#f7f0dc;--ink-2:#c7c1ea;--ink-3:#928cc4;
 --violet-ink:#b9a8ff;--coral-ink:#ff9a86;--teal-ink:#7ff0e3;--shadow:#05041a;--chip-lime:rgba(201,236,63,.16);--chip-lime-ink:#d8f56a;
 --chip-aqua:rgba(98,230,216,.14);color-scheme:dark}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--paper:#13113a;--card:#1e1a55;--chip:#28236a;--line:#2e2876;--ink:#f7f0dc;
 --ink-2:#c7c1ea;--ink-3:#928cc4;--violet-ink:#b9a8ff;--coral-ink:#ff9a86;--teal-ink:#7ff0e3;--shadow:#05041a;
 --chip-lime:rgba(201,236,63,.16);--chip-lime-ink:#d8f56a;--chip-aqua:rgba(98,230,216,.14);color-scheme:dark}}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 var(--body);-webkit-font-smoothing:antialiased}
main{padding:14px 16px 16px}
button,input{font:inherit;color:inherit}
.top{display:flex;align-items:center;gap:8px;margin:0 0 12px}
.mark{width:10px;height:10px;background:var(--lime);clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%)}
.brand{font:400 18px/1 var(--display)}
.hint{margin-left:auto;font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.sh{filter:drop-shadow(3px 3px 0 var(--shadow));margin:0 3px 12px 0}
.slip{background:var(--card);padding:12px 14px;
 clip-path:polygon(0 0,calc(100% - 10px) 0,100% 10px,100% 100%,10px 100%,0 calc(100% - 10px));position:relative}
.sh.off{opacity:.55}
.when{display:flex;align-items:flex-start;gap:10px}
.sentence{flex:1;margin:0;font:400 16px/1.3 var(--display)}
.sentence i{font-style:normal;color:var(--violet-ink)}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin:8px 0 6px}
.chip{font:500 12px/1 var(--body);padding:5px 8px;background:var(--chip);color:var(--ink-2)}
.chip.lock{background:var(--chip-aqua);color:var(--teal-ink)}
.chip.auto{background:var(--chip-lime);color:var(--chip-lime-ink)}
.then{font-size:13px;color:var(--ink-2);margin:2px 0 0}.then b{color:var(--ink);font-weight:600}
.fired{font:500 11px/1.3 var(--mono);color:var(--ink-3);margin-top:6px}
.switch{appearance:none;flex:none;width:38px;height:22px;margin:0;background:var(--chip);border:0;cursor:pointer;position:relative;border-radius:2px}
.switch::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;background:var(--ink-3);transition:left .15s,background .15s;
 clip-path:polygon(50% 0,100% 50%,50% 100%,0 50%)}
.switch:checked{background:var(--ink)}.switch:checked::after{left:19px;background:var(--lime)}
.switch:focus-visible,.btn:focus-visible,.opt:focus-within{outline:2px solid var(--ultra);outline-offset:2px}
.lbl{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);margin:12px 0 6px}
.opt{display:flex;gap:8px;align-items:flex-start;padding:7px 8px;margin:0 0 4px;background:var(--paper);cursor:pointer}
.opt.on{background:var(--chip-lime);color:var(--chip-lime-ink)}
.opt input{margin:2px 0 0;accent-color:var(--ink)}
.opt small{display:block;color:var(--ink-3);font-size:12px}.opt.on small{color:inherit;opacity:.75}
.sum{font-size:13px;margin:0 0 6px}
.s{display:flex;gap:8px;font-size:12.5px;color:var(--ink-3);padding:2px 0}.s.yes{color:var(--ink)}
.s .p{flex:none;width:34px;font:500 11px/1.6 var(--mono)}
.then-in{width:100%;padding:8px 10px;border:1px solid var(--line);background:var(--paper)}
.row{display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap}
.seg{display:inline-flex;background:var(--chip)}.seg button{border:0;background:none;padding:6px 10px;cursor:pointer;font-size:12.5px}
.seg button.on{background:var(--ink);color:var(--paper)}
.btn{border:0;cursor:pointer;padding:8px 14px;background:var(--ink);color:var(--paper);font-weight:600;
 clip-path:polygon(6px 0,100% 0,100% calc(100% - 6px),calc(100% - 6px) 100%,0 100%,0 6px)}
.btn.ghost{background:none;color:var(--ink-2);padding:8px 6px;text-decoration:underline;text-underline-offset:3px}
.btn[disabled]{opacity:.45;cursor:default}
.grow{flex:1}
.err{color:var(--coral-ink);font-size:13px;margin:8px 0 0}
.ok{font:400 16px/1.3 var(--display)}
.note{font-size:12.5px;color:var(--ink-3);margin:0 0 10px}
.empty{color:var(--ink-3);font-size:13px}
.busy{opacity:.6;pointer-events:none}
</style></head>
<body><main id="root"><div class="top"><span class="mark"></span><span class="brand">Ambient</span><span class="hint">loading…</span></div></main>
<script>
"use strict";
const $root = document.getElementById("root");
const clip = (s, n) => (s = String(s || "")).length > n ? s.slice(0, n - 1).trimEnd() + "…" : s;
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ago = ts => { if (!ts) return ""; const s = Date.now()/1000 - ts; return s < 90 ? "just now" : s < 5400 ? Math.round(s/60) + "m ago" : s < 129600 ? Math.round(s/3600) + "h ago" : Math.round(s/86400) + "d ago"; };

/* ── MCP Apps bridge: JSON-RPC over postMessage with the host (spec 2026-01-26) ── */
let nid = 1; const pending = new Map();
const post = m => window.parent.postMessage({jsonrpc: "2.0", ...m}, "*");
const rpc = (method, params) => new Promise((res, rej) => { const id = nid++; pending.set(id, {res, rej}); post({id, method, params}); });
const notify = (method, params) => post({method, params});
window.addEventListener("message", e => {
  if (e.source !== window.parent) return;
  const m = e.data; if (!m || m.jsonrpc !== "2.0") return;
  if (m.id != null && !m.method) {
    const p = pending.get(m.id); if (!p) return; pending.delete(m.id);
    return m.error ? p.rej(new Error(m.error.message || "the host refused")) : p.res(m.result);
  }
  if (m.method === "ui/notifications/tool-result") show(m.params);
  else if (m.method === "ui/notifications/tool-input") { if (!S.view) waiting(m.params?.arguments); }
  else if (m.method === "ui/notifications/host-context-changed") context(m.params);
  else if (m.method === "ui/notifications/tool-cancelled") { S.view = S.view || "cancelled"; render(); }
  if (m.id != null && m.method) post({id: m.id, result: {}});      // ping, ui/resource-teardown
});
function context(c){ if (c?.theme) document.documentElement.dataset.theme = c.theme; }
async function call(name, args){
  $root.classList.add("busy");
  try { const r = await rpc("tools/call", {name, arguments: args}); show(r); return r; }
  catch (err) { S.err = err.message; render(); }
  finally { $root.classList.remove("busy"); }
}
function tell(text){ rpc("ui/update-model-context", {content: [{type: "text", text}]}).catch(() => {}); }

/* ── state and drawing ── */
const S = {view: null};
function show(r){
  const d = r?.structuredContent; S.err = null;
  if (!d) { S.view = "text"; S.text = (r?.content || []).map(c => c.text || "").join("\n"); return render(); }
  Object.assign(S, d); S.view = d.view; if (d.view === "create") S.edit = {kinds: new Set(d.kinds.filter(k => k.picked).map(k => k.sig)), auto: d.mode === "automatic", then: d.then};
  render();
}
function waiting(a){ $root.innerHTML = head("…") + `<p class="empty">${a?.intent ? "Reading “" + esc(a.intent) + "” on this device…" : "Asking Ambient…"}</p>`; }
const head = hint => `<div class="top"><span class="mark"></span><span class="brand">Ambient</span><span class="hint">${esc(hint)}</span></div>`;

function render(){
  if (S.view === "triggers") $root.innerHTML = triggers();
  else if (S.view === "create") $root.innerHTML = create();
  else if (S.view === "moments") $root.innerHTML = head("moments") + (S.moments.map(m => `<div class="sh"><div class="slip"><p class="then"><b>${esc(m.app)}</b> · ${esc(m.title || m.kind)}${m.who && m.who !== "you" ? " · " + esc(m.who) : ""}</p><p class="sentence" style="font-size:14px">${esc(m.text)}</p></div></div>`).join("") || `<p class="empty">Nothing has matched a trigger yet.</p>`);
  else if (S.view === "text") $root.innerHTML = head("") + `<p class="empty">${esc(S.text)}</p>`;
  else if (S.view === "cancelled") $root.innerHTML = head("") + `<p class="empty">Cancelled.</p>`;
}

function triggers(){
  const ts = S.triggers || [];
  const hint = `${S.active} on${S.automatic ? " · " + S.automatic + " automatic" : ""}${ts.length > S.active ? " · " + (ts.length - S.active) + " off" : ""}`;
  return head(hint) + (S.note ? `<p class="note">${esc(S.note)}</p>` : "") + (S.err ? `<p class="err">${esc(S.err)}</p>` : "") +
    (ts.map(t => `<div class="sh ${t.on ? "" : "off"}"><div class="slip">
      <div class="when"><p class="sentence"><i>When</i> ${esc(t.when)}</p>
        <input type="checkbox" class="switch" data-toggle="${t.id}" ${t.on ? "checked" : ""} aria-label="Trigger ${esc(t.when)} on"></div>
      <div class="chips">${t.watches.slice(0, 4).map(w => `<span class="chip">${esc(w.app)} · ${esc(w.label)}</span>`).join("")}${t.watches.length > 4 ? `<span class="chip">+${t.watches.length - 4} more</span>` : ""}
        ${t.content_check ? `<span class="chip lock">Content checked on this device</span>` : ""}
        <span class="chip ${t.mode === "automatic" ? "auto" : ""}">${t.mode === "automatic" ? "Automatic" : "Ask me first"}</span></div>
      <p class="then">→ <b>${esc(clip(t.then || t.name, 120))}</b></p>
      <div class="fired">${t.fired ? `Fired ${t.fired}× · ${ago(t.last_fired)}` : "Hasn't fired yet"}</div></div></div>`).join("")
     || `<p class="empty">No triggers yet. Ask for one in a sentence, like “someone asks me for a file”.</p>`);
}

function create(){
  if (S.error && !S.kinds) return head("new trigger") + `<p class="err">${esc(S.error)}</p>`;
  if (S.saved) return head("saved") + `<div class="sh"><div class="slip"><p class="ok">Saved. It's on.</p><p class="then"><i>When</i> ${esc(S.intent)} → <b>${esc(S.then)}</b></p>
     <div class="row"><span class="chip ${S.mode === "automatic" ? "auto" : ""}">${S.mode === "automatic" ? "Automatic" : "Ask me first"}</span><span class="grow"></span>
     <button class="btn ghost" data-k="all">See all triggers</button></div></div></div>`;
  const E = S.edit, b = S.backtest || {}, span = b.span_hours >= 1 ? ` over the last ${Math.round(b.span_hours)}h` : "";
  const kinds = S.kinds.map(k => `<label class="opt ${E.kinds.has(k.sig) ? "on" : ""}"><input type="checkbox" data-sig="${esc(k.sig)}" ${E.kinds.has(k.sig) ? "checked" : ""}>
      <span><b>${esc(k.label)}</b><small>${esc(k.app)}${k.seen ? " · seen " + k.seen + "×" : ""}${k.example ? " · “" + esc(k.example) + "”" : ""}</small></span></label>`).join("");
  const test = !E.kinds.size ? "" : !b.checked ? `<p class="sum">No past activity of this kind to test against yet.</p>` :
    `<p class="sum">${S.content_check ? `Tested on your last ${b.checked} like this${span}: it would have fired <b>${b.matched}</b> time${b.matched === 1 ? "" : "s"}.` : `Fires every time: that was <b>${b.checked}</b> time${b.checked === 1 ? "" : "s"}${span}.`}</p>` +
    (b.samples || []).map(s => `<div class="s ${s.passed ? "yes" : ""}"><span class="p">${s.p == null ? (s.passed ? "✓" : "–") : Math.round(s.p * 100) + "%"}</span><span>${s.who && s.who !== "you" ? esc(s.who) + ": " : ""}${esc(s.text)}</span></div>`).join("");
  return head("new trigger") + `<div class="sh"><div class="slip">
    <p class="sentence"><i>When</i> ${esc(S.intent)}</p>
    <div class="lbl">Watches</div>${kinds || `<p class="empty">Nothing like that seen yet. Do it once in the app, then try again.</p>`}
    ${!S.sure && !E.kinds.size && S.kinds.length ? `<p class="note">Not sure which of these you mean. Tick one.</p>` : ""}
    ${S.content_check ? `<div class="chips"><span class="chip lock">Content checked on this device</span></div>` : ""}
    ${test ? `<div class="lbl">Backtest</div>${test}` : ""}
    <div class="lbl">Then your agent</div><input class="then-in" id="then" value="${esc(E.then)}" aria-label="What your agent should do">
    <div class="row"><div class="seg" role="group" aria-label="When to run"><button data-auto="0" class="${E.auto ? "" : "on"}">Ask me first</button><button data-auto="1" class="${E.auto ? "on" : ""}">Automatic</button></div>
      <span class="grow"></span><button class="btn" data-k="save" ${E.kinds.size ? "" : "disabled"}>Save trigger</button></div>
    ${S.err || S.error ? `<p class="err">${esc(S.err || S.error)}</p>` : ""}</div></div>`;
}

function args(extra){ const E = S.edit; return {intent: S.intent, then: (document.getElementById("then")?.value || E.then).trim(), kinds: [...E.kinds], automatic: E.auto, content_check: S.content_check, ...extra}; }
$root.addEventListener("change", async e => {
  const t = e.target;
  if (t.dataset.toggle) { const r = await call("toggle_trigger", {id: +t.dataset.toggle, on: t.checked}); if (r) tell(`The user switched Ambient trigger #${t.dataset.toggle} ${t.checked ? "on" : "off"}.`); }
  if (t.dataset.sig) { S.edit.then = document.getElementById("then").value; t.checked ? S.edit.kinds.add(t.dataset.sig) : S.edit.kinds.delete(t.dataset.sig); await call("create_trigger", args()); }
});
$root.addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.auto) { S.edit.then = document.getElementById("then").value; S.edit.auto = b.dataset.auto === "1"; render(); }
  if (b.dataset.k === "save") { const r = await call("create_trigger", args({confirm: true})); if (r?.structuredContent?.saved) tell(`The user saved Ambient trigger #${r.structuredContent.saved}: when ${S.intent}.`); }
  if (b.dataset.k === "all") call("list_triggers", {});
});

/* ── size: tell the host how tall we are ── */
let lastH = 0;
new ResizeObserver(() => { const h = Math.ceil(document.documentElement.scrollHeight); if (h !== lastH) { lastH = h; notify("ui/notifications/size-changed", {height: h}); } }).observe(document.body);

rpc("ui/initialize", {protocolVersion: "2026-01-26", clientInfo: {name: "ambient-triggers", version: "0.3"},
  appCapabilities: {availableDisplayModes: ["inline"]}})
  .then(r => { context(r?.hostContext); notify("ui/notifications/initialized", {}); })
  .catch(() => notify("ui/notifications/initialized", {}));
</script></body></html>
"""


if __name__ == "__main__":
    serve_stdio()
