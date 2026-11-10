"""Evaluate trigger writing and checking on a fixed synthetic set: 40 intents x 30 events.

    uv run python tests/eval_triggers.py            # on this device only (no network)
    uv run python tests/eval_triggers.py --zdr      # also Jev (needs AMBIENT_TYPESAFE_KEY or --key-file)

Compares
  judging   local NLI vs Jev on every (intent, event) pair: is this event what the sentence means?
  matching  old (suggest() ticks kinds + optional local check) vs new (compile() -> program -> match + check),
            with the program written and checked on the device or with Jev.
Everything is made up here. Jev replies are cached in the scratch file given by --cache, so a rerun sends
nothing new.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from fnmatch import fnmatchcase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.dirname(os.path.abspath(__file__))]
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ["AMBIENT_AGENT_CMD"] = "cat"

from engine import deciders  # noqa: E402
from engine import triggers as P  # noqa: E402
from engine.events import Events  # noqa: E402
from engine.moments import Moments, derive_statement  # noqa: E402
from engine.store import Store  # noqa: E402
from helpers import send, visit  # noqa: E402

LINEAR = "mutation IssueCreate($i: I!) { issueCreate(input: $i) { success } }"
LCOMMENT = "mutation CommentCreate($b: String) { commentCreate(body: $b) { success } }"


def chat(ev, app, kind, text, who="Priya", key=None):
    out = kind == "sent"
    return ev.emit(app=app, sig=f"{app.lower()}:{kind}", label={"dm": "someone DMs me", "sent": "I send a message",
                   "mention": "someone @mentions me"}[kind], direction="out" if out else "in",
                   klass="action" if out else "incoming", who="you" if out else who, place=f"DM with {who}",
                   text=text, key=key or f"{app}:{kind}:{text}")


def mail_in(ev, frm, subject, body):
    ev.ws("Zen", "mail.google.com", "/sync/u/0", json.dumps({"type": "new_message", "message": {
        "from": frm, "subject": subject, "body": body}}), from_client=False)


def mail_out(ev, to, subject, body):
    send(ev, "Zen", "mail.google.com", "/mail/u/0/api/send", {"to": [to], "subject": subject, "body": body})


def calendar(ev, title, attendees):
    send(ev, "Zen", "www.googleapis.com", "/calendar/v3/calendars/primary/events",
         {"summary": title, "attendees": [{"email": a} for a in attendees], "start": {"dateTime": "2026-11-12T10:00:00Z"}})


# (id, how to make it). Ids are what the labels below refer to.
EVENTS = [
    (1, lambda e: chat(e, "Slack", "dm", "can you send me the Q3 deck before the board call?")),
    (2, lambda e: chat(e, "Slack", "dm", "haha nice one")),
    (3, lambda e: chat(e, "Slack", "dm", "are you free for a quick call tomorrow at 3?")),
    (4, lambda e: chat(e, "Slack", "dm", "the checkout page is throwing a 500 again")),
    (5, lambda e: chat(e, "Slack", "dm", "how much would 40 seats cost on the enterprise plan?", who="Omar")),
    (6, lambda e: chat(e, "Slack", "sent", "I'll send you the contract by Friday")),
    (7, lambda e: chat(e, "Slack", "sent", "lol same")),
    (8, lambda e: chat(e, "WhatsApp", "dm", "could you share the photos from the trip?", who="Dana")),
    (9, lambda e: chat(e, "Discord", "mention", "can you review my PR when you get a sec?", who="kai")),
    (10, lambda e: mail_in(e, "jo@customer.example", "Refund request for order 1182",
                           "I was charged twice this month, please refund the extra charge")),
    (11, lambda e: mail_in(e, "news@tips.example", "Your weekly newsletter", "Ten tips for a more productive week")),
    (12, lambda e: mail_in(e, "billing@hosting.example", "Invoice INV-2207 from Acme Hosting",
                           "Your invoice for November is attached, due November 30")),
    (13, lambda e: mail_out(e, "ap@client.example", "Invoice for October", "Please find attached the invoice for October work")),
    (14, lambda e: mail_out(e, "sam@friend.example", "Dinner on Saturday?", "Are you around for dinner on Saturday evening?")),
    (15, lambda e: calendar(e, "Quarterly planning", ["dana@acme.example", "lee@acme.example"])),
    (16, lambda e: calendar(e, "Focus time", [])),
    (17, lambda e: send(e, "Zen", "api.github.com", "/user/starred/acme/rocket", method="PUT")),
    (18, lambda e: send(e, "Zen", "api.github.com", "/repos/acme/api/issues/42/comments",
                        {"body": "Looks good, merging after CI passes"})),
    (19, lambda e: send(e, "Zen", "api.github.com", "/repos/acme/api/issues",
                        {"title": "Login fails on Safari", "body": "Steps to reproduce: open the login page in Safari 18"})),
    (20, lambda e: send(e, "Zen", "client-api.linear.app", "/graphql", {"operationName": "IssueCreate", "query": LINEAR,
                        "variables": {"i": {"title": "Export to CSV drops the last row"}}})),
    (21, lambda e: send(e, "Notion", "www.notion.so", "/api/v3/saveTransactions",
                        {"operations": [{"command": "set", "args": {"title": "Meeting notes: hiring plan for Q1"}}]})),
    (22, lambda e: visit(e, "Zen", "github.com", "/acme/api/pull/12", "Fix flaky test · Pull Request #12 · acme/api")),
    (23, lambda e: visit(e, "Zen", "news.ycombinator.com", "/news", "Hacker News")),
    (25, lambda e: e.emit(app="Granola", sig="granola:meeting", label="a meeting ends", direction="in", klass="incoming",
                          text="Weekly sync. We agreed Dana will send the proposal by Monday.", key="g1",
                          context={"transcript": "Weekly sync. We agreed Dana will send the proposal by Monday."})),
    (26, lambda e: send(e, "Zen", "x.com", "/i/api/graphql/a1B2c3/CreateTweet",
                        {"variables": {"tweet_text": "Just shipped our new release, check it out"}})),
    (27, lambda e: send(e, "Zen", "client-api.linear.app", "/graphql", {"operationName": "CommentCreate", "query": LCOMMENT,
                        "variables": {"b": "Same here on Safari, looks related"}})),
    (28, lambda e: chat(e, "Slack", "dm", "please send over the signed NDA when you can", who="Lee")),
    (29, lambda e: mail_in(e, "dana@acme.example", "Can we move our meeting?", "Could we move Tuesday's meeting to Thursday?")),
    (30, lambda e: chat(e, "Discord", "dm", "the bot is down again, nothing responds", who="kai")),
]

INTENTS = [
    ("someone asks me for a file", {1, 8, 28}),
    ("someone DMs me", {1, 2, 3, 4, 5, 8, 28, 30}),
    ("someone @mentions me", {9}),
    ("I promise to do something in a message I send", {6}),
    ("someone wants to schedule a meeting with me", {3, 29}),
    ("someone reports a bug or something broken", {4, 30}),
    ("a customer emails about a refund", {10}),
    ("I receive an invoice by email", {12}),
    ("I send an email with an invoice", {13}),
    ("I send an email", {13, 14}),
    ("I get an email", {10, 11, 12, 29}),
    ("I create a calendar event", {15, 16}),
    ("I create a calendar event with attendees", {15}),
    ("I star a repository on GitHub", {17}),
    ("I comment on a GitHub issue", {18}),
    ("I open an issue on GitHub", {19}),
    ("I create an issue in Linear", {20}),
    ("I file a bug report", {19, 20}),
    ("I open a pull request page on GitHub", {22}),
    ("I open a page on any website", {22, 23}),
    ("a meeting ends", {25}),
    ("I post a tweet", {26}),
    ("I comment on something in Linear", {27}),
    ("someone asks about pricing", {5}),
    ("someone asks me to review something", {9}),
    ("I write meeting notes in Notion", {21}),
    ("in any app, I send something that mentions an invoice", {13}),
    ("someone emails me to reschedule a meeting", {29}),
    ("I send a message on Slack", {6, 7}),
    ("someone messages me on WhatsApp", {8}),
    ("someone messages me on Discord", {9, 30}),
    ("I receive a newsletter", {11}),
    ("a meeting ends where someone commits to sending something", {25}),
    ("I reply on a GitHub issue", {18}),
    ("I announce a release", {26}),
    ("something breaks in production", {4, 30}),
    ("I agree to send a contract", {6}),
    ("someone asks me to share photos", {8}),
    ("I invite a friend to dinner", {14}),
    ("I block focus time in my calendar", {16}),
]


def build():
    st = Store(os.path.join(tempfile.mkdtemp(), "eval.db"))
    ev = Events(st)
    m = Moments(st, ev)
    for t in m.triggers():
        m.delete_trigger(t["id"])
    ids = {}
    for eid, make in EVENTS:
        before = st.q("SELECT COALESCE(MAX(id),0) n FROM events")[0]["n"]
        make(ev)
        rows = st.q("SELECT id FROM events WHERE id > ?", (before,))
        assert rows, f"event {eid} was not stored"
        ids[rows[-1]["id"]] = eid
    ev.flush()
    rows = {ids[r["id"]]: r for r in ev.recent(limit=200) if r["id"] in ids}
    return m, ev, rows


def prf(fired: dict, truth: dict) -> dict:
    tp = sum(len(fired[i] & truth[i]) for i in truth)
    fp = sum(len(fired[i] - truth[i]) for i in truth)
    fn = sum(len(truth[i] - fired[i]) for i in truth)
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    exact = sum(fired[i] == truth[i] for i in truth)
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / (p + r), 3) if p + r else 0,
            "exact": f"{exact}/{len(truth)}"}


def cached_post(path):
    cache = json.load(open(path)) if os.path.exists(path) else {}
    real = deciders.http_post
    sent = {"n": 0}

    def post(url, body, key, timeout=deciders.TIMEOUT):
        k = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if k not in cache:
            cache[k] = real(url, body, key, timeout=30)
            sent["n"] += 1
            json.dump(cache, open(path, "w"))
        return cache[k]
    return post, sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zdr", action="store_true")
    ap.add_argument("--key-file", default=None)
    ap.add_argument("--cache", default=os.path.join(tempfile.gettempdir(), "ambient_eval_jev_cache.json"))
    a = ap.parse_args()
    m, ev, rows = build()
    if a.key_file:
        m.set_setting("zdr_key_file", a.key_file)
    sent = {"n": 0}
    if a.zdr:
        deciders.http_post, sent = cached_post(a.cache)
    truth = {i: lab for i, (_, lab) in enumerate(INTENTS)}
    conds = [derive_statement(P.split_intent(t)[0]) for t, _ in INTENTS]
    states = {eid: P.check_state({"check": {"fields": P.DEFAULT_CHECK_FIELDS}}, r["wide"]) for eid, r in rows.items()}

    # ---- judging: every (intent, event) pair ----
    t0 = time.time()
    local = {(i, e): deciders.Local().judge(states[e], c).p for i, c in enumerate(conds) for e in rows}
    t_local = (time.time() - t0) / max(len(local), 1) * 1000
    judges = {"device": local}
    if a.zdr:
        t0 = time.time()
        zdr = {}
        jev = deciders.Jev(m.setting)
        for e in rows:                                           # one request per event, all conditions in it
            res = jev.judge_many([(states[e], c) for c in conds])
            for i, j in enumerate(res):
                zdr[(i, e)] = j.p
        judges["zdr"] = zdr
        t_zdr = (time.time() - t0) / len(rows) * 1000
    print(f"\n{len(INTENTS)} intents x {len(rows)} events = {len(local)} pairs "
          f"({sum(len(v) for v in truth.values())} positive)\n")
    print("JUDGING  (is this event what the sentence means? threshold 0.5)")
    for name, js in judges.items():
        tp = sum(1 for (i, e), p in js.items() if p is not None and p >= .5 and e in truth[i])
        fp = sum(1 for (i, e), p in js.items() if p is not None and p >= .5 and e not in truth[i])
        fn = sum(1 for (i, e), p in js.items() if (p is None or p < .5) and e in truth[i])
        tn = len(js) - tp - fp - fn
        none = sum(1 for p in js.values() if p is None)
        pos, neg = tp + fn, fp + tn
        bacc = (tp / pos + tn / neg) / 2
        prec = tp / (tp + fp) if tp + fp else 0
        print(f"  {name:7} balanced acc {bacc:.3f}  recall {tp / pos:.3f}  precision {prec:.3f}  "
              f"false alarms {fp}/{neg}  no answer {none}")
    print(f"  latency: device {t_local:.1f} ms/pair" + (f", Jev {t_zdr:.0f} ms/request ({len(conds)} questions)" if a.zdr else ""))

    # ---- matching: whole pipeline per intent ----
    print("\nMATCHING  (which events would the trigger have fired on)")
    results = {}
    old = {}
    for i, (text, _) in enumerate(INTENTS):
        s = m.suggest(text)
        picked = [k["sig"] for k in s["kinds"] if k.get("picked")]
        fired = set()
        for e, r in rows.items():
            if not any(fnmatchcase(r["sig"], p) for p in picked):
                continue
            if s.get("about_content"):
                p = m.judge({**r, "context": {}}, {"text": text, "semantic": 1})
                if p is None or p < 0.5:
                    continue
            fired.add(e)
        old[i] = fired
    results["old: suggest + local check"] = old
    combos = [("device", "device")] + ([("device", "zdr"), ("zdr", "device"), ("zdr", "zdr")] if a.zdr else [])
    progs = {}
    for using, check_on in combos:
        if using not in progs:
            progs[using] = [c["program"] for c in m.compile_many([t for t, _ in INTENTS], using=using)] \
                if using == "device" else [c["program"] for b in range(0, len(INTENTS), 4)
                                           for c in m.compile_many([t for t, _ in INTENTS[b:b + 4]], using=using)]
        fired = {}
        for i, prog in enumerate(progs[using]):
            fired[i] = set()
            for e, r in rows.items():
                if not P.matches(prog, r["wide"]):
                    continue
                if prog["check"]:
                    key = (i, e)
                    p = judges[check_on].get(key) if prog["check"]["condition"] == conds[i] else None
                    if p is None and prog["check"]["condition"] != conds[i]:
                        p = deciders.get(check_on, m.setting).judge(states[e], prog["check"]["condition"]).p
                    if p is None or p < prog["check"]["threshold"]:
                        continue
                fired[i].add(e)
        results[f"new: written {using}, checked {check_on}"] = fired
    for name, fired in results.items():
        r = prf(fired, truth)
        print(f"  {name:38} precision {r['precision']:.3f}  recall {r['recall']:.3f}  F1 {r['f1']:.3f}  "
              f"intents exactly right {r['exact']}")
    if os.environ.get("EVAL_VERBOSE"):
        for i, (text, lab) in enumerate(INTENTS):
            line = " | ".join(f"{sorted(f[i])}" for f in results.values())
            print(f"  {text[:50]:50} want {sorted(lab)}  got {line}")
        for using, ps in progs.items():
            for i, p in enumerate(ps):
                print(f"  [{using}] {INTENTS[i][0][:45]:45} {json.dumps(p['match'])[:150]} check={bool(p['check'])}")
    if a.zdr:
        print(f"\nJev requests sent this run: {sent['n']} (cached replies reused otherwise); usage {deciders.usage}")


if __name__ == "__main__":
    main()
