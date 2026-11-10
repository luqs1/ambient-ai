"""Triggers and moments.

A trigger is one plain sentence ("when someone asks me for a file") bound to any number of event
kinds from the universal catalog (events.py), plus what an agent should do about it. Nothing in
here knows about particular apps.

When an event of a bound kind arrives:
  1. deterministic match: is the event's signature one the trigger is bound to? A trigger written as a
     program (engine/triggers.py) also has field conditions over the event's wide record.
  2. meaning check (optional): a small on-device model decides whether the event matches the trigger's
     sentence. A program may instead run its check on Jev under zero data retention (engine/deciders.py),
     which is sent only the fields the program names; never a general-purpose cloud model.
  3. only if it passes, a *moment* is created (shown in the feed) and the trigger either offers
     its action as a button ("ask me first") or runs its agent right away ("automatic").
If the checking model is unavailable, a trigger that needs the meaning check does not fire (or, for Jev
with `zdr_fallback=device`, is checked on this device instead).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from fnmatch import fnmatchcase

from engine import deciders
from engine import triggers as programs
from engine.events import HTTP_SIG, wide_view
from engine.recognizers import CHAT_LABELS, Recognizers, WATCHABLE  # noqa: F401  (WATCHABLE re-exported)

SCHEMA = """
CREATE TABLE IF NOT EXISTS moments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, app TEXT, kind TEXT,          -- dm | mention | sent | group | meeting | event
  key TEXT UNIQUE,
  who TEXT, place TEXT, title TEXT, text TEXT, context TEXT,
  status TEXT DEFAULT 'new',             -- new | replied | done | dismissed
  action TEXT, action_status TEXT, action_result TEXT, action_ts REAL
);
CREATE INDEX IF NOT EXISTS moments_ts ON moments(ts);
CREATE TABLE IF NOT EXISTS names (app TEXT, id TEXT, name TEXT, extra TEXT, PRIMARY KEY (app, id));
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS triggers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, app TEXT, kind TEXT,        -- app/kind: pre-catalog triggers only (migrated to sigs)
  action TEXT,                           -- draft | ask
  instruction TEXT,                      -- what the agent should do, in the user's words
  auto INTEGER DEFAULT 0,                -- 1 = run as soon as it happens, 0 = offer a button
  enabled INTEGER DEFAULT 1, created REAL,
  condition TEXT,                        -- optional explicit statement for the local check
  threshold REAL DEFAULT 0.5
);
"""
MIGRATIONS = (("moments", "trigger_id", "INTEGER"), ("moments", "gate", "TEXT"), ("moments", "sig", "TEXT"),
              ("triggers", "condition", "TEXT"), ("triggers", "threshold", "REAL DEFAULT 0.5"),
              ("triggers", "text", "TEXT"),            # the "when…" sentence
              ("triggers", "sigs", "TEXT"),            # JSON list of event signatures / patterns
              ("triggers", "semantic", "INTEGER DEFAULT 0"),   # 1 = also check meaning locally
              ("triggers", "agent", "TEXT"),           # which agent runs it ('' = default)
              ("triggers", "checked", "INTEGER DEFAULT 0"), ("triggers", "matched", "INTEGER DEFAULT 0"),
              ("triggers", "program", "TEXT"),         # a trigger program (engine/triggers.py), JSON
              ("triggers", "check_on", "TEXT DEFAULT 'device'"))   # device | zdr: where the meaning check runs

NOTHING = "(nothing)"
PROMISE = "The writer promises to do something."
# Patterns that mean "this kind, in whichever chat app".
GROUPS = {f"*:{k}": {"label": v, "app": "your chat apps"} for k, v in CHAT_LABELS.items()}
GROUPS["*|VISIT"] = {"label": "I open a page", "app": "any website"}      # every site's "open a page" kind
GROUP_APPS = ("your chat apps", "any website")
STARTER_TRIGGERS = [
    dict(name="Draft a reply", text="someone DMs me", sigs=["*:dm"], action="draft", instruction=""),
    dict(name="What do they need?", text="someone @mentions me", sigs=["*:mention"], action="ask",
         instruction="What do they need from me, and what's the smallest next step? Two lines."),
    dict(name="Draft follow-ups", text="a meeting ends", sigs=["granola:meeting"], action="draft", instruction=""),
    dict(name="Catch my promises", text="I promise to do something in a message I send", sigs=["*:sent"],
         action="ask", auto=True, semantic=True, condition=PROMISE, threshold=0.5,
         instruction="If in this message I promised, agreed or offered to do something, write it as ONE todo "
                     "line: what, for whom, and by when if I said. If I didn't commit to anything, reply with "
                     f"exactly {NOTHING} and nothing else."),
]
TRIGGER_SEED_VERSION = "4"
AGENTS = {
    "claude": {"label": "Claude Code", "cmd": ["claude", "-p"], "local": False},
    "codex": {"label": "Codex", "cmd": ["codex", "exec", "-"], "local": False},
    "opencode": {"label": "opencode", "cmd": ["opencode", "run"], "local": False},
}
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
# Kinds the curated recognizers and local adapters can produce, offered before they've been seen once.
KNOWN_KINDS = {
    "granola:meeting": ("Granola", "a meeting ends", "in"),
    "slack:dm": ("Slack", "someone DMs me", "in"), "slack:mention": ("Slack", "someone @mentions me", "in"),
    "slack:sent": ("Slack", "I send a message", "out"),
    "discord:dm": ("Discord", "someone DMs me", "in"), "discord:mention": ("Discord", "someone @mentions me", "in"),
    "discord:sent": ("Discord", "I send a message", "out"),
    "whatsapp:dm": ("WhatsApp", "someone DMs me", "in"), "whatsapp:group": ("WhatsApp", "someone writes in a group chat", "in"),
    "whatsapp:sent": ("WhatsApp", "I send a message", "out"),
}
SOMEONE = re.compile(r"(someone|somebody|anyone|anybody|people|they|he|she|a (colleague|customer|client|friend|person|"
                     r"teammate|user|prospect|lead)|my \w+|the \w+ team|\w+ (asks|tells|messages|emails|pings|dms|texts))\b")
SPEECH = re.compile(r"\b(say|said|promise|tell|told|write|wrote|send|sent|reply|replied|agree|offer|mention|commit|"
                    r"ask|message|text|post)\w*\b")
VISITISH = re.compile(r"\b(open|opens|opened|visit|visits|view|views|look at|look up|read|reads|browse|land on|go to|"
                      r"search|searches)\b")
# The sentence is about something other than a chat message ("I send an email", "someone replies to my
# tweet", "someone comments on my issue"): the chat-app groups are then the wrong guess.
_MEDIUM = r"(e-?mails?|mails?|tweets?|posts?|comments?|issues?|tickets?|pull requests?|prs?|reviews?|commits?|invoices?|orders?|invites?)"
NONCHAT = re.compile(rf"^(\S+\s+)(\w+\s+){{0,2}}?(to\s+|on\s+)?(an?|my|the|our)\s+(new\s+)?{_MEDIUM}\b"
                     r"|^(\S+\s+){1,3}(e-?mails|tweets|comments|posts|commits)\b")
# Words that say only that something happened, not what it was about. What is left of a sentence
# after these (and the app's and the kind's own words) is its content: "…asks me for money".
FILLER = frozenset("""someone somebody anyone anybody people person they them she her his him you your our the and any for
from with into onto that this when whenever every each time once after about there are was were has have had will
send sends sent get gets got receive receives received message messages messaged dms dmed text texts texted chat
chats ping pings pinged write writes wrote written mention mentions mentioned reply replies replied group direct new
one something anything mine app apps channel channels thread threads post posts posted email emails emailed mail
note notes say says said tell tells told open opens opened visit visits visited view views page pages site sites
website websites look looks browse load add adds added create creates created make makes made save saves saved
start starts started set sets does did happen happens arrive arrives end ends ended""".split())
WRITE_FAMILIES = [{"create", "add", "new", "make", "save", "submit", "open", "start", "book", "schedule"},
                  {"update", "edit", "change", "rename", "move", "save", "set"}, {"delete", "remove", "archive", "close"},
                  {"send", "post", "reply", "comment", "share", "publish", "write", "message"},
                  {"star", "react", "vote", "like", "follow", "upvote", "favorite", "bookmark", "pin"},
                  {"push", "merge", "commit", "deploy", "release"}, {"search", "find", "lookup", "query"},
                  {"upload", "attach", "import"}, {"invite", "assign"}, {"pay", "buy", "order", "purchase", "checkout"}]


# Things you do with a click and no words (a star, a like, an archive): a kind with no text in it can
# be exactly that. Creating, editing and sending carry words, so there a wordless kind is a poor guess.
WORDLESS_VERBS = frozenset({"star", "react", "vote", "like", "follow", "upvote", "favorite", "bookmark", "pin", "delete",
                            "remove", "archive", "close", "push", "merge", "deploy", "release", "assign", "invite"})


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            if suf in ("ing", "ed") and len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
                w = w[:-1]                    # starred -> star, tagged -> tag
            return w
    return w


FILLER_STEMS = frozenset(_stem(w) for w in FILLER) | FILLER
CHAT_APPS = frozenset({"Slack", "Discord", "WhatsApp"})
NOT_APPS = frozenset("Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July "
                     "August September October November December I".split())
OLLAMA_MODEL = re.compile(r"[A-Za-z0-9][\w.\-/:]{0,79}")


def known_agent(choice) -> str:
    """An agent id we know how to run, or "". Triggers arrive over the local API: never run what they name
    as a command."""
    choice = choice if isinstance(choice, str) else ""
    if choice in AGENTS or (choice.startswith("ollama:") and OLLAMA_MODEL.fullmatch(choice[7:])):
        return choice
    return ""


WITH_FIELD = re.compile(r"\bwith (?:an? |any |some |at least one )?(attendees?|guests?|invitees?|recipients?|attachments?|"
                        r"reviewers?|assignees?|labels?|participants?)\b")
_SINGULAR = {"attendee": "attendees", "guest": "guests", "invitee": "invitees", "recipient": "recipients",
             "attachment": "attachments", "reviewer": "reviewers", "assignee": "assignees", "label": "labels",
             "participant": "participants"}


def unseen_apps(text: str, known: list[str]) -> list[str]:
    """Apps a sentence names ("…on GitHub", "in Linear") that the catalog has not seen yet: a program can
    still say "only in this app", and it starts matching the first time that app does something."""
    out = []
    for nm in re.findall(r"\b(?:on|in|via|from|through|using)\s+([A-Z][A-Za-z0-9.]+(?:\s[A-Z][A-Za-z0-9.]+)?)", text or ""):
        if nm.split()[0] in NOT_APPS or nm.lower() in {k.lower() for k in known}:
            continue
        out.append(nm)
    return list(dict.fromkeys(out))[:3]


def derive_statement(text: str) -> str:
    """'When someone asks me for a file.' -> 'someone asks me for a file'"""
    s = (text or "").strip().rstrip(".!?")
    s = re.sub(r"^(when(ever)?|if|every time|each time|as soon as|once|after)\s+", "", s, flags=re.I)
    return s


class Moments:
    def __init__(self, store, events=None):
        self.store, self.events = store, events
        with store.lock:
            store.db.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r[1] for r in store.db.execute(f"PRAGMA table_info({table})")}:
                    store.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            store.db.commit()
        self.cond = threading.Condition()
        self.notes: list[dict] = []                 # notification feed for the menu-bar app
        self.note_seq = 0
        self.checks: deque = deque(maxlen=300)      # recent local meaning checks (shown in Activity)
        self._tcache: list[dict] | None = None
        self._emb: dict[str, object] = {}
        self.work: queue.Queue = queue.Queue(maxsize=500)
        self.rec = Recognizers(store, events, self.setting, self.set_setting, self.mark_replied) if events else None
        self._seed_triggers()
        if events is not None:
            events.on_event = self.on_event
            events.muted_apps = {r["k"][6:] for r in store.q("SELECT k FROM settings WHERE k LIKE 'watch:%' AND v='0'")}
            events.keep = self.bound_sig        # pruning the catalog never forgets a kind a trigger is bound to
            events.on_refold = self.rebind      # a kind merged into a wider one takes its triggers with it
            self.rebind()
        threading.Thread(target=self._worker, daemon=True).start()
        if any(t["semantic"] or t["condition"] for t in self._enabled()):
            threading.Thread(target=lambda: __import__("engine.decide").decide.judge(), daemon=True).start()

    # ---------- settings ----------
    def setting(self, k: str, default: str | None = None) -> str | None:
        r = self.store.q("SELECT v FROM settings WHERE k=?", (k,))
        return r[0]["v"] if r else default

    def set_setting(self, k: str, v: str):
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (k, v))
            self.store.db.commit()

    def watching(self, app: str) -> bool:
        return self.setting(f"watch:{app}", "1") == "1"

    def set_watch(self, app: str, on: bool):
        self.set_setting(f"watch:{app}", "1" if on else "0")
        if self.events is not None:
            (self.events.muted_apps.discard if on else self.events.muted_apps.add)(app)

    def mark_replied(self, app: str, channel: str):
        with self.store.lock:
            self.store.db.execute("UPDATE moments SET status='replied' WHERE app=? AND status='new' "
                                  "AND json_extract(context,'$.channel')=?", (app, channel))
            self.store.db.commit()

    # ---------- events -> moments ----------
    @staticmethod
    def _bound(t: dict, ev: dict) -> bool:
        """Is this trigger bound to this event's kind (by its signature, or its exact path before
        name positions were generalised)? A trigger with a program must also pass its match."""
        sigs = [ev["sig"]] + ([ev["sig0"]] if ev.get("sig0") else [])
        if not any(fnmatchcase(sg, p) for sg in sigs for p in t["_sigs"]):
            return False
        prog = t.get("_program")
        if prog is None:
            return True
        w = ev.get("wide") or wide_view(ev)
        if ev.get("sig0"):
            w = {**w, "kind0": ev["sig0"]}
        return programs.matches(prog, w)

    def rebind(self):
        """Move triggers bound to an exact path ("star luqs1/ambient-ai") to the kind it was merged into once
        the path's name positions were learned ("star any repository"), and tell the event layer which
        path groups triggers are watching so it learns those from the second example."""
        if self.events is None:
            return
        groups, changed = {}, False
        for r in self.store.q("SELECT id, sigs FROM triggers"):
            try:
                sigs = [x for x in json.loads(r["sigs"] or "[]") if isinstance(x, str)]
            except Exception:
                continue
            new = list(dict.fromkeys(self.events.folded(x) if "|" in x and "*" not in x else x for x in sigs))
            for x in new:
                g = self.events.group_of(x) if "*" not in x else None
                if g:
                    groups.setdefault(g, set()).add(tuple(HTTP_SIG.match(x)["path"].split("/")))
            if new != sigs:
                with self.store.lock:
                    self.store.db.execute("UPDATE triggers SET sigs=? WHERE id=?", (json.dumps(new), r["id"]))
                    self.store.db.commit()
                changed = True
        self.events.eager_groups = groups
        if changed:
            self._tcache = None

    def bound_sig(self, sig: str) -> bool:
        """Is any trigger, enabled or not, bound to this kind? (Events.keep: such kinds are never pruned.)"""
        cache = self._enabled()                         # replaced whenever a trigger is saved or deleted
        if getattr(self, "_bound_for", None) is not cache:
            pats: list[str] = []
            for r in self.store.q("SELECT sigs FROM triggers"):
                try:
                    pats += [p for p in json.loads(r["sigs"] or "[]") if isinstance(p, str) and p != "*"]
                except Exception:
                    pass
            self._bound_pats, self._bound_for = pats, cache
        return any(fnmatchcase(sig, p) for p in self._bound_pats)

    def on_event(self, ev: dict):
        if any(self._bound(t, ev) for t in self._enabled()):
            try:
                self.work.put_nowait(ev)
            except queue.Full:
                print("ambient moments: busy, dropped an event", flush=True)

    def _worker(self):
        while True:
            ev = self.work.get()
            try:
                self._handle(ev)
            except Exception as e:
                print(f"ambient moments: {e!r}", flush=True)

    @staticmethod
    def _kind(sig: str) -> str:
        return sig.split(":", 1)[1] if ":" in sig and "|" not in sig else "event"

    def gate_context(self, ev: dict) -> tuple[str, str]:
        """(a short description of what kind of thing this is, the text to judge)"""
        kind, app = self._kind(ev["sig"]), ev["app"]
        text = ev.get("text") or ""
        if kind == "meeting":
            return "Transcript of a meeting", (ev.get("context") or {}).get("transcript", "")[-3000:] or text
        if ev["sig"].endswith("|VISIT"):
            url = (ev.get("fields") or {}).get("url", "")
            return f"Page opened in {app}", f"{text} ({url})" if url else text
        if kind in ("dm", "mention", "group"):
            return f"Message received in {app}" + (f" from {ev['who']}" if ev.get("who") else ""), text
        if kind == "sent":
            return f"Message I sent in {app}", text
        if ev.get("direction") == "in" and "|LOCAL " not in ev["sig"]:      # a row in an app's own store may be
            return f"{ev.get('label', 'message')} received in {app}", text   # something the user wrote: no "received"
        return f"{ev.get('label', 'action')} in {app}", text

    def judge(self, ev: dict, t: dict) -> float | None:
        """P(this event is what the trigger's sentence describes), from the local model. None = can't tell."""
        from engine import decide
        ctx, text = self.gate_context(ev)
        if not text.strip():
            return None
        text = text[:1500]
        if t.get("condition"):                       # an explicit statement, judged against the raw text
            return decide.prob(text, t["condition"])
        hyp = derive_statement(t.get("text") or "")
        if not hyp:
            return None
        ps = [decide.prob(f'{ctx}: "{text}"', f"In this case, {hyp}."), decide.prob(text, f"This example is: {hyp}.")]
        if any(p is None for p in ps):
            return None
        return sum(ps) / len(ps)

    def _check_programs(self, ev: dict, ts: list[dict]) -> dict:
        """Run the checks of program triggers on this event: {trigger id: Judgment}. Checks that run on Jev
        go in one request; if it can't be reached they fail closed, or run here when `zdr_fallback` says so."""
        w = ev.get("wide") or wide_view(ev)
        local, zdr = [], []
        for t in ts:
            (zdr if t["_program"]["check"]["run"] == "zdr" else local).append(t)
        out = {}
        for t in local:
            out[t["id"]] = deciders.Local().judge(programs.check_state(t["_program"], w), t["_program"]["check"]["condition"])
        if zdr:
            paused = self.events is not None and ev.get("app") in self.events.muted_apps
            items = [({**programs.check_state(t["_program"], w), **({"paused": True} if paused else {})},
                      t["_program"]["check"]["condition"]) for t in zdr]
            res = deciders.Jev(self.setting).judge_many(items)
            fallback = self.setting("zdr_fallback", "closed") == "device"
            for t, j, (state, cond) in zip(zdr, res, items):
                if j.p is None and fallback and not paused:
                    lj = deciders.Local().judge(state, cond)
                    lj.by = f"{lj.by} (Jev unreachable)"
                    lj.error = j.error
                    j = lj
                out[t["id"]] = j
        return out

    def _handle(self, ev: dict):
        ts = [t for t in self._enabled() if self._bound(t, ev)]
        gates, passing = [], []
        judged = self._check_programs(ev, [t for t in ts if t.get("_program") and t["_program"]["check"]])
        for t in ts:
            if t["id"] in judged:
                j = judged[t["id"]]
                ck = t["_program"]["check"]
                passed = j.p is not None and j.p >= ck["threshold"]
                p = None if j.p is None else round(j.p, 3)
                gates.append({"trigger": t["id"], "name": t["name"], "p": p, "passed": passed, "local": j.run == "device",
                              "run": j.run, "by": j.by, "reason": j.reason, "error": j.error})
                self.checks.appendleft({"ts": time.time(), "app": ev["app"], "sig": ev["sig"],
                                        "label": ev.get("label"), "text": (ev.get("text") or "")[:140],
                                        "trigger": t["id"], "name": t["name"], "p": p, "passed": passed,
                                        "run": j.run, "by": j.by, "reason": j.reason, "error": j.error})
                with self.store.lock:
                    self.store.db.execute("UPDATE triggers SET checked=COALESCE(checked,0)+1, "
                                          "matched=COALESCE(matched,0)+? WHERE id=?", (1 if passed else 0, t["id"]))
                    self.store.db.commit()
            elif t.get("_program") is not None:
                passed = True
            elif t["semantic"] or t["condition"]:
                p = self.judge(ev, t)
                passed = p is not None and p >= (t["threshold"] or 0.5)
                gates.append({"trigger": t["id"], "name": t["name"], "p": None if p is None else round(p, 3),
                              "passed": passed, "local": True})
                self.checks.appendleft({"ts": time.time(), "app": ev["app"], "sig": ev["sig"],
                                        "label": ev.get("label"), "text": (ev.get("text") or "")[:140],
                                        "trigger": t["id"], "name": t["name"],
                                        "p": None if p is None else round(p, 3), "passed": passed})
                with self.store.lock:
                    self.store.db.execute("UPDATE triggers SET checked=COALESCE(checked,0)+1, "
                                          "matched=COALESCE(matched,0)+? WHERE id=?", (1 if passed else 0, t["id"]))
                    self.store.db.commit()
            else:
                passed = True
            if passed:
                passing.append(t)
        if not passing:
            return                                  # checked locally, nothing matched: no moment, nothing sent
        kind = self._kind(ev["sig"])
        context = dict(ev.get("context") or {})
        if ev.get("fields"):
            context["fields"] = ev["fields"]
        mid = self.add(app=ev["app"], kind=kind, sig=ev["sig"], key=f"ev:{ev['id']}", who=ev.get("who"),
                       place=ev.get("place"), title=ev.get("label") or kind, text=ev.get("text") or "",
                       context=context, status="done" if ev.get("direction") == "out" else "new",
                       gate=gates or None)
        if not mid:
            return
        # Every automatic trigger that passed runs: they are separate agents with separate instructions.
        # A moment holds one action and its result, so the first runs on this moment and each further one
        # gets a moment of its own for the same event (already "done": it records a run, it is not a
        # second thing waiting for the user). The key makes a redelivered event a no-op here too.
        for i, t in enumerate(t for t in passing if t["auto"]):
            target = mid if i == 0 else self.add(
                app=ev["app"], kind=kind, sig=ev["sig"], key=f"ev:{ev['id']}:t{t['id']}", who=ev.get("who"),
                place=ev.get("place"), title=ev.get("label") or kind, text=ev.get("text") or "", context=context,
                status="done", gate=gates or None)
            if target:
                self.run_trigger(target, t["id"])
        offer = [t for t in passing if not t["auto"]]
        if offer and ev.get("direction") != "out":
            who = ev.get("who")
            title = (f"{who} messaged you" if kind in ("dm", "group") and who else
                     f"{who} mentioned you" if kind == "mention" and who else
                     "Meeting ended" if kind == "meeting" else (ev.get("label") or "Something happened"))
            self._note(f"{title} · {ev['app']}", f"{(ev.get('text') or '')[:140]}\n→ {offer[0]['name']}", mid, "offer")

    def add(self, *, app, kind, key, who, place, title, text, context, status="new", sig=None, gate=None) -> int | None:
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT OR IGNORE INTO moments (ts, app, kind, sig, key, who, place, title, text, context, status, gate) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), app, kind, sig, key, who, place, title, text, json.dumps(context), status,
                 json.dumps(gate) if gate else None))
            self.store.db.commit()
            mid = cur.lastrowid if cur.rowcount else None
        if mid:
            with self.cond:
                self.cond.notify_all()
        return mid

    # ---------- notifications (read by the menu-bar app) ----------
    def _note(self, title: str, body: str, mid: int, kind: str):
        with self.cond:
            self.note_seq += 1
            self.notes.append({"seq": self.note_seq, "title": title, "body": body[:240], "moment": mid,
                               "kind": kind, "ts": time.time()})
            del self.notes[:-100]

    def notes_after(self, seq: int) -> dict:
        with self.cond:
            return {"seq": self.note_seq, "notes": [n for n in self.notes if n["seq"] > seq]}

    def status(self) -> dict:
        q = self.store.q
        return {
            "waiting": q("SELECT COUNT(*) n FROM moments WHERE status='new' AND ts > ?",
                         (time.time() - 86400,))[0]["n"],
            "running": q("SELECT COUNT(*) n FROM moments WHERE action_status='running'")[0]["n"],
            "armed": [t["name"] for t in q("SELECT name FROM triggers WHERE enabled=1 AND auto=1")],
            "active": q("SELECT COUNT(*) n FROM triggers WHERE enabled=1")[0]["n"],
        }

    # ---------- triggers ----------
    def _seed_triggers(self):
        seeded = self.setting("triggers_seeded")
        if seeded == TRIGGER_SEED_VERSION:
            return
        if seeded is None and not self.store.q("SELECT 1 FROM triggers LIMIT 1"):
            for t in STARTER_TRIGGERS:
                self.save_trigger(t)
        else:
            # Older triggers were bound to (app, kind) presets. Rebind them to catalog signatures.
            labels = {**CHAT_LABELS, "meeting": "a meeting ends"}
            for t in self.store.q("SELECT * FROM triggers WHERE sigs IS NULL"):
                app, kind = t["app"] or "*", t["kind"] or ""
                sig = f"*:{kind}" if app == "*" else f"{app.lower()}:{kind}"
                text = labels.get(kind, kind)
                if t["condition"] == PROMISE:
                    text = "I promise to do something in a message I send"
                self.store.update_table("triggers", t["id"], sigs=json.dumps([sig]), text=text,
                                        semantic=1 if t["condition"] else 0)
        self._tcache = None
        self.set_setting("triggers_seeded", TRIGGER_SEED_VERSION)

    def _enabled(self) -> list[dict]:
        if self._tcache is None:
            rows = self.store.q("SELECT * FROM triggers WHERE enabled=1 ORDER BY id")
            for t in rows:
                try:
                    t["_sigs"] = [s for s in json.loads(t["sigs"] or "[]") if isinstance(s, str)]
                except Exception:
                    t["_sigs"] = []
                t["_program"] = None
                if t.get("program"):
                    prog = programs.normalize(t["program"])
                    # the kinds live in `sigs` (which rebind() keeps folded); the program carries the rest
                    prog["match"]["kinds"] = [s for s in t["_sigs"] if s != "*"]
                    t["_program"] = prog
            self._tcache = rows
        return self._tcache

    def describe_sig(self, sig: str) -> dict:
        if sig in GROUPS:
            return {"sig": sig, **GROUPS[sig]}
        t = self.events.type(sig) if self.events else None
        if t:
            return {"sig": sig, "label": t["label"], "app": t["app"], "direction": t["direction"],
                    "source": t.get("source")}
        if ":" in sig and "|" not in sig:
            app, kind = sig.split(":", 1)
            return {"sig": sig, "label": {**CHAT_LABELS, "meeting": "a meeting ends"}.get(kind, kind),
                    "app": app.capitalize()}
        # A kind not in the catalog (yet): make its signature readable.
        from engine.events import words
        app, _, rest = sig.partition("|")
        rest = rest.removesuffix("+text")
        op = rest.split("#", 1)[1] if "#" in rest else ""
        label = words(op) or words(" ".join(rest.split(" ", 1)[-1].split("/")[-2:])) or rest[:60]
        return {"sig": sig, "label": label, "app": app}

    def triggers(self) -> list[dict]:
        rows = self.store.q("SELECT * FROM triggers ORDER BY id")
        stats = {r["trigger_id"]: r for r in self.store.q(
            "SELECT trigger_id, COUNT(*) n, MAX(action_ts) last FROM moments "
            "WHERE trigger_id IS NOT NULL GROUP BY trigger_id")}
        for t in rows:
            st = stats.get(t["id"]) or {}
            t["fired"], t["last_fired"] = st.get("n", 0), st.get("last")
            try:
                t["sigs"] = json.loads(t["sigs"] or "[]")
            except Exception:
                t["sigs"] = []
            t["kinds"] = [self.describe_sig(s) for s in t["sigs"] if s != "*"]
            t["statement"] = t["condition"] or (derive_statement(t["text"] or "") if t["semantic"] else None)
            t["check_on"] = t.get("check_on") or "device"
            if t.get("program"):
                prog = programs.normalize(t["program"])
                prog["match"]["kinds"] = [s for s in t["sigs"] if s != "*"]
                prog["explain"] = programs.explain(prog)
                t["program"] = prog
                t["explain"] = prog["explain"]
                t["statement"] = prog["check"]["condition"] if prog["check"] else None
                t["check_on"] = prog["check"]["run"] if prog["check"] else t["check_on"]
            else:
                t["program"] = None
                t["explain"] = None
            t["checked_by"] = deciders.LABEL.get(t["check_on"], deciders.LABEL["device"]) if t["statement"] else None
        return rows

    def _program_of(self, t: dict) -> dict | None:
        """The program a saved or draft trigger runs: its own, or (for a sentence trigger whose check runs on
        Jev) one built from its kinds and sentence. None = a plain sentence trigger checked on this device."""
        run = t.get("check_on") if t.get("check_on") in programs.RUN else None
        if t.get("program"):
            prog = programs.normalize(t["program"])
            if prog["check"] and run:
                prog["check"]["run"] = run
            if t.get("sigs") and not prog["match"]["kinds"]:
                prog["match"]["kinds"] = [s for s in t["sigs"] if isinstance(s, str) and s != "*"][:40]
            prog["explain"] = programs.explain(prog)
            return prog
        semantic = bool(t.get("semantic") or t.get("condition"))
        if run == "zdr" and semantic:
            cond = (t.get("condition") or "").strip() or derive_statement(t.get("text") or "")
            if not cond:
                return None
            return programs.normalize({"when": t.get("text") or "", "match": {"kinds": t.get("sigs") or []},
                                       "check": {"condition": cond, "threshold": t.get("threshold") or 0.5, "run": "zdr"},
                                       "do": {"action": t.get("action"), "instruction": t.get("instruction") or ""}})
        return None

    def save_trigger(self, t: dict) -> dict:
        if t.get("id") and "program" not in t:          # an older client re-saving (toggling): keep the program
            old = self.store.q("SELECT program, check_on FROM triggers WHERE id=?", (int(t["id"]),))
            if old and old[0]["program"]:
                prog_old = programs.normalize(old[0]["program"])
                t = {**t, "program": prog_old, "check_on": t.get("check_on") or old[0]["check_on"]}
                if t.get("sigs"):
                    prog_old["match"]["kinds"] = [s for s in t["sigs"] if isinstance(s, str) and s != "*"]
        prog = self._program_of(t)
        if prog is not None:
            if programs.empty(prog):
                return {"error": "this would fire on everything: say which events to watch"}
            t = {**t, "sigs": prog["match"]["kinds"] or ["*"], "text": t.get("text") or prog["when"],
                 "instruction": t.get("instruction") or prog["do"]["instruction"],
                 "action": t.get("action") or prog["do"]["action"], "condition": None,
                 "semantic": bool(prog["check"]),
                 "threshold": prog["check"]["threshold"] if prog["check"] else t.get("threshold")}
            if not prog["do"]["instruction"] and t["instruction"]:
                prog["do"] = {**prog["do"], "instruction": t["instruction"], "steps": programs.split_steps(t["instruction"])}
                prog["explain"] = programs.explain(prog)
        sigs = t.get("sigs")
        if not sigs and t.get("kind"):                # pre-catalog shape
            app = t.get("app") or "*"
            sigs = [f"*:{t['kind']}" if app == "*" else f"{app.lower()}:{t['kind']}"]
        sigs = [s for s in (sigs or []) if isinstance(s, str) and s][:40]
        if not sigs:
            return {"error": "pick at least one kind of event"}
        text = (t.get("text") or "").strip()[:300]
        action = t.get("action") if t.get("action") in ("draft", "ask") else "ask"
        instr = (t.get("instruction") or "").strip()[:2000]
        if action == "ask" and not instr:
            return {"error": "say what your agent should do"}
        name = (t.get("name") or "").strip()[:80] or (
            re.split(r"[.\n]", instr)[0][:60] if action == "ask" else "Draft a reply")
        cond = (t.get("condition") or "").strip()[:300] or None
        try:
            thr = min(max(float(t.get("threshold") or 0.5), 0.05), 0.95)
        except (TypeError, ValueError):
            thr = 0.5
        row = dict(name=name, text=text, sigs=json.dumps(sigs), action=action, instruction=instr,
                   auto=1 if t.get("auto") else 0, enabled=0 if t.get("enabled") in (False, 0) else 1,
                   condition=cond, threshold=thr, semantic=1 if (t.get("semantic") or cond) else 0,
                   agent=known_agent(t.get("agent")),
                   program=json.dumps(prog, ensure_ascii=False) if prog is not None else None,
                   check_on=(prog["check"]["run"] if prog is not None and prog["check"] else "device"))
        with self.store.lock:
            if t.get("id"):
                sets = ", ".join(f"{k}=?" for k in row)
                self.store.db.execute(f"UPDATE triggers SET {sets} WHERE id=?", (*row.values(), int(t["id"])))
                tid = int(t["id"])
            else:
                cols = ", ".join(row)
                cur = self.store.db.execute(
                    f"INSERT INTO triggers ({cols}, created) VALUES ({', '.join('?' * len(row))}, ?)",
                    (*row.values(), time.time()))
                tid = cur.lastrowid
            self.store.db.commit()
        self._tcache = None
        self.rebind()
        out = {"ok": True, "id": tid}
        if row["check_on"] == "zdr" and not deciders.Jev(self.setting).available():
            out["warning"] = "No TypeSafe key is set up: this trigger's checks will fail closed until one is."
        return out

    def delete_trigger(self, tid: int):
        with self.store.lock:
            self.store.db.execute("DELETE FROM triggers WHERE id=?", (tid,))
            self.store.db.commit()
        self._tcache = None

    # ---------- writing a trigger: suggestions and previews, all local ----------
    def _embed(self, texts: list[str]):
        from engine import decide
        e = decide.embedder()
        if e is None:
            return None
        missing = [t for t in dict.fromkeys(texts) if t not in self._emb]
        for i in range(0, len(missing), 32):
            chunk = missing[i:i + 32]
            for t, v in zip(chunk, e.embed(chunk)):
                self._emb[t] = v
        return [self._emb[t] for t in texts]

    def suggest(self, text: str, limit: int = 8, ranked: bool = False) -> dict:
        """Which kinds of event does this sentence most likely mean? Ranked by a local embedding model."""
        q = derive_statement(text)
        if not q or self.events is None:
            return {"kinds": []}
        self.events.flush()
        cands = [{"sig": s, "label": g["label"], "app": g["app"], "direction": "in" if s[2:] in ("dm", "mention", "group") else "out",
                  "klass": "group", "n": None, "example": None,
                  "doc": ("I open a page on any website" if s == "*|VISIT" else
                          f"{g['label']} in a chat app: Slack, Discord or WhatsApp")}
                 for s, g in GROUPS.items()]
        have = set(self.events.types)
        for sig, (app, label, direction) in KNOWN_KINDS.items():       # kinds we can recognise but haven't seen yet
            if sig not in have:
                cands.append({"sig": sig, "label": label, "app": app, "direction": direction, "klass": "known",
                              "n": 0, "example": None, "doc": f"{label} in {app}"})
        types = [t for t in self.events.types.values() if t["klass"] in ("action", "incoming", "signal")]
        types.sort(key=lambda t: -(t["last_ts"] or 0))
        for t in types[:500]:
            if t["curated"]:
                doc = f"{t['label']} in {t['app']}"
            else:   # say who did it, so "I send…" and "someone sends…" land on different kinds
                doc = f"I {t['label']} in {t['app']}" if t["direction"] == "out" else f"{t['label']} arrives in {t['app']}"
            cands.append({"sig": t["sig"], "label": t["label"], "app": t["app"], "direction": t["direction"],
                          "klass": t["klass"], "n": t["n"], "example": t["example"], "doc": doc,
                          "source": t.get("source"), "last_ts": t["last_ts"]})
        vecs = self._embed([QUERY_PREFIX + q] + [c["doc"] for c in cands])
        ql = q.lower()
        qtok = set(re.findall(r"[a-z]{3,}", ql))
        qstems = {_stem(w) for w in qtok}
        receiving = bool(re.match(r"(i|we)\s+(get|got|receive|received|am sent|have received)\b", ql))
        mine = bool(re.match(r"(i|i'm|i've|we)\b", ql)) and not receiving      # "I get a message" is theirs
        theirs = bool(SOMEONE.match(ql)) or receiving
        speech = bool(SPEECH.search(ql))
        visitish = bool(VISITISH.search(ql))
        nonchat = bool(NONCHAT.search(ql))
        # What the sentence says I do ("I star…", "I create…"): a kind that names a different sort of
        # action ("repository create") is then a worse guess, however many other words it shares.
        m = re.match(r"(?:i|we)\s+(?:just\s+|have\s+|\w+ly\s+)?([a-z]+)", ql)
        verb = _stem(m.group(1)) if (mine and m) else ""
        vfam = set().union(*[f for f in WRITE_FAMILIES if verb in f]) if verb else set()
        names: dict[str, bool] = {}
        qwords = re.findall(r"[a-z0-9]+", ql)
        qjoined = set(qwords) | {a + b for a, b in zip(qwords, qwords[1:])}

        def named(al: str) -> bool:
            """Does the sentence name this app? Whole words only (the app "X" is not in "fix"), allowing
            for a space more or less ("whats app", "hackernews")."""
            if al not in names:
                squashed = al.replace(" ", "")
                names[al] = bool(re.search(rf"(?<![a-z0-9]){re.escape(al)}(?![a-z0-9])", ql)
                                 or (len(squashed) >= 4 and squashed in qjoined))
            return names[al]

        def app_named(c) -> bool:
            al = c["app"].lower()
            return named(al) or any(_stem(w) in qstems for w in re.findall(r"[a-z]{4,}", al)
                                    if w not in ("chat", "search", "page"))

        named_app = any(named(c["app"].lower()) for c in cands if c["app"] not in GROUP_APPS)
        # "…on Telegram", "…in Linear": the sentence names an app. If it is one we have never seen, no kind
        # we know is what was meant; better to tick nothing and offer "show me once" than to guess.
        unknown_app = False
        for nm in re.findall(r"\b(?:on|in|via|from|through|using)\s+([A-Z][A-Za-z0-9.]+(?:\s[A-Z][A-Za-z0-9.]+)?)", q):
            toks = {_stem(w) for w in re.findall(r"[a-z0-9]{2,}", nm.lower())}
            if nm.split()[0] in NOT_APPS or not toks:
                continue
            if not any(toks <= {_stem(w) for w in re.findall(r"[a-z0-9]{2,}", c["app"].lower())}
                       for c in cands if c["app"] not in GROUP_APPS):
                unknown_app = True
        for i, c in enumerate(cands):
            cos = float(vecs[0] @ vecs[i + 1]) if vecs is not None else 0.0
            ltok = set(re.findall(r"[a-z]{3,}", c["label"].lower()))
            lstems = {_stem(w) for w in ltok}
            bonus = 0.06 * (len(qstems & lstems) / max(len(lstems), 1))
            site_visit = c["sig"].endswith("|VISIT") and c["klass"] != "group"
            if c["klass"] != "group" and app_named(c):
                bonus += 0.08
            chatty = self._kind(c["sig"]) in ("dm", "mention", "group", "sent")
            wrong = False
            if c["klass"] == "signal" and not (verb in WORDLESS_VERBS and verb in lstems):
                bonus -= 0.08                       # no text in these: rarely what a sentence means
            if (mine and c["direction"] == "in") or (theirs and c["direction"] == "out"):
                bonus -= 0.07
                if c["klass"] != "group" and "|LOCAL " not in c["sig"]:     # (a row you wrote in a local store is "in")
                    wrong = True                # "I edit a message" is not "a message arrives"
            if c["sig"].endswith("|VISIT") and not visitish:
                bonus -= 0.12                       # "I create an event" is not "I open a page"
                wrong = True
            if vfam and c["direction"] == "out" and not c["sig"].endswith("|VISIT") and c["klass"] != "group":
                lfam = [f for f in WRITE_FAMILIES if lstems & f]
                if verb in lstems:
                    bonus += 0.05
                elif lfam and not any(f & vfam for f in lfam):
                    bonus -= 0.12
                    wrong = True
            elif verb and not vfam and c["direction"] == "out" and not c["sig"].endswith("|VISIT") and c["klass"] != "group":
                # a verb we have no family for ("I unstar…", "I join…"): a kind that plainly names some
                # other action ("repository create", "I send a message") is not it
                if verb not in lstems and any(lstems & f for f in WRITE_FAMILIES):
                    bonus -= 0.12
                    wrong = True
            # A sentence about what a message *says* ("someone asks me for…", "I promise…") means chat
            # messages in general; the local meaning check then does the rest.
            if c["klass"] == "group" and not named_app:
                if c["sig"] == "*|VISIT":
                    bonus += 0.12 if (visitish and mine) else 0
                elif nonchat:
                    bonus -= 0.1
                    wrong = True
                elif theirs and c["sig"] in ("*:dm", "*:mention"):
                    bonus += 0.14
                elif theirs and c["sig"] == "*:group":
                    bonus += 0.08
                elif mine and speech and c["sig"] == "*:sent":
                    bonus += 0.16
            if unknown_app and c["sig"] != "*|VISIT" and not (c["klass"] != "group" and app_named(c)):
                wrong = True
            if nonchat and (chatty or c["app"] in CHAT_APPS) and c["klass"] != "group" and not app_named(c):
                bonus -= 0.1                        # "I send an email" is not a Slack message
                wrong = True
            c["score"] = round(cos + bonus, 3)
            c["_site_visit"], c["_named"], c["_wrong"] = site_visit, c["klass"] != "group" and app_named(c), wrong
            c["_guess"] = not (c["klass"] in ("group", "known") or c["_named"] or (self.events.type(c["sig"]) or {}).get("curated"))
            c.pop("doc")
        cands.sort(key=lambda c: -c["score"])
        ranking = [{k: c[k] for k in ("sig", "label", "app", "direction", "klass", "score")} | {"wrong": c["_wrong"]}
                   for c in cands[:30]] if ranked else None
        best = cands[0]["score"] if cands else 0
        top = [c for c in cands[:limit] if c["score"] >= best - 0.1][:6]
        # How sure the top guess must be before we tick it: a chat group for a "someone…" sentence is a
        # safe default (the meaning check narrows it); a kind in an app the sentence never names is not.
        need = 0.64 if (top and top[0]["klass"] == "group" and (theirs or speech or visitish)) else \
            0.78 if (top and top[0]["_guess"]) else 0.72
        for i, c in enumerate(top):
            c["picked"] = bool(best >= need and not c["_wrong"]
                               and (i == 0 or (c["score"] >= best - 0.02 and c["klass"] != "signal")))
            # One site's "open a page" is only a guess we tick when the sentence names that site; otherwise
            # "any website" (with the meaning check) is what was meant.
            if c["_site_visit"] and not c["_named"]:
                c["picked"] = False
            if i and top[0]["_named"] and not c["_named"]:      # the sentence named an app: stay in it
                c["picked"] = False
        if theirs and not nonchat and not unknown_app and top and top[0]["klass"] == "group" and top[0]["sig"] != "*|VISIT":
            for c in top:
                if c["sig"] in ("*:dm", "*:mention", "*:group"):
                    c["picked"] = True
        picked = [c for c in top if c["picked"]]
        # Is the sentence about what the content says, rather than just that something happened? What is
        # left of it after the words for "a message arrived", the app's name and the kind's own label.
        used = set()
        for c in picked:
            used |= {_stem(w) for w in re.findall(r"[a-z]{3,}", (c["label"] + " " + c["app"]).lower())}
        rest = {w for w in qstems if w not in FILLER_STEMS and w not in used}

        def carries_words(c) -> bool:
            kind = self._kind(c["sig"])
            return (c["klass"] in ("group", "incoming") or kind in ("dm", "mention", "group", "sent")
                    or c["sig"].endswith("|VISIT") or (c["direction"] == "out" and speech and c["klass"] == "action"))

        content = bool(rest and any(carries_words(c) for c in picked))
        for c in top:
            for k in ("_site_visit", "_named", "_wrong", "_guess"):
                c.pop(k, None)
        out = {"kinds": top, "statement": q, "model": "local" if vecs is not None else "unavailable",
               "sure": bool(picked) and vecs is not None, "about_content": content}
        if ranked:
            out["ranked"], out["mine"], out["theirs"], out["named_apps"] = ranking, mine, theirs, sorted(
                {c["app"] for c in cands if c["klass"] not in ("group",) and c["app"] not in GROUP_APPS
                 and named(c["app"].lower())})
            out["content_words"] = sorted({w for w in qstems if w not in FILLER_STEMS} - {
                _stem(w) for c in cands if c["app"] not in GROUP_APPS and named(c["app"].lower())
                for w in re.findall(r"[a-z]{3,}", c["app"].lower())})
        return out

    ZDR_PREVIEW_MAX = 20            # a backtest on Jev sends at most this many recent events, in one request

    def preview_program(self, prog: dict, limit: int = 60) -> dict:
        """Backtest a program over stored events: its match on every recent event (window of 3000), then its
        check on the ones that pass, where the check says it runs."""
        if self.events is None or programs.empty(prog):
            return {"checked": 0, "matched": 0, "samples": [], "program": prog, "explain": prog["explain"]}
        ck = prog["check"]
        rows = self.events.scan(lambda w: programs.matches(prog, w), limit=limit)
        js: list = [None] * len(rows)
        by = None
        if ck:
            muted = self.events.muted_apps
            states = [programs.check_state(prog, r["wide"]) for r in rows]
            if ck["run"] == "zdr":
                idx = [i for i, r in enumerate(rows) if r["app"] not in muted][:self.ZDR_PREVIEW_MAX]
                rows = [rows[i] for i in idx]
                states = [states[i] for i in idx]
                js = deciders.Jev(self.setting).judge_batch(states, ck["condition"])
                if js and all(j.p is None for j in js) and self.setting("zdr_fallback", "closed") == "device":
                    js = [deciders.Local().judge(st, ck["condition"]) for st in states]
            else:
                js = [deciders.Local().judge(st, ck["condition"]) for st in states]
            by = js[0].by if js else deciders.LABEL[ck["run"]]
        out, matched = [], 0
        for r, j in zip(rows, js):
            passed = True if j is None else (j.p is not None and j.p >= ck["threshold"])
            matched += passed
            out.append({"ts": r["ts"], "app": r["app"], "who": r.get("who"), "label": r.get("label"),
                        "text": (r.get("text") or r["wide"].get("title") or "")[:160],
                        "p": None if j is None or j.p is None else round(j.p, 3), "passed": passed,
                        "reason": None if j is None else j.reason, "id": r["id"], "kind": r["sig"]})
        yes = [x for x in out if x["passed"]][:6]
        no = sorted([x for x in out if not x["passed"]], key=lambda x: -(x["p"] or 0))[:4]
        span = (rows[0]["ts"] - rows[-1]["ts"]) if len(rows) > 1 else 0
        return {"checked": len(out), "matched": matched, "semantic": bool(ck), "span_hours": round(span / 3600, 1),
                "samples": yes + no, "program": prog, "explain": prog["explain"], "by": by,
                "run": ck["run"] if ck else None,
                "errors": sum(1 for j in js if j is not None and j.error)}

    def preview(self, t: dict, limit: int = 60) -> dict:
        """Run a draft trigger over events already seen: how often would it have fired, and on what?"""
        prog = self._program_of(t)
        if prog is not None:
            return self.preview_program(prog, limit)
        sigs = [s for s in (t.get("sigs") or []) if isinstance(s, str)]
        if not sigs or self.events is None:
            return {"checked": 0, "matched": 0, "samples": []}
        thr = float(t.get("threshold") or 0.5)
        semantic = bool(t.get("semantic") or t.get("condition"))
        rows = self.events.recent(sigs=sigs, limit=limit)
        out, matched = [], 0
        for r in rows:
            r["context"] = {}
            p = self.judge(r, t) if semantic else None
            passed = (p is not None and p >= thr) if semantic else True
            matched += passed
            out.append({"ts": r["ts"], "app": r["app"], "who": r.get("who"), "label": r.get("label"),
                        "text": (r.get("text") or "")[:160], "p": None if p is None else round(p, 3),
                        "passed": passed})
        yes = [s for s in out if s["passed"]][:6]
        no = sorted([s for s in out if not s["passed"]], key=lambda s: -(s["p"] or 0))[:4]
        span = (rows[0]["ts"] - rows[-1]["ts"]) if len(rows) > 1 else 0
        return {"checked": len(out), "matched": matched, "semantic": semantic, "span_hours": round(span / 3600, 1),
                "samples": yes + no}

    # ---------- writing a trigger as a program ----------
    def compile(self, text: str, using: str | None = None, check_on: str | None = None) -> dict:
        """One sentence (optionally with what to do: "whenever a customer emails about a refund, draft a reply
        and file a ticket") -> a trigger program, its one-line explanation and who wrote it."""
        return self.compile_many([text], using=using, check_on=check_on)[0]

    def compile_many(self, texts: list[str], using: str | None = None, check_on: str | None = None) -> list[dict]:
        using = using if using in ("device", "zdr") else deciders.compile_with(self.setting)
        check_on = check_on if check_on in programs.RUN else "device"
        parts = [programs.split_intent(t) for t in texts]
        anyapp = [programs.says_any_app(t) for t in texts]
        sugg = [self.suggest(w, ranked=True) if w else {"kinds": [], "ranked": []} for w, _ in parts]
        for (w, _), sg, aa in zip(parts, sugg, anyapp):
            sg["any_app"] = aa
            sg["unseen_apps"] = unseen_apps(w, sg.get("named_apps") or [])
            sg["with_fields"] = [f"fields.*{_SINGULAR.get(x, x)}.count" for x in WITH_FIELD.findall(w.lower())]
        sig_list = [None] * len(texts)
        note = None
        if using == "zdr" and self.events is not None:
            try:
                sig_list = self._jev_signals([w for w, _ in parts], sugg)
            except deciders.Unavailable as e:
                using, note = "device", f"Jev unreachable ({e}); written on this device"
        out = []
        for (when, do_text), sg, js in zip(parts, sugg, sig_list):
            prog = self._assemble(when, do_text, sg, js, check_on)
            out.append({"program": prog, "explain": prog["explain"], "written_by": using,
                        "written_by_label": deciders.COMPILE_LABEL[using], "note": note,
                        "kinds": [self.describe_sig(k) for k in prog["match"]["kinds"]],
                        "signals": js["summary"] if js else None, "empty": programs.empty(prog)})
        return out

    def _cands_for(self, sugg: list[dict]) -> tuple[list[dict], dict, list[str], list[dict]]:
        """The masked catalog a compiler is shown: candidate kinds (union of each sentence's local top 12 and
        the kinds of apps it names), every app, and candidate count conditions."""
        mask = self.setting("compile_examples", "masked") != "off"
        sigs: list[str] = []
        for sg in sugg:
            sigs += [r["sig"] for r in (sg.get("ranked") or [])[:12]]
            named = set(sg.get("named_apps") or [])
            if named:
                sigs += [t["sig"] for t in self.events.types.values() if t["app"] in named
                         and t["klass"] in ("action", "incoming")][:8]
        sigs = list(dict.fromkeys(sigs))[:40]
        cands, keymap, fcands = [], {}, []
        for n, sig in enumerate(sigs):
            d = self.describe_sig(sig)
            t = self.events.type(sig) or {}
            sch = self.events.schema(sig)
            names = [k for k in sch if k not in ("via", "source", "transport", "path", "path_template", "host", "text_len")][:15]
            m = HTTP_SIG.match(sig)
            endpoint = None
            if m:      # the host and the path's plain words: no ids, no names spelled like ids
                segs = [x for x in m["path"].split("/") if re.fullmatch(r"[a-z]{3,}(?:[-_][a-z]+)*", x)]
                endpoint = f"{m['method']} {m['host']} " + " ".join(segs[-5:]) + (f" ({m['op']})" if m["op"] else "")
            c = {"key": f"k{n}", "app": d.get("app"), "label": d.get("label"), "endpoint": endpoint,
                 "direction": {"in": "reaches me", "out": "I do it"}.get(d.get("direction") or t.get("direction"), "either"),
                 "fields": names}
            if mask and sch:
                c["example"] = {k: deciders.mask_example(k, sch[k][2]) for k in names[:6]}
            cands.append(c)
            keymap[c["key"]] = sig
            for k in names:
                if not (k.endswith(".count") and sch[k][0] == "number"):
                    continue
                same = next((f for f in fcands if f["field"] == k), None)
                if same:
                    same["sigs"].append(sig)
                elif len(fcands) < 8:
                    fcands.append({"key": f"c{len(fcands)}", "field": k, "op": "gt", "value": 0, "sigs": [sig],
                                   "words": f"{programs._field_words(k)} is more than 0"})
        apps = sorted({t["app"] for t in self.events.types.values() if t["klass"] in ("action", "incoming")})[:25]
        return cands, keymap, apps, fcands

    def _jev_signals(self, whens: list[str], sugg: list[dict]) -> list[dict]:
        cands, keymap, apps, fcands = self._cands_for(sugg)
        res = deciders.Jev(self.setting).signals_many(whens, cands, apps, fcands)
        out = []
        for r in res:
            kinds = {keymap[k]: p for k, p in r["kinds"].items() if p is not None}
            out.append({"kinds": kinds, "apps": {a: p for a, p in r["apps"].items() if p is not None},
                        "direction": r["direction"], "content": r["content"],
                        "fields": [{**f, "p": r["fields"].get(f["key"])} for f in fcands],
                        "summary": {"kinds": {k: round(p, 2) for k, p in sorted(kinds.items(), key=lambda kv: -kv[1])[:6]},
                                    "direction": {k: round(v, 2) for k, v in (r["direction"] or {}).items()},
                                    "content": None if r["content"] is None else round(r["content"], 2)}})
        return out

    @staticmethod
    def _assemble(when: str, do_text: str, sg: dict, js: dict | None, check_on: str) -> dict:
        """The program from the evidence: kinds if some fit well, else app/direction filters; a meaning check
        when the sentence is about content or nothing narrower fits."""
        match: dict = {"kinds": [], "apps": [], "where": [], "any": []}
        content = bool(sg.get("about_content"))
        if js is None:                                          # on this device: the local ranking and rules
            match["kinds"] = [k["sig"] for k in sg.get("kinds") or [] if k.get("picked")]
            if not match["kinds"]:
                match["apps"] = list(sg.get("named_apps") or []) + list(sg.get("unseen_apps") or [])
                if sg.get("mine") or sg.get("theirs"):
                    match["where"].append({"field": "direction", "op": "eq", "value": "out" if sg.get("mine") else "in"})
                content = content or bool(sg.get("content_words"))
        else:                                                   # Jev's probabilities, thresholds in code
            ranked = sorted(js["kinds"].items(), key=lambda kv: -kv[1])
            top = ranked[0][1] if ranked else 0
            match["kinds"] = [k for k, p in ranked if p >= 0.6 and p >= top - 0.25][:6]
            named = {a.lower() for a, p in js["apps"].items() if p >= 0.8}
            if named and any(k.split("|")[0].split(":")[0].lower() in named for k in match["kinds"]):
                match["kinds"] = [k for k in match["kinds"] if not k.startswith("*")]   # one app named: stay in it
            if not match["kinds"]:
                apps = [a for a, p in js["apps"].items() if p >= 0.6]
                match["apps"] = apps if len(apps) <= 4 else []
                d = js["direction"] or {}
                best = max(d, key=d.get) if d else None
                if best in ("in", "out") and d[best] >= 0.6:
                    match["where"].append({"field": "direction", "op": "eq", "value": best})
            for f in js["fields"]:
                # only a field the chosen kinds carry (or any kind, when the program names none)
                if f.get("p") is not None and f["p"] >= 0.7 and (not match["kinds"] or set(f["sigs"]) & set(match["kinds"])):
                    match["where"].append({"field": f["field"], "op": f["op"], "value": f["value"]})
            content = (js["content"] or 0) >= 0.5
            if not match["kinds"] and not match["apps"]:
                match["apps"] = list(sg.get("unseen_apps") or [])
        if sg.get("any_app") and (match["kinds"] or match["apps"]):     # "in any app: …" means any app
            match["kinds"], match["apps"] = [], []
            d = (js or {}).get("direction") or {}
            best = max(d, key=d.get) if d else None
            dirn = best if best in ("in", "out") and d[best] >= 0.6 else (
                "out" if sg.get("mine") else "in" if sg.get("theirs") else None)
            if dirn and not any(c["field"] == "direction" for c in match["where"]):
                match["where"].append({"field": "direction", "op": "eq", "value": dirn})
        have = {c["field"] for c in match["where"]}
        for f in sg.get("with_fields") or []:                  # "… with attendees": that list is not empty
            if not any(h.endswith(f.split("*", 1)[1]) for h in have):
                match["where"].append({"field": f, "op": "gt", "value": 0})
        if not match["kinds"] and when:
            content = True                                      # nothing narrower than an app: the check does the rest
        prog = {"when": when, "match": match,
                "check": {"condition": derive_statement(when), "threshold": 0.5, "run": check_on} if content and when else None,
                "do": programs.do_from(do_text)}
        return programs.normalize(prog)

    def agents(self) -> list[dict]:
        out = []
        override = os.environ.get("AMBIENT_AGENT_CMD")
        if override:
            out.append({"id": "", "label": "Stand-in agent (testing)", "available": True, "local": True})
        for k, a in AGENTS.items():
            out.append({"id": k, "label": a["label"], "available": bool(shutil.which(a["cmd"][0])), "local": False})
        if shutil.which("ollama"):
            try:
                names = [ln.split()[0] for ln in subprocess.run(
                    ["ollama", "list"], capture_output=True, text=True, timeout=4).stdout.splitlines()[1:6] if ln.strip()]
            except Exception:
                names = []
            for n in names:
                out.append({"id": f"ollama:{n}", "label": f"{n} (on this machine)", "available": True, "local": True})
        return out

    def agent_cmd(self, t: dict | None) -> list[str]:
        from engine import osal
        override = os.environ.get("AMBIENT_AGENT_CMD")
        if override:
            return osal.split_cmd(override)
        # Only agents we know: a trigger (or a setting) reaches us over the local API and must never be
        # able to name a command to run. Anything else falls back to the default.
        choice = known_agent((t or {}).get("agent")) or known_agent(self.setting("agent")) or "claude"
        if choice.startswith("ollama:"):
            cmd = ["ollama", "run", choice[7:]]
        else:
            cmd = list(AGENTS[choice]["cmd"])
        cmd[0] = shutil.which(cmd[0]) or cmd[0]
        return cmd

    def run_trigger(self, mid: int, tid: int) -> dict:
        t = self.store.q("SELECT * FROM triggers WHERE id=?", (tid,))
        if not t:
            return {"error": "no such trigger"}
        t = t[0]
        return self.run_action(mid, t["action"], t["instruction"] or "", label=t["name"], trigger=t)

    # ---------- reading moments ----------
    def list(self, after: int = 0, limit: int = 50, include_sent: bool = True, status: str | None = None,
             upto: int | None = None):
        where, args = ["id > ?"], [after]
        if upto is not None:
            where.append("id <= ?"); args.append(upto)
        if not include_sent:
            where.append("kind != 'sent'")
        if status:
            where.append("status = ?"); args.append(status)
        rows = self.store.q(f"SELECT * FROM moments WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                            (*args, limit))
        for r in rows:
            r["context"] = c = json.loads(r["context"] or "{}")
            r["gate"] = json.loads(r["gate"]) if r.get("gate") else None
            if self.rec and r["app"] == "Slack" and r["kind"] != "sent":   # names may arrive after the moment
                r["who"] = self.rec.slack_name(c.get("user")) if c.get("user") else r["who"]
                r["place"] = self.rec.slack_place(c.get("channel", "")) or r["place"]
            elif self.rec and r["app"] == "Discord":
                r["place"] = self.rec.names.get(("Discord", c.get("channel", ""))) or r["place"]
        return rows

    def get(self, mid: int):
        r = self.list(after=mid - 1, limit=1, upto=mid)
        return r[0] if r else None

    def set_status(self, mid: int, status: str):
        self.store.update_table("moments", mid, status=status)

    def wait(self, after: int, timeout: float) -> list[dict]:
        end = time.time() + min(timeout, 300)
        while True:
            rows = self.list(after=after, include_sent=False, status="new")
            if rows or time.time() >= end:
                return rows
            with self.cond:
                self.cond.wait(timeout=min(5, end - time.time()))

    # ---------- actions ----------
    def prompt_for(self, m: dict, action: str, question: str = "") -> str:
        c = m["context"]
        kind = m["kind"]
        if kind == "meeting":
            src = f"Transcript of a meeting I just had (from {m['app']}):\n\n{c.get('transcript', '')}"
        elif kind in ("dm", "mention", "group", "sent"):
            who = "I" if kind == "sent" else m["who"]
            src = f"{m['app']}, {m['place']}. {who} wrote:\n\n{m['text']}"
        else:
            details = json.dumps(c.get("fields") or {}, ensure_ascii=False, indent=1)[:1500]
            src = (f"Something just happened in {m['app']}: {m['title']}.\n\n{m['text']}"
                   + (f"\n\nDetails:\n{details}" if details not in ("{}", "") else ""))
        if action == "draft":
            if kind == "meeting":
                return (src + "\n\nDraft a short follow-up message to the other participants: what we agreed, "
                        "who does what next. Output ONLY the message, plain text, no preamble or notes.")
            return (src + "\n\nDraft my reply. Short, friendly, in my voice (casual, direct). "
                    "Output ONLY the message I'd send: no preamble, no notes, no markdown, no options. "
                    "If they ask for something (a link, doc, file, time, fact), look it up with your tools first "
                    "(my notes, my drive, files on this computer) and put it in the reply.")
        return src + "\n\n" + (question or ("What should I do about this? Be brief and concrete."
                                             if kind != "meeting" else
                                             "List the action items for me and anything I committed to."))

    def run_action(self, mid: int, action: str, question: str = "", label: str | None = None,
                   trigger: dict | None = None) -> dict:
        m = self.get(mid)
        if not m:
            return {"error": "no such moment"}
        if m.get("action_status") == "running":
            return {"error": "already running"}
        prompt = self.prompt_for(m, action, question)
        trigger_id = trigger["id"] if trigger else None
        self.store.update_table("moments", mid, action=label or action, action_status="running",
                                action_result=None, action_ts=time.time(), trigger_id=trigger_id)
        cmd = self.agent_cmd(trigger)

        def go():
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                                   cwd=os.path.expanduser("~"), errors="replace")
                out = p.stdout.strip() or p.stderr.strip() or f"(no output, exit {p.returncode})"
                status = "ok" if p.returncode == 0 else "error"
            except FileNotFoundError:
                out, status = f"{os.path.basename(cmd[0])} isn't installed. Pick another agent for this trigger.", "error"
            except Exception as e:
                out, status = repr(e), "error"
            self.store.update_table("moments", mid, action_status=status, action_result=out)
            if trigger and trigger.get("auto") and out.strip() != NOTHING:
                self._note(f"{trigger['name']} ran" if status == "ok" else f"{trigger['name']} failed", out, mid, "result")
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "id": mid, "action": action}


