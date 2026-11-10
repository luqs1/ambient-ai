"""Trigger programs: a trigger as data.

A program is plain JSON a person (or a decision model) can read, edit and test:

    {"v": 1,
     "when":  "a customer emails about a refund",                  # the user's words
     "match": {"kinds": ["Gmail|*"],                                # globs over kind signatures; [] = any kind
               "apps":  ["Gmail", "Outlook"],                       # [] = any app
               "where": [{"field": "direction", "op": "eq", "value": "in"},
                         {"field": "fields.*attendees.count", "op": "gt", "value": 0}],
               "any":   []},                                        # at least one of these, when given
     "check": {"condition": "the message asks for a refund",        # optional semantic part
               "threshold": 0.7, "run": "device" | "zdr",
               "fields": ["app", "label", "direction", "title", "text"]},   # what the checker is shown
     "do":    {"action": "ask" | "draft", "instruction": "draft a reply and file a ticket",
               "steps": ["draft a reply", "file a ticket"]}}

`match` is deterministic and runs on every event (cheap); `check` runs only on events that pass it.
Field names are those of the wide record (events.wide_view); a field may use `*` to mean any key that
fits ("fields.*.count"). Conditions can also be written as short strings: "direction=in", "op~create",
"fields.event.attendees.count>0", "text contains invoice", "who exists".
"""
from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase

OPS = ("eq", "ne", "in", "nin", "contains", "not_contains", "matches", "exists", "missing", "gt", "gte", "lt", "lte")
RUN = ("device", "zdr")
DEFAULT_CHECK_FIELDS = ["app", "label", "direction", "who", "place", "title", "recipients", "text"]
CHECK_FIELDS_MAX = 12
_COND = re.compile(r"^\s*(?P<field>[\w.\[\]*\-]+)\s*(?P<op>!=|>=|<=|=|>|<|~|!~|\s+(?:contains|not contains|matches|in|"
                   r"exists|missing)\b)\s*(?P<value>.*?)\s*$", re.I)
_OPWORD = {"=": "eq", "!=": "ne", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "~": "contains", "!~": "not_contains",
           "contains": "contains", "not contains": "not_contains", "matches": "matches", "in": "in",
           "exists": "exists", "missing": "missing"}
OP_WORDS = {"eq": "is", "ne": "is not", "in": "is one of", "nin": "is none of", "contains": "mentions",
            "not_contains": "does not mention", "matches": "matches", "exists": "is present", "missing": "is absent",
            "gt": "is more than", "gte": "is at least", "lt": "is less than", "lte": "is at most"}
RUN_LABEL = {"device": "checked on this device", "zdr": "checked by Jev, zero data retention"}


def parse_cond(c) -> dict | None:
    """A condition as {field, op, value}, from a dict or a short string."""
    if isinstance(c, dict):
        f, op = str(c.get("field") or "").strip(), str(c.get("op") or "eq").strip().lower()
        if not f or op not in OPS:
            return None
        return {"field": f[:120], "op": op, "value": _clean_value(c.get("value"))}
    if not isinstance(c, str):
        return None
    m = _COND.match(c)
    if not m:
        return None
    op = _OPWORD[m["op"].strip().lower()]
    v = m["value"].strip().strip("\"'“”")
    value = [x.strip() for x in v.split(",") if x.strip()] if op == "in" else _num(v)
    return {"field": m["field"], "op": op, "value": None if op in ("exists", "missing") else value}


def _num(v):
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
        return float(v) if "." in v else int(v)
    return v


def _clean_value(v):
    if isinstance(v, (list, tuple)):
        return [x for x in (_clean_value(y) for y in v[:20]) if x is not None]
    if isinstance(v, str):
        return v[:200]
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    return str(v)[:200]


