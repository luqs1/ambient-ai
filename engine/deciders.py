"""Where a trigger's decisions run: on this device, or on a zero-data-retention decision model.

Two decisions, one interface:
  judge(state, condition) -> Judgment     does this event satisfy the trigger's condition? (P + short reason)
  signals(intent, catalog) -> dict        evidence for writing a trigger: which kinds, apps, direction,
                                          whether the sentence is about content, which field conditions
Both providers feed the same assembly (Moments.compile), so a program is the same shape whichever wrote it.

  device  the local NLI judge (DeBERTa-v3-xsmall) and embedder (bge-small) in decide.py. Nothing leaves.
  zdr     TypeSafe's Jev, a System One decision model: typed answers and calibrated probabilities, no text
          generation. Used under the account's zero-data-retention terms. It is sent only what a decision
          needs: for a check, the fields the trigger names (`check.fields`); for compiling, the sentence plus
          the catalog's field names with every example value masked to its shape.

Never a general-purpose cloud LLM. Never events of a paused app (callers check; judge refuses `paused`).
The key comes from AMBIENT_TYPESAFE_KEY / TYPESAFE_API_KEY, or a file named by the `zdr_key_file`
setting (a .env line TYPESAFE_API_KEY=...). It is never stored in the database, logged or returned.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

API = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
TIMEOUT = 10.0
LABEL = {"device": "checked on this device", "zdr": "checked by Jev, zero data retention"}
COMPILE_LABEL = {"device": "written on this device", "zdr": "written with Jev, zero data retention"}
usage = {"requests": 0, "questions": 0, "input_tokens": 0, "errors": 0, "last_error": None, "last_ts": None}
_usage_lock = threading.Lock()


class Unavailable(Exception):
    """The provider could not answer (no key, network, rate limit, bad reply)."""


@dataclass
class Judgment:
    p: float | None
    reason: str
    run: str                       # device | zdr  (which provider actually answered)
    by: str                        # the words the UI shows: "checked on this device" …
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


def _explicit(condition: str) -> bool:
    """A full statement ("The message asks for a refund.") rather than a sentence fragment ("someone asks me
    for a refund"): judged as written instead of wrapped in "In this case, …"."""
    c = condition.strip()
    return c.endswith(".") and c[:1].isupper()


# ---------- on this device ----------
class Local:
    id = "device"
    label = LABEL["device"]

    def available(self) -> bool:
        from engine import decide
        return decide.judge() is not None

    def judge(self, state: dict, condition: str) -> Judgment:
        from engine import decide
        text = str(state.get("text") or state.get("title") or "")[:1500]
        if state.get("title") and state.get("text") and state["title"] not in text:
            text = f"{state['title']}: {text}"[:1500]
        wordless = not text.strip()
        if wordless and state.get("label"):            # a click with no words (a star, an archive): judge what it is
            text = f"{state['label']} in {state.get('app') or 'an app'}"
        if not text.strip():
            return Judgment(None, "nothing to read in this event", "device", self.label, "no text")
        if _explicit(condition):
            p = decide.prob(text, condition)
        else:
            app, label = state.get("app") or "an app", state.get("label") or "something"
            ctx = f"{label} {'received ' if state.get('direction') == 'in' else ''}in {app}"
            if state.get("who"):
                ctx += f" from {state['who']}"
            ps = [decide.prob(f'{ctx}: "{text}"', f"In this case, {condition}."),
                  decide.prob(text, f"This example is: {condition}.")]
            p = None if any(x is None for x in ps) else sum(ps) / len(ps)
        if p is None:
            return Judgment(None, "the on-device model is unavailable", "device", self.label, "model unavailable")
        return Judgment(p, f"{round(p * 100)}% that {condition.rstrip('.')}", "device", self.label)

    def judge_many(self, items: list[tuple[dict, str]]) -> list[Judgment]:
        return [self.judge(s, c) for s, c in items]


# ---------- Jev, zero data retention ----------
def _read_key(setting) -> str | None:
    k = os.environ.get("AMBIENT_TYPESAFE_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if k:
        return k.strip()
    path = setting("zdr_key_file") if setting else None
    if path:
        try:
            for line in Path(os.path.expanduser(path)).read_text().splitlines():
                m = re.match(r"\s*(?:export\s+)?(?:AMBIENT_TYPESAFE_KEY|TYPESAFE_API_KEY)\s*=\s*['\"]?([^'\"\s#]+)", line)
                if m:
                    return m.group(1)
        except OSError:
            return None
    return None


def http_post(url: str, body: dict, key: str, timeout: float = TIMEOUT) -> dict:
    """POST JSON, return JSON. Retries once on 429/529/5xx. Raises Unavailable. (Tests replace this.)"""
    data = json.dumps(body).encode()
    last = None
    for attempt in range(2):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "ambient"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (429, 529) or e.code >= 500:
                retry = e.headers.get("retry-after") if e.headers else None
                time.sleep(min(float(retry) if retry and retry.replace(".", "").isdigit() else 0.8, 3))
                continue
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except Exception:
                detail = None
            raise Unavailable(f"{last}: {str(detail)[:200]}" if detail else last) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"network: {type(e).__name__}"
            if attempt == 0:
                time.sleep(0.4)
                continue
        except ValueError:
            raise Unavailable("bad reply") from None
    raise Unavailable(last or "unreachable")


def _note(n_questions: int, resp: dict | None, err: str | None = None):
    with _usage_lock:
        usage["requests"] += 1
        usage["questions"] += n_questions
        usage["last_ts"] = time.time()
        if resp:
            usage["input_tokens"] += int((resp.get("usage") or {}).get("input_tokens") or 0)
        if err:
            usage["errors"] += 1
            usage["last_error"] = err


class Jev:
    id = "zdr"
    label = LABEL["zdr"]

    def __init__(self, setting=None):
        self.setting = setting or (lambda k, d=None: d)

    def available(self) -> bool:
        return bool(_read_key(self.setting))

    def model(self) -> str:
        m = self.setting("zdr_model", DEFAULT_MODEL) or DEFAULT_MODEL
        return m if re.fullmatch(r"jev[\w.\-]*", m) else DEFAULT_MODEL

    def ask(self, state, questions: dict) -> dict:
        """One System One request. Returns {question id: answer}. Raises Unavailable."""
        key = _read_key(self.setting)
        if not key:
            raise Unavailable("no TypeSafe key configured")
        body = {"model": self.model(), "state": state, "questions": questions}
        try:
            resp = http_post(API, body, key)
        except Unavailable as e:
            _note(len(questions), None, str(e))
            raise
        _note(len(questions), resp)
        answers = resp.get("answers") if isinstance(resp, dict) else None
        if not isinstance(answers, dict):
            raise Unavailable("bad reply")
        return answers

    @staticmethod
    def _noul(condition: str) -> dict:
        if _explicit(condition):
            instr = {"task": "Is this statement true of the event in `event`?", "statement": condition}
        else:
            instr = {"task": "Is the event in `event` a case of what this trigger sentence describes? Judge what the "
                             "event is and what it says, not just its kind.",
                     "trigger sentence": condition}
        return {"type": "noul", "instructions": instr}

    def judge_many(self, items: list[tuple[dict, str]]) -> list[Judgment]:
        """Several (event state, condition) pairs. Pairs with the same state share one request."""
        out: list[Judgment | None] = [None] * len(items)
        groups: dict[str, list[int]] = {}
        for i, (state, cond) in enumerate(items):
            if state.get("paused"):
                out[i] = Judgment(None, "app is paused: not sent", "zdr", self.label, "paused")
                continue
            groups.setdefault(json.dumps(state, sort_keys=True, default=str), []).append(i)
        for sk, idxs in groups.items():
            state = json.loads(sk)
            qs = {f"c{n}": self._noul(items[i][1]) for n, i in enumerate(idxs)}
            try:
                ans = self.ask({"event": state}, qs)
            except Unavailable as e:
                for i in idxs:
                    out[i] = Judgment(None, f"Jev unreachable ({e})", "zdr", self.label, str(e))
                continue
            for n, i in enumerate(idxs):
                a = ans.get(f"c{n}") or {}
                p = a.get("noul")
                if not isinstance(p, (int, float)):
                    out[i] = Judgment(None, "Jev gave no answer", "zdr", self.label, "no answer")
                else:
                    p = float(p)
                    out[i] = Judgment(p, f"Jev: {round(p * 100)}% that {items[i][1].rstrip('.')}", "zdr", self.label)
        return out  # type: ignore[return-value]

    def judge(self, state: dict, condition: str) -> Judgment:
        return self.judge_many([(state, condition)])[0]

    def judge_batch(self, events: list[dict], condition: str) -> list[Judgment]:
        """One condition over many events in a single request (backtests): state holds every event."""
        if not events:
            return []
        state = {"events": {f"e{i}": e for i, e in enumerate(events)}}
        qs = {}
        for i in range(len(events)):
            q = self._noul(condition)
            q["instructions"] = {**q["instructions"], "event": f"`events.e{i}`",
                                 "task": q["instructions"]["task"].replace("`event`", f"`events.e{i}`")}
            qs[f"e{i}"] = q
        try:
            ans = self.ask(state, qs)
        except Unavailable as e:
            return [Judgment(None, f"Jev unreachable ({e})", "zdr", self.label, str(e)) for _ in events]
        out = []
        for i in range(len(events)):
            p = (ans.get(f"e{i}") or {}).get("noul")
            out.append(Judgment(float(p), f"Jev: {round(float(p) * 100)}% that {condition.rstrip('.')}", "zdr", self.label)
                       if isinstance(p, (int, float)) else Judgment(None, "Jev gave no answer", "zdr", self.label, "no answer"))
        return out

    # ----- compiling a trigger -----
    def signals_many(self, intents: list[str], cands: list[dict], apps: list[str], field_cands: list[dict]) -> list[dict]:
        """Evidence for several trigger sentences in one request. `cands` are masked kind descriptions
        ({key, app, label, direction, fields, example}), `field_cands` are candidate conditions
        ({key, words}). The catalog is the shared state; each sentence goes in its questions."""
        state = {"kinds": {c["key"]: {k: v for k, v in c.items() if k != "key"} for c in cands}, "apps": apps}
        qs: dict = {}
        for i, intent in enumerate(intents):
            ts = {"trigger sentence": intent}
            qs[f"i{i}.dir"] = {"type": "choice", "instructions": {
                **ts, "task": "Who acts in the event this trigger sentence describes?"},
                "criteria": {"in": "Something reaches me or is done by someone else: a message, email, mention, "
                                   "invite, notification, or another person's action",
                             "out": "I do it myself: I send, write, create, edit, star, post, open or visit something",
                             "either": "It could be either, or the sentence does not say"}}
            qs[f"i{i}.content"] = {"type": "noul", "instructions": {
                **ts, "task": "Does the sentence restrict what the message or item says or is about, beyond which "
                              "kind of event happens?"},
                "criteria": {"true": "It names a topic, request or content: 'someone asks me for a file', 'an email "
                                     "about a refund', 'a page about pricing', 'I promise to do something'",
                             "false": "Only the kind of event: 'someone DMs me', 'I star a repository', 'a meeting "
                                      "ends', 'I create a calendar event'"}}
            for c in cands:
                qs[f"i{i}.k.{c['key']}"] = {"type": "noul", "instructions": {
                    **ts, "kind": f"`kinds.{c['key']}`",
                    "task": "Should a trigger for this sentence watch events of this kind? Yes if this kind of event "
                            "is the kind of thing the sentence describes happening (ignore what the content says). "
                            "No if it is a different action, a different app from one the sentence names, or the "
                            "opposite direction (something I do versus something that reaches me)."}}
            for n, a in enumerate(apps):
                qs[f"i{i}.a{n}"] = {"type": "noul", "instructions": {
                    **ts, "app": a, "task": "Does the sentence name this app, or describe something that would happen "
                                            "in it (for example email for a mail app)?"}}
            for f in field_cands:
                qs[f"i{i}.f.{f['key']}"] = {"type": "noul", "instructions": {
                    **ts, "condition": f["words"],
                    "task": "Does the sentence require this condition to hold for the events it describes?"}}
        ans = self.ask(state, qs)
        out = []
        for i in range(len(intents)):
            def p(k):
                v = (ans.get(f"i{i}.{k}") or {}).get("noul")
                return float(v) if isinstance(v, (int, float)) else None
            d = ans.get(f"i{i}.dir") or {}
            out.append({"direction": d.get("probabilities") or {}, "content": p("content"),
                        "kinds": {c["key"]: p(f"k.{c['key']}") for c in cands},
                        "apps": {a: p(f"a{n}") for n, a in enumerate(apps)},
                        "fields": {f["key"]: p(f"f.{f['key']}") for f in field_cands}})
        return out


# ---------- masking what a compiler sees ----------
META_FIELDS = frozenset({"method", "transport", "direction", "source", "status", "op", "klass"})


def mask_example(field_name: str, v) -> object:
    """The shape of an example value, not the value: a compiler needs to know a field holds an email or a
    count, never whose email."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return "<number>"
    s = str(v)
    if field_name in META_FIELDS and re.fullmatch(r"[A-Za-z_\-]{1,20}|\d{3}", s):
        return s
    if re.search(r"[\w.+-]+@[\w-]+\.", s):
        return "<email>"
    if s.startswith(("http://", "https://")):
        return "<url>"
    if re.fullmatch(r"\d{4}-\d\d-\d\d.*", s):
        return "<date>"
    if re.fullmatch(r"[\w\-:.]{6,}", s) and re.search(r"\d", s):
        return "<id>"
    n = len(s.split())
    return f"<text, {n} word{'s' if n != 1 else ''}>"


def get(run: str, setting=None):
    return Jev(setting) if run == "zdr" else Local()


def status(setting) -> dict:
    j = Jev(setting)
    with _usage_lock:
        u = dict(usage)
    return {"providers": [
        {"id": "device", "label": LABEL["device"], "available": Local().available(), "default": True,
         "sees": "everything stays on this device"},
        {"id": "zdr", "label": LABEL["zdr"], "available": j.available(), "model": j.model(),
         "sees": "only the fields a trigger names, for events that already passed its match"}],
        "fallback": setting("zdr_fallback", "closed") or "closed",
        "compile_with": compile_with(setting), "compile_examples": setting("compile_examples", "masked") or "masked",
        "usage": u}


def compile_with(setting) -> str:
    v = setting("compile_with", None)
    if v in ("device", "zdr"):
        return v if v == "device" or Jev(setting).available() else "device"
    return "zdr" if Jev(setting).available() else "device"