# ---------- MCP (Streamable HTTP, JSON responses only) ----------
TOOLS = [
    {"name": "list_moments",
     "description": "Recent moments Ambient noticed in the user's apps: events that matched one of the user's "
                    "triggers. Newest first.",
     "inputSchema": {"type": "object", "properties": {
         "after_id": {"type": "integer", "description": "only moments with a larger id"},
         "limit": {"type": "integer", "default": 20},
         "status": {"type": "string", "enum": ["new", "replied", "done", "dismissed"]}}}},
    {"name": "wait_for_moment",
     "description": "Block until a new moment arrives (or timeout), then return it. Use in a loop to react "
                    "to the user's apps as things happen.",
     "inputSchema": {"type": "object", "properties": {
         "after_id": {"type": "integer", "description": "last id you've seen (default: now)"},
         "timeout_seconds": {"type": "number", "default": 120, "maximum": 300}}}},
]


def _slim(m: dict) -> dict:
    out = {k: m[k] for k in ("id", "ts", "app", "kind", "who", "place", "title", "text", "status")}
    if m["kind"] == "meeting":
        out["transcript"] = m["context"].get("transcript")
    return out


def mcp_handle(moments: Moments, msg: dict) -> dict | None:
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}  # noqa: E731
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "ambient", "version": "0.2"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, a = params.get("name"), params.get("arguments") or {}
        if name == "list_moments":
            rows = moments.list(after=int(a.get("after_id", 0)), limit=min(int(a.get("limit", 20)), 100),
                                status=a.get("status"))
            data = [_slim(m) for m in rows]
        elif name == "wait_for_moment":
            after = a.get("after_id")
            if after is None:
                last = moments.store.q("SELECT COALESCE(MAX(id),0) n FROM moments")[0]["n"]
                after = last
            data = [_slim(m) for m in moments.wait(int(after), float(a.get("timeout_seconds", 120)))]
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        return ok({"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}]})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