def normalize(p) -> dict:
    """A valid program from whatever arrived (a dict or JSON text). Unknown keys are dropped."""
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except Exception:
            p = {}
    p = p if isinstance(p, dict) else {}
    m = p.get("match") if isinstance(p.get("match"), dict) else {}
    kinds = [k[:200] for k in (m.get("kinds") or []) if isinstance(k, str) and k.strip()][:40]
    apps = [a[:60] for a in (m.get("apps") or []) if isinstance(a, str) and a.strip() and a != "*"][:20]
    where = [c for c in (parse_cond(x) for x in (m.get("where") or [])[:20]) if c]
    any_ = [c for c in (parse_cond(x) for x in (m.get("any") or [])[:20]) if c]
    out = {"v": 1, "when": str(p.get("when") or "")[:300],
           "match": {"kinds": kinds, "apps": apps, "where": where, "any": any_}, "check": None}
    ck = p.get("check")
    if isinstance(ck, dict) and str(ck.get("condition") or "").strip():
        try:
            thr = min(max(float(ck.get("threshold") or 0.5), 0.05), 0.95)
        except (TypeError, ValueError):
            thr = 0.5
        fields = [f[:120] for f in (ck.get("fields") or DEFAULT_CHECK_FIELDS) if isinstance(f, str)][:CHECK_FIELDS_MAX]
        out["check"] = {"condition": str(ck["condition"]).strip()[:300], "threshold": thr,
                        "run": ck.get("run") if ck.get("run") in RUN else "device",
                        "fields": fields or list(DEFAULT_CHECK_FIELDS)}
    do = p.get("do") if isinstance(p.get("do"), dict) else {}
    instr = str(do.get("instruction") or "").strip()[:2000]
    steps = [s.strip()[:300] for s in (do.get("steps") or []) if isinstance(s, str) and s.strip()][:8] or split_steps(instr)
    out["do"] = {"action": do.get("action") if do.get("action") in ("ask", "draft") else "ask",
                 "instruction": instr, "steps": steps}
    out["explain"] = explain(out)
    return out


def empty(p: dict) -> bool:
    """A program that would fire on everything you do and everything you get."""
    m = p["match"]
    return not (m["kinds"] or m["apps"] or m["where"] or m["any"] or p.get("check"))


# ---------- matching ----------
def _values(w: dict, field: str) -> list:
    if field == "content":                          # what the event says: text, title, subject
        return [v for v in (w.get("text"), w.get("title")) if v]
    if "*" in field:
        return [v for k, v in w.items() if fnmatchcase(k, field)]
    return [w[field]] if field in w else []


