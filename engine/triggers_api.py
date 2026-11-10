"""JSON routes for trigger programs and decision providers (mounted by ui.py; same local-only rules).

  GET  /api/schema                 the catalog as wide-event schemas (field names, types, counts, examples)
  GET  /api/deciders               where checks can run, which is the default, fallback, usage counters
  POST /api/deciders               {fallback: closed|device, compile_with: device|zdr, compile_examples: masked|off,
                                    zdr_model, key_file}  -> the same status (the key itself is never accepted or shown)
  POST /api/triggers/compile       {text, using?: device|zdr, check_on?: device|zdr, preview?: bool}
                                   -> {program, explain, written_by, written_by_label, kinds, signals, preview?}
  POST /api/triggers/explain       {program} -> {program (normalized), explain, empty}
Saving and backtesting use the existing routes: POST /api/triggers and /api/triggers/preview accept
`program` and `check_on` alongside the old fields.
"""
from __future__ import annotations

from engine import deciders
from engine import triggers as programs


def get(path: str, q: dict, moments, events):
    if path == "/api/schema" and events is not None:
        try:
            limit = min(int(q.get("limit", 300)), 1000)
        except ValueError:
            limit = 300
        return {"kinds": events.schemas(limit=limit)}, 200
    if path == "/api/deciders" and moments is not None:
        return deciders.status(moments.setting), 200
    return None


def post(path: str, b: dict, moments, events):
    if moments is None:
        return None
    b = b if isinstance(b, dict) else {}
    if path == "/api/triggers/compile":
        text = str(b.get("text") or "").strip()[:600]
        if not text:
            return {"error": "write what should happen"}, 400
        r = moments.compile(text, using=b.get("using"), check_on=b.get("check_on"))
        if b.get("preview"):
            r["preview"] = moments.preview_program(r["program"])
        return r, 200
    if path == "/api/triggers/explain":
        prog = programs.normalize(b.get("program") or {})
        return {"program": prog, "explain": prog["explain"], "empty": programs.empty(prog)}, 200
    if path == "/api/deciders":
        for k, allowed in (("fallback", ("closed", "device")), ("compile_with", ("device", "zdr")),
                           ("compile_examples", ("masked", "off"))):
            if b.get(k) in allowed:
                moments.set_setting({"fallback": "zdr_fallback"}.get(k, k), b[k])
        if isinstance(b.get("zdr_model"), str) and b["zdr_model"].startswith("jev"):
            moments.set_setting("zdr_model", b["zdr_model"][:40])
        if isinstance(b.get("key_file"), str):
            moments.set_setting("zdr_key_file", b["key_file"][:500])
        return deciders.status(moments.setting), 200
    return None