def _num_of(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _str_eq(a, b) -> bool:
    if isinstance(a, (int, float)) and not isinstance(a, bool) and _num_of(b) is not None:
        return float(a) == float(_num_of(b))
    if isinstance(a, bool):
        return str(a).lower() == str(b).lower()
    return str(a).strip().lower() == str(b).strip().lower()


def cond_holds(c: dict, w: dict) -> bool:
    vals, op, want = _values(w, c["field"]), c["op"], c.get("value")
    if op == "exists":
        return bool(vals)
    if op == "missing":
        return not vals
    if op == "ne":
        return not any(_str_eq(v, want) for v in vals)
    if op == "not_contains":
        words = want if isinstance(want, list) else [want]
        return not any(str(x).lower() in str(v).lower() for v in vals for x in words if x not in (None, ""))
    if op == "nin":
        return not any(_str_eq(v, x) for v in vals for x in (want or []))
    for v in vals:
        if op == "eq" and _str_eq(v, want):
            return True
        if op == "in" and any(_str_eq(v, x) for x in (want if isinstance(want, list) else [want])):
            return True
        if op == "contains":
            words = want if isinstance(want, list) else [want]
            if any(x not in (None, "") and str(x).lower() in str(v).lower() for x in words):
                return True
        if op == "matches":
            try:
                if re.search(str(want)[:200], str(v)[:4000], re.I):
                    return True
            except re.error:
                return False
        if op in ("gt", "gte", "lt", "lte"):
            a, b = _num_of(v), _num_of(want)
            if a is not None and b is not None and {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]:
                return True
    return False


def matches(p: dict, w: dict) -> bool:
    """Does this wide event pass the program's deterministic part?"""
    m = p["match"]
    sigs = [s for s in (w.get("kind"), w.get("kind0")) if s]
    if m["kinds"] and not any(fnmatchcase(s, k) for s in sigs for k in m["kinds"]):
        return False
    if m["apps"] and str(w.get("app") or "").lower() not in {a.lower() for a in m["apps"]}:
        return False
    if not all(cond_holds(c, w) for c in m["where"]):
        return False
    if m["any"] and not any(cond_holds(c, w) for c in m["any"]):
        return False
    return True


def check_state(p: dict, w: dict) -> dict:
    """Only the fields the check needs, to show a decision model (and nothing for an absent check)."""
    ck = p.get("check") or {}
    out = {}
    for f in ck.get("fields") or DEFAULT_CHECK_FIELDS:
        if f == "content":
            out.update({k: w[k] for k in ("title", "text") if w.get(k)})
        elif "*" in f:
            for k, v in w.items():
                if fnmatchcase(k, f) and len(out) < 24:
                    out[k] = v
        elif f in w:
            out[f] = w[f]
    if "text" in out and isinstance(out["text"], str):
        out["text"] = out["text"][:1500]
    return out


# ---------- explaining ----------
def _field_words(f: str) -> str:
    named = {"text": "the text", "content": "what it says", "title": "the title", "who": "who it's from",
             "place": "where it happened", "direction": "the direction", "op": "the operation",
             "recipients": "the recipients", "recipients.count": "the number of recipients", "app": "the app",
             "method": "the request method", "status": "the status", "host": "the site"}
    if f in named:
        return named[f]
    tail = f.split(".", 1)[1] if f.startswith(("fields.", "resp.")) else f
    tail = tail.replace("[]", "").replace("*.", "").replace("*", "").strip(".")
    if tail.endswith(".count"):
        return "the number of " + tail[:-6].replace(".", " ").replace("_", " ")
    return tail.replace(".", " ").replace("_", " ")


def _cond_words(c: dict) -> str:
    f, op, v = c["field"], c["op"], c.get("value")
    if f == "direction" and op == "eq":
        return {"in": "it reaches you", "out": "you did it"}.get(str(v), f"the direction is {v}")
    if op in ("exists", "missing"):
        return f"{_field_words(f)} {OP_WORDS[op]}"
    val = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
    if op in ("contains", "not_contains"):
        val = f"“{val}”"
    return f"{_field_words(f)} {OP_WORDS[op]} {val}"


def explain(p: dict) -> str:
    """One plain sentence that says what the program does."""
    m = p["match"]
    parts = []
    if m["kinds"]:
        names = sorted({k.split("|", 1)[0].split(":", 1)[0] for k in m["kinds"]} - {"*", ""})
        parts.append(f"one of {len(m['kinds'])} kinds of event" + (f" in {_join(names)}" if names else "")
                     if len(m["kinds"]) > 1 else f"a {_kind_words(m['kinds'][0])}")
    else:
        parts.append("anything" + (f" in {_join(m['apps'])}" if m["apps"] else " in any app"))
    if m["kinds"] and m["apps"]:
        parts.append(f"in {_join(m['apps'])}")
    conds = [_cond_words(c) for c in m["where"]]
    if m["any"]:
        conds.append("either " + " or ".join(_cond_words(c) for c in m["any"]))
    s = "Fires on " + " ".join(parts)
    if conds:
        s += " where " + _join(conds)
    ck = p.get("check")
    if ck:
        s += f", if “{ck['condition']}” holds ({RUN_LABEL[ck['run']]}, at {round(ck['threshold'] * 100)}% or more)"
    do = p.get("do") or {}
    steps = do.get("steps") or []
    if do.get("action") == "draft" and not steps:
        s += "; then your agent drafts a reply"
    elif steps:
        s += "; then your agent will " + _join(steps)
    elif do.get("instruction"):
        s += "; then your agent: " + do["instruction"][:120]
    return s + "."


def _kind_words(sig: str) -> str:
    if sig in ("*:dm",):
        return "direct message in a chat app"
    if sig in ("*:mention",):
        return "mention in a chat app"
    if sig in ("*:sent",):
        return "message you send in a chat app"
    if sig in ("*:group",):
        return "group chat message"
    if sig == "*|VISIT":
        return "page you open on any website"
    app, _, rest = sig.partition("|")
    if not rest and ":" in sig:
        app, kind = sig.split(":", 1)
        return f"{kind} in {app.capitalize()}"
    from engine.events import words
    op = rest.split("#", 1)[1].removesuffix("+text") if "#" in rest else ""
    label = words(op) or words(" ".join(rest.removesuffix("+text").split(" ", 1)[-1].split("/")[-2:])) or "event"
    return f"“{label}” in {app}" if app and app != "*" else f"“{label}”"


def _join(xs) -> str:
    xs = [x for x in xs if x]
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1] if xs else ""


# ---------- one-prompt triggers ----------
_WHEN = re.compile(r"^\s*(when(?:ever)?|if|every time|each time|as soon as|once|after)\s+", re.I)
_THEN = re.compile(r"\s*(?:,\s*(?:then\s+)?|\s+then\s+|\s*[-–—:;]\s+)(?=(?:please\s+)?(?:draft|write|reply|file|create|"
                   r"open|make|send|add|summari[sz]e|tell|notify|remind|ask|log|save|post|forward|book|schedule|"
                   r"update|check|look|find|flag|label|move|archive|let me know|give)\b)", re.I)


_ANYAPP = re.compile(r"^(?:(?:in|on|from) )?any (?:app|application|website|site)\s*[:,\-–—]?\s*", re.I)


def split_intent(text: str) -> tuple[str, str]:
    """'whenever a customer emails about a refund, draft a reply and file a ticket' ->
    ('a customer emails about a refund', 'draft a reply and file a ticket'). No 'then' part -> ''."""
    s = (text or "").strip().rstrip(".!")
    m = _THEN.search(s)
    when, do = (s[:m.start()], s[m.end():]) if m else (s, "")
    when = _WHEN.sub("", when).strip().rstrip(",")
    when = _ANYAPP.sub("", when).strip()
    when = _WHEN.sub("", when).strip()
    return when, do.strip()


def says_any_app(text: str) -> bool:
    s = _WHEN.sub("", (text or "").strip())
    return bool(_ANYAPP.match(s) or re.search(r"\b(in|on|from) any (app|application)\b", s, re.I))


def split_steps(instr: str) -> list[str]:
    s = (instr or "").strip().rstrip(".")
    if not s or len(s) > 300 or "\n" in s:
        return []
    parts = [x.strip() for x in re.split(r",\s*(?:and\s+|then\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+(?=(?:draft|"
                                         r"write|reply|file|create|open|make|send|add|summari[sz]e|tell|notify|"
                                         r"remind|ask|log|save|post|forward|book|schedule|update|flag|label|move|"
                                         r"archive)\b)", s) if x.strip()]
    return parts if len(parts) > 1 else []


def do_from(text: str) -> dict:
    """The agent's part of a one-prompt trigger."""
    if not text:
        return {"action": "ask", "instruction": "", "steps": []}
    if re.fullmatch(r"(please\s+)?draft (me )?a (short )?reply", text.strip(), re.I):
        return {"action": "draft", "instruction": "", "steps": []}
    return {"action": "ask", "instruction": text[0].upper() + text[1:] + ".", "steps": split_steps(text)}
