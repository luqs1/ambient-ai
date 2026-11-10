"""Triggers and moments: binding, the on-device meaning check, auto vs ask-first, pausing, migration,
suggestions and previews. The local judge is replaced by `fake_prob` except where a test says otherwise."""
import json

import pytest

from engine import decide
from engine.moments import GROUPS, NOTHING, PROMISE, STARTER_TRIGGERS, Moments, derive_statement, mcp_handle
from engine.store import Store
from engine.events import Events
from helpers import kind, send, stored, visit, wait_for

LINEAR = {"operationName": "IssueCreate", "query": "mutation IssueCreate($i: I!) { issueCreate(input: $i) { success } }"}


def dm(events, text, app="Slack", who="Priya", key=None, kind_="dm"):
    return events.emit(app=app, sig=f"{app.lower()}:{kind_}", label="someone DMs me" if kind_ == "dm" else "I send a message",
                       direction="out" if kind_ == "sent" else "in", klass="action" if kind_ == "sent" else "incoming",
                       who="you" if kind_ == "sent" else who, place=f"DM with {who}", text=text,
                       key=key or f"{app}:{kind_}:{text}", context={"channel": "D024BE91L"})


def linear_issue(events, title):
    send(events, "Zen", "client-api.linear.app", "/graphql", {**LINEAR, "variables": {"i": {"title": title}}})
    return kind(events, "IssueCreate")["sig"]


def add(moments, **kw):
    r = moments.save_trigger({"action": "ask", "instruction": "Summarise this in one line.", **kw})
    assert r.get("ok"), r
    return r["id"]


def trig(moments, tid):
    return next(t for t in moments.triggers() if t["id"] == tid)


# ---------- binding ----------
def test_binding_by_exact_signature(moments, events):
    sig = linear_issue(events, "Export button does nothing on staging")
    assert moments.list() == []                                    # nothing bound yet: an event is not a moment
    add(moments, name="New issue", text="I create an issue in Linear", sigs=[sig])
    linear_issue(events, "Login page throws a 500 after password reset")
    send(events, "Zen", "client-api.linear.app", "/graphql",
         {"query": "mutation CommentCreate($b: String) { commentCreate(body: $b) { success } }", "variables": {"b": "Same here on Safari, looks related"}})
    ms = moments.list()
    assert len(ms) == 1
    m = ms[0]
    assert m["sig"] == sig and m["app"] == "Linear" and m["kind"] == "event" and m["title"] == "issue create"
    assert m["text"] == "Login page throws a 500 after password reset" and m["gate"] is None
    assert m["status"] == "done"                                   # something I did, nothing to answer
    assert len(moments.work.seen) == 1                             # the unbound kind never reached the worker


def test_binding_by_pattern(moments, events):
    add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    dm(events, "can you send me the deck?", app="Slack")
    dm(events, "are you around this afternoon?", app="WhatsApp", who="Dana")
    dm(events, "on my way now", app="Discord", kind_="sent")
    events.emit(app="Granola", sig="granola:meeting", label="a meeting ends", direction="in", klass="incoming", text="thanks all", key="g1")
    ms = moments.list()
    assert sorted((m["app"], m["kind"], m["who"]) for m in ms) == [("Slack", "dm", "Priya"), ("WhatsApp", "dm", "Dana")]
    assert all(m["status"] == "new" and m["sig"].endswith(":dm") for m in ms)


def test_binding_generic_kinds_by_wildcard(moments, events):
    add(moments, name="Anything in Linear", text="I do something in Linear", sigs=["Linear|POST *"])
    linear_issue(events, "Export button does nothing on staging")
    send(events, "Zen", "client-api.linear.app", "/graphql",
         {"query": "mutation CommentCreate($b: String) { commentCreate(body: $b) { success } }", "variables": {"b": "Same here on Safari, looks related"}})
    visit(events, "Zen", "linear.app", "/acme/inbox", "Inbox · Linear")
    assert sorted(m["title"] for m in moments.list()) == ["comment create", "issue create"]


def test_one_event_makes_one_moment_even_with_several_triggers(moments, events):
    a = add(moments, name="A", text="someone DMs me", sigs=["*:dm"])
    b = add(moments, name="B", text="someone DMs me on Slack", sigs=["slack:dm"])
    ev = dm(events, "can you send me the deck?")
    moments._handle(ev)                                            # delivered twice
    assert len(moments.list()) == 1 and a != b
    assert [n["kind"] for n in moments.notes_after(0)["notes"]] == ["offer"]


def test_disabled_and_deleted_triggers_do_not_fire(moments, events):
    tid = add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"], enabled=False)
    dm(events, "first message here")
    assert moments.list() == []
    moments.save_trigger({"id": tid, "name": "Any DM", "text": "someone DMs me", "sigs": ["*:dm"], "action": "draft"})
    dm(events, "second message here")
    assert len(moments.list()) == 1
    moments.delete_trigger(tid)
    dm(events, "third message here")
    assert len(moments.list()) == 1 and moments.triggers() == []


# ---------- the meaning check ----------
def test_no_moment_when_the_meaning_check_fails(moments, events, fake_prob):
    tid = add(moments, name="File asks", text="When someone asks me for a file.", sigs=["*:dm"], semantic=True)
    fake_prob.value = 0.02
    dm(events, "haha nice")
    assert moments.list() == [] and moments.notes_after(0)["notes"] == []
    assert len(stored(events)) == 1                                # the event is still in the catalog, just not a moment
    c = moments.checks[0]
    assert (c["passed"], c["p"], c["trigger"], c["app"], c["sig"], c["text"]) == (False, 0.02, tid, "Slack", "slack:dm", "haha nice")
    t = trig(moments, tid)
    assert (t["checked"], t["matched"], t["fired"]) == (1, 0, 0)


def test_moment_when_the_meaning_check_passes(moments, events, fake_prob):
    tid = add(moments, name="File asks", text="When someone asks me for a file.", sigs=["*:dm"], semantic=True, threshold=0.6)
    fake_prob.value = 0.91
    dm(events, "can you send me the deck from yesterday?")
    m = moments.list()[0]
    assert m["gate"] == [{"trigger": tid, "name": "File asks", "p": 0.91, "passed": True, "local": True}]
    assert moments.checks[0]["passed"] is True
    assert (trig(moments, tid)["checked"], trig(moments, tid)["matched"]) == (1, 1)
    # the model was asked about this message and this sentence, with the sender as context
    assert fake_prob.calls == [
        ('Message received in Slack from Priya: "can you send me the deck from yesterday?"', "In this case, someone asks me for a file."),
        ("can you send me the deck from yesterday?", "This example is: someone asks me for a file.")]


def test_threshold_is_respected(moments, events, fake_prob):
    add(moments, name="Strict", text="someone asks me for a file", sigs=["*:dm"], semantic=True, threshold=0.8)
    fake_prob.value = 0.79
    dm(events, "could you share that spreadsheet")
    assert moments.list() == []
    fake_prob.value = 0.8
    dm(events, "could you share that other spreadsheet")
    assert len(moments.list()) == 1


def test_fail_closed_when_the_model_is_unavailable(moments, events, fake_prob):
    tid = add(moments, name="File asks", text="someone asks me for a file", sigs=["*:dm"], semantic=True, auto=True)
    fake_prob.value = None
    dm(events, "can you send me the deck from yesterday?")
    assert moments.list() == [] and moments.notes_after(0)["notes"] == []
    assert moments.checks[0]["p"] is None and moments.checks[0]["passed"] is False
    assert (trig(moments, tid)["checked"], trig(moments, tid)["matched"]) == (1, 0)


def test_fail_closed_when_only_one_of_the_two_checks_answers(moments, events, fake_prob):
    add(moments, name="File asks", text="someone asks me for a file", sigs=["*:dm"], semantic=True)
    fake_prob.value = lambda ctx, hyp: 0.99 if hyp.startswith("In this case") else None
    dm(events, "can you send me the deck from yesterday?")
    assert moments.list() == []


def test_fail_closed_on_events_without_text(moments, events, fake_prob):
    add(moments, name="Stars", text="I star something important", sigs=["Thing|*"], semantic=True)
    fake_prob.value = 0.99
    send(events, "Thing", "api.example.com", "/v1/items/7781/star", {"starred": True})
    assert kind(events)["klass"] == "signal" and moments.list() == [] and fake_prob.calls == []


def test_explicit_condition_is_judged_against_the_raw_text(moments, events, fake_prob):
    add(moments, name="Promises", text="I promise to do something in a message I send", sigs=["*:sent"], condition=PROMISE, threshold=0.5)
    fake_prob.value = 0.97
    dm(events, "I'll send you the deck by Friday", kind_="sent")
    assert fake_prob.calls == [("I'll send you the deck by Friday", PROMISE)]
    assert len(moments.list()) == 1 and trig(moments, moments.triggers()[0]["id"])["semantic"] == 1


def test_unchecked_trigger_does_not_touch_the_model(moments, events, fake_prob):
    tid = add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    dm(events, "hello there")
    assert fake_prob.calls == [] and len(moments.checks) == 0 and len(moments.list()) == 1
    assert trig(moments, tid)["checked"] in (0, None)


def test_mixed_triggers_on_one_event(moments, events, fake_prob):
    plain = add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    picky = add(moments, name="File asks", text="someone asks me for a file", sigs=["*:dm"], semantic=True)
    fake_prob.value = 0.01
    dm(events, "haha nice")
    m = moments.list()[0]                                          # the plain trigger still fires
    assert m["gate"] == [{"trigger": picky, "name": "File asks", "p": 0.01, "passed": False, "local": True}]
    assert moments.notes_after(0)["notes"][0]["body"].endswith("→ Any DM") and plain != picky


def test_checks_ring_is_bounded_and_newest_first(moments, events, fake_prob):
    add(moments, name="File asks", text="someone asks me for a file", sigs=["*:dm"], semantic=True)
    fake_prob.value = lambda ctx, hyp: 0.9 if "deck" in ctx else 0.1
    for i in range(310):
        dm(events, f"message {i} " + ("with the deck" if i % 10 == 0 else "about lunch") + " " + "x" * 200)
    assert len(moments.checks) == 300
    assert moments.checks[0]["text"].startswith("message 309 ") and len(moments.checks[0]["text"]) == 140
    assert moments.checks[-1]["text"].startswith("message 10 ")
    t = moments.triggers()[0]
    assert (t["checked"], t["matched"]) == (310, 31) and len(moments.list(limit=500)) == 31


def test_gate_context_describes_the_kind_of_event(moments):
    g = moments.gate_context
    assert g({"sig": "slack:dm", "app": "Slack", "who": "Priya", "text": "hi"}) == ("Message received in Slack from Priya", "hi")
    assert g({"sig": "whatsapp:group", "app": "WhatsApp", "text": "hi"}) == ("Message received in WhatsApp", "hi")
    assert g({"sig": "discord:sent", "app": "Discord", "text": "on it"}) == ("Message I sent in Discord", "on it")
    assert g({"sig": "GitHub|VISIT", "app": "GitHub", "text": "Fix flaky test · Pull Request #12", "fields": {"url": "https://github.com/acme/api/pull/12"}}) \
        == ("Page opened in GitHub", "Fix flaky test · Pull Request #12 (https://github.com/acme/api/pull/12)")
    assert g({"sig": "granola:meeting", "app": "Granola", "text": "bye", "context": {"transcript": "a\nb\nc"}}) == ("Transcript of a meeting", "a\nb\nc")
    assert g({"sig": "Linear|POST x#IssueCreate", "app": "Linear", "label": "issue create", "direction": "out", "text": "t"}) == ("issue create in Linear", "t")
    assert g({"sig": "Thing|WS↓ x#note", "app": "Thing", "label": "note", "direction": "in", "text": "t"}) == ("note received in Thing", "t")


@pytest.mark.parametrize("text, want", [
    ("When someone asks me for a file.", "someone asks me for a file"), ("whenever I push code", "I push code"),
    ("If a customer complains!", "a customer complains"), ("every time a meeting ends", "a meeting ends"),
    ("someone DMs me", "someone DMs me"), ("", ""), (None, ""),
])
def test_derive_statement(text, want):
    assert derive_statement(text) == want


# ---------- with the real local model (clear-cut cases only) ----------
needs_judge = pytest.mark.skipif(decide.judge() is None, reason="local judge model is not in the Hugging Face cache")
needs_embedder = pytest.mark.skipif(decide.embedder() is None, reason="local embedding model is not in the Hugging Face cache")


@needs_judge
def test_real_judge_separates_clear_cases(moments):
    t = {"text": "when someone asks me for a file or document"}
    def p(text):
        return moments.judge({"sig": "slack:dm", "app": "Slack", "who": "Priya", "direction": "in", "text": text}, t)
    assert p("can you send me the deck from yesterday?") >= 0.5 and p("could you share the Q3 spreadsheet pls") >= 0.5
    assert p("haha nice") < 0.2 and p("are we still on for friday?") < 0.2
    promise = {"condition": PROMISE}
    def q(text):
        return moments.judge({"sig": "slack:sent", "app": "Slack", "direction": "out", "text": text}, promise)
    assert q("I'll send you the deck by Friday") >= 0.5 and q("what time is it?") < 0.2


@needs_judge
def test_real_judge_end_to_end(moments, events):
    add(moments, name="File asks", text="When someone asks me for a file or document", sigs=["*:dm"], semantic=True)
    dm(events, "haha nice")
    dm(events, "can you send me the deck from yesterday?", app="WhatsApp", who="Dana")
    ms = moments.list()
    assert [(m["app"], m["text"]) for m in ms] == [("WhatsApp", "can you send me the deck from yesterday?")]
    assert ms[0]["gate"][0]["passed"] and [c["passed"] for c in moments.checks] == [True, False]


# ---------- automatic vs ask first ----------
def test_ask_first_offers_and_does_not_run(moments, events):
    add(moments, name="What do they need?", text="someone DMs me", sigs=["*:dm"], instruction="What do they need from me?")
    dm(events, "can you send me the deck from yesterday? " + "please " * 40)
    m = moments.list()[0]
    assert m["status"] == "new" and m["action"] is None and m["action_status"] is None and m["trigger_id"] is None
    notes = moments.notes_after(0)
    assert notes["seq"] == 1 and len(notes["notes"]) == 1
    n = notes["notes"][0]
    assert n["kind"] == "offer" and n["moment"] == m["id"] and n["title"] == "Priya messaged you · Slack"
    assert n["body"].endswith("\n→ What do they need?") and len(n["body"]) <= 240
    assert moments.notes_after(1)["notes"] == []
    st = moments.status()
    assert st["waiting"] == 1 and st["running"] == 0 and st["armed"] == [] and st["active"] == 1


def test_automatic_runs_the_agent_and_reports(moments, events):
    tid = add(moments, name="Todo catcher", text="I send a message", sigs=["*:sent"], auto=True,
              instruction="Write this as one todo line.")
    dm(events, "I'll send you the deck by Friday", kind_="sent")
    m = moments.list()[0]
    assert m["status"] == "done" and m["action"] == "Todo catcher" and m["trigger_id"] == tid
    done = wait_for(lambda: (moments.get(m["id"]) or {}).get("action_status") not in (None, "running") and moments.get(m["id"]))
    assert done["action_status"] == "ok"
    # the stand-in agent is `cat`: its output is exactly the prompt the real agent would have been given
    assert done["action_result"] == "Slack, DM with Priya. I wrote:\n\nI'll send you the deck by Friday\n\nWrite this as one todo line."
    note = wait_for(lambda: moments.notes_after(0)["notes"])[0]
    assert note["kind"] == "result" and note["title"] == "Todo catcher ran" and note["moment"] == m["id"]
    t = trig(moments, tid)
    assert t["fired"] == 1 and t["last_fired"] and moments.status()["armed"] == ["Todo catcher"]


def test_automatic_incoming_runs_without_an_offer(moments, events):
    add(moments, name="Auto draft", text="someone DMs me", sigs=["*:dm"], auto=True, action="draft")
    dm(events, "can you send me the deck?")
    m = moments.list()[0]
    done = wait_for(lambda: moments.get(m["id"])["action_status"] == "ok" and moments.get(m["id"]))
    assert done["action"] == "Auto draft" and "Draft my reply" in done["action_result"] and "Priya wrote:" in done["action_result"]
    assert [n["kind"] for n in wait_for(lambda: moments.notes_after(0)["notes"])] == ["result"]


def test_outgoing_event_with_an_ask_first_trigger_is_recorded_quietly(moments, events):
    add(moments, name="Note it", text="I send a message", sigs=["*:sent"])
    dm(events, "on my way now", kind_="sent")
    m = moments.list()[0]
    assert m["status"] == "done" and m["action_status"] is None and moments.notes_after(0)["notes"] == []
    assert moments.list(include_sent=False) == [] and moments.status()["waiting"] == 0


def test_agent_saying_nothing_is_not_announced(moments, events, monkeypatch):
    monkeypatch.setenv("AMBIENT_AGENT_CMD", f"echo {NOTHING}")
    add(moments, name="Promises", text="I promise something", sigs=["*:sent"], auto=True)
    dm(events, "wassup", kind_="sent")
    m = moments.list()[0]
    done = wait_for(lambda: moments.get(m["id"])["action_status"] == "ok" and moments.get(m["id"]))
    assert done["action_result"] == NOTHING and moments.notes_after(0)["notes"] == []


def test_missing_agent_is_reported_not_raised(moments, events, monkeypatch):
    monkeypatch.setenv("AMBIENT_AGENT_CMD", "ambient-no-such-agent-binary --flag")
    add(moments, name="Auto", text="someone DMs me", sigs=["*:dm"], auto=True)
    dm(events, "can you send me the deck?")
    m = moments.list()[0]
    done = wait_for(lambda: moments.get(m["id"])["action_status"] == "error" and moments.get(m["id"]))
    assert "isn't installed" in done["action_result"]
    assert wait_for(lambda: moments.notes_after(0)["notes"])[0]["title"] == "Auto failed"


def test_two_automatic_triggers_on_one_event_both_run(moments, events):
    add(moments, name="First", text="I send a message", sigs=["*:sent"], auto=True, instruction="one")
    add(moments, name="Second", text="I send a message", sigs=["slack:sent"], auto=True, instruction="two")
    dm(events, "I'll send you the deck by Friday", kind_="sent")
    titles = wait_for(lambda: len(moments.notes_after(0)["notes"]) >= 2 and {n["title"] for n in moments.notes_after(0)["notes"]}, timeout=1.5)
    assert titles == {"First ran", "Second ran"}


def test_manual_run_and_double_run_guard(moments, events, monkeypatch):
    add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    dm(events, "can you send me the deck?")
    mid = moments.list()[0]["id"]
    monkeypatch.setenv("AMBIENT_AGENT_CMD", "sleep 0.4")
    assert moments.run_action(mid, "ask", "What do they need?") == {"ok": True, "id": mid, "action": "ask"}
    assert moments.run_action(mid, "ask", "again") == {"error": "already running"}
    assert moments.run_action(99999, "ask") == {"error": "no such moment"} and moments.run_trigger(mid, 99999) == {"error": "no such trigger"}
    assert wait_for(lambda: moments.get(mid)["action_status"] != "running")


def test_real_worker_thread_path(store, events):
    m = Moments(store, events)                                     # no SyncQueue: the daemon worker picks it up
    dm(events, "can you send me the deck?")                        # starter trigger "Draft a reply" is bound to *:dm
    got = wait_for(lambda: m.list())
    assert got and got[0]["kind"] == "dm" and got[0]["text"] == "can you send me the deck?"


# ---------- pausing an app ----------
def test_paused_app_stores_nothing_and_makes_no_moments(moments, events, store):
    add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    add(moments, name="Slack things", text="I do something in Slack", sigs=["Slack|*"])
    assert moments.watching("Slack")
    moments.set_watch("Slack", False)
    assert not moments.watching("Slack") and events.muted_apps == {"Slack"}
    assert dm(events, "can you send me the deck?") is None
    send(events, "Slack", "acme.slack.com", "/api/reminders.add", "text=buy+oat+milk+on+the+way+home", ctype="application/x-www-form-urlencoded")
    dm(events, "are you around this afternoon?", app="WhatsApp", who="Dana")
    assert [r["app"] for r in stored(events)] == ["WhatsApp"] and [m["app"] for m in moments.list()] == ["WhatsApp"]
    assert kind(events, "slack:dm")["n"] == 1                      # still counted

    again = Events(store)                                          # the pause survives a restart
    Moments(store, again)
    assert again.muted_apps == {"Slack"}

    moments.set_watch("Slack", True)
    assert events.muted_apps == set() and dm(events, "second try, can you send the deck?") is not None
    assert len(moments.list()) == 2


# ---------- saving triggers ----------
def test_starter_triggers_are_seeded_once(store, events):
    m = Moments(store, events)
    names = [t["name"] for t in m.triggers()]
    assert names == [t["name"] for t in STARTER_TRIGGERS] and len(names) >= 3
    promises = next(t for t in m.triggers() if t["condition"] == PROMISE)
    assert promises["auto"] == 1 and promises["semantic"] == 1 and promises["sigs"] == ["*:sent"] and promises["statement"] == PROMISE
    m.delete_trigger(promises["id"])
    m2 = Moments(store, Events(store))
    assert [t["name"] for t in m2.triggers()] == [n for n in names if n != promises["name"]]     # not re-added


def test_save_trigger_validation_and_defaults(moments):
    assert moments.save_trigger({"text": "x", "action": "draft"}) == {"error": "pick at least one kind of event"}
    assert moments.save_trigger({"text": "x", "sigs": [None, 3, ""], "action": "draft"}) == {"error": "pick at least one kind of event"}
    assert moments.save_trigger({"text": "x", "sigs": ["*:dm"], "action": "ask"}) == {"error": "say what your agent should do"}
    assert moments.save_trigger({"text": "x", "sigs": ["*:dm"], "action": "rm -rf"}) == {"error": "say what your agent should do"}
    tid = moments.save_trigger({"text": "  someone DMs me  ", "sigs": ["*:dm", "slack:mention"], "action": "ask",
                                "instruction": "Tell me what they want. Then stop.", "threshold": 7, "condition": "  "})["id"]
    t = trig(moments, tid)
    assert t["name"] == "Tell me what they want" and t["text"] == "someone DMs me" and t["sigs"] == ["*:dm", "slack:mention"]
    assert t["threshold"] == 0.95 and t["semantic"] == 0 and t["condition"] is None and t["auto"] == 0 and t["enabled"] == 1
    assert [k["label"] for k in t["kinds"]] == ["someone DMs me", "someone @mentions me"] and t["kinds"][0]["app"] == "your chat apps"
    for thr, want in (("abc", 0.5), (0, 0.5), (None, 0.5), (0.01, 0.05), (0.3, 0.3)):
        moments.save_trigger({"id": tid, "sigs": ["*:dm"], "action": "draft", "threshold": thr})
        assert trig(moments, tid)["threshold"] == want
    assert trig(moments, tid)["name"] == "Draft a reply" and len(moments.triggers()) == 1
    tid2 = moments.save_trigger({"sigs": ["*:dm"], "action": "draft", "condition": "The message is about money."})["id"]
    assert trig(moments, tid2)["semantic"] == 1 and trig(moments, tid2)["statement"] == "The message is about money."


def test_save_trigger_accepts_the_old_app_kind_shape(moments, events):
    a = moments.save_trigger({"app": "Slack", "kind": "dm", "action": "draft"})["id"]
    b = moments.save_trigger({"app": "*", "kind": "mention", "action": "draft"})["id"]
    c = moments.save_trigger({"kind": "sent", "action": "draft"})["id"]
    assert [trig(moments, i)["sigs"] for i in (a, b, c)] == [["slack:dm"], ["*:mention"], ["*:sent"]]
    dm(events, "hello there", app="WhatsApp", who="Dana")
    assert moments.list() == []
    dm(events, "hello there")
    assert len(moments.list()) == 1


@pytest.mark.parametrize("seeded", ["3", None])
def test_old_app_kind_triggers_migrate(store, seeded):
    Moments(store, Events(store))                                   # creates today's schema
    with store.lock:
        store.db.execute("DELETE FROM triggers")
        for name, app, kind_, cond, auto in [("Draft a reply", "Slack", "dm", None, 0), ("Mentions", "*", "mention", None, 0),
                                             ("Follow-ups", "Granola", "meeting", None, 0), ("Promises", "*", "sent", PROMISE, 1),
                                             ("WhatsApp", "WhatsApp", "group", None, 0), ("No app", None, "dm", None, 0)]:
            store.db.execute("INSERT INTO triggers (name, app, kind, action, instruction, auto, enabled, created, condition, threshold) "
                             "VALUES (?,?,?,?,?,?,1,0,?,0.5)", (name, app, kind_, "ask", "Do the thing.", auto, cond))
        if seeded is None:
            store.db.execute("DELETE FROM settings WHERE k='triggers_seeded'")
        else:
            store.db.execute("INSERT OR REPLACE INTO settings VALUES ('triggers_seeded', ?)", (seeded,))
        store.db.commit()

    events = Events(store)
    m = Moments(store, events)
    got = {t["name"]: t for t in m.triggers()}
    assert len(got) == 6                                            # migrated in place, no starter set added on top
    assert got["Draft a reply"]["sigs"] == ["slack:dm"] and got["Draft a reply"]["text"] == "someone DMs me"
    assert got["Mentions"]["sigs"] == ["*:mention"] and got["Mentions"]["text"] == "someone @mentions me"
    assert got["Follow-ups"]["sigs"] == ["granola:meeting"] and got["Follow-ups"]["text"] == "a meeting ends"
    assert got["WhatsApp"]["sigs"] == ["whatsapp:group"] and got["No app"]["sigs"] == ["*:dm"]
    p = got["Promises"]
    assert p["sigs"] == ["*:sent"] and p["semantic"] == 1 and p["auto"] == 1 and p["condition"] == PROMISE
    assert p["text"] == "I promise to do something in a message I send"
    assert got["Draft a reply"]["semantic"] == 0 and got["Draft a reply"]["instruction"] == "Do the thing."
    assert m.setting("triggers_seeded") not in (None, "3")

    m.work = type("Q", (), {"put_nowait": lambda self, ev: m._handle(ev)})()
    dm(events, "can you send me the deck?")                         # and they fire
    assert [x["kind"] for x in m.list()] == ["dm"]
    Moments(store, Events(store))                                   # idempotent
    assert len(m.triggers()) == 6


# ---------- suggestions ----------
@needs_embedder
def test_suggest_chat_groups_for_a_sentence_about_what_a_message_says(moments):
    s = moments.suggest("When someone asks me for a file.")
    assert s["statement"] == "someone asks me for a file" and s["model"] == "local"
    picked = [k["sig"] for k in s["kinds"] if k["picked"]]
    assert {"*:dm", "*:mention"} <= set(picked) and s["kinds"][0]["sig"] in ("*:dm", "*:mention")
    assert s["about_content"] is True and s["sure"] is True
    assert all(k["sig"] != "*:sent" for k in s["kinds"] if k["picked"])
    assert all(set(k) >= {"sig", "label", "app", "direction", "klass", "score", "picked"} and "doc" not in k for k in s["kinds"])


@needs_embedder
def test_suggest_my_own_promises_point_at_sent_messages(moments):
    s = moments.suggest("I promise to do something in a message I send")
    assert s["kinds"][0]["sig"] == "*:sent" and s["kinds"][0]["picked"] and s["about_content"] is True


@needs_embedder
def test_suggest_finds_a_seen_kind_named_in_the_sentence(moments, events):
    sig = linear_issue(events, "Export button does nothing on staging")
    visit(events, "Zen", "github.com", "/acme/api/pull/12", "Fix flaky test · Pull Request #12 · acme/api")
    send(events, "Zen", "linear.app", "/api/telemetry", {"n": 1})
    s = moments.suggest("when I create an issue in Linear")
    top = s["kinds"][0]
    assert top["sig"] == sig and top["picked"] and top["app"] == "Linear" and top["n"] == 1
    assert top["example"] == "Export button does nothing on staging" and s["sure"] and s["about_content"] is False
    s = moments.suggest("I open a page on GitHub")
    assert s["kinds"][0]["sig"] == "GitHub|VISIT" and s["kinds"][0]["picked"] and s["about_content"] is False
    assert moments.suggest("I open a GitHub pull request")["about_content"] is True          # which page matters: check the title
    offered = {k["sig"] for q in ("telemetry", "I create an issue in Linear") for k in moments.suggest(q, limit=50)["kinds"]}
    assert not any("telemetry" in x for x in offered)                                         # background kinds are never offered


@needs_embedder
def test_suggest_offers_known_kinds_before_they_are_seen(moments):
    s = moments.suggest("a meeting ends")
    assert s["kinds"][0]["sig"] == "granola:meeting" and s["kinds"][0]["klass"] == "known" and s["kinds"][0]["n"] == 0


def test_suggest_without_the_embedding_model_still_answers(moments, events, monkeypatch):
    monkeypatch.setattr(decide, "embedder", lambda: None)
    linear_issue(events, "Export button does nothing on staging")
    s = moments.suggest("someone asks me for a file")
    assert s["model"] == "unavailable" and s["sure"] is False
    assert [k["sig"] for k in s["kinds"]][:2] == ["*:dm", "*:mention"] and all(k["picked"] for k in s["kinds"][:2])
    assert moments.suggest("") == {"kinds": []} and Moments(moments.store).suggest("anything at all") == {"kinds": []}


@needs_embedder
def test_suggest_app_name_must_match_a_whole_word(moments, events):
    q = "someone asks me to fix a bug"
    before = {k["sig"] for k in moments.suggest(q)["kinds"] if k["picked"]}
    assert {"*:dm", "*:mention"} <= before
    send(events, "Zen", "x.com", "/i/api/graphql/a1b2C3d4E5f6G7h8I9j0Kl/CreateTweet", {"variables": {"tweet_text": "Writing tests at midnight"}})
    after = moments.suggest(q)
    assert {k["sig"] for k in after["kinds"] if k["picked"]} == before and after["about_content"] is True


# ---------- previews ----------
def test_preview_counts(moments, events, fake_prob):
    for i, text in enumerate(["can you send me the deck?", "haha nice", "send the invoice pdf please", "lunch?"]):
        dm(events, text, key=f"p{i}")
    dm(events, "are you around?", app="WhatsApp", who="Dana")
    dm(events, "on my way", kind_="sent")

    plain = moments.preview({"sigs": ["slack:dm"]})
    assert (plain["checked"], plain["matched"], plain["semantic"]) == (4, 4, False)
    assert all(s["passed"] and s["p"] is None for s in plain["samples"]) and fake_prob.calls == []
    assert moments.preview({"sigs": ["*:dm"]})["checked"] == 5 and moments.preview({"sigs": ["*:dm"]}, limit=2)["checked"] == 2

    fake_prob.value = lambda ctx, hyp: 0.9 if ("deck" in ctx or "invoice" in ctx) else 0.2
    sem = moments.preview({"sigs": ["slack:dm"], "text": "when someone asks me for a file", "semantic": True})
    assert (sem["checked"], sem["matched"], sem["semantic"]) == (4, 2, True)
    assert [(s["text"], s["passed"], s["p"]) for s in sem["samples"]] == [
        ("send the invoice pdf please", True, 0.9), ("can you send me the deck?", True, 0.9),
        ("lunch?", False, 0.2), ("haha nice", False, 0.2)]
    assert sem["samples"][0]["who"] == "Priya" and sem["samples"][0]["app"] == "Slack"
    strict = moments.preview({"sigs": ["slack:dm"], "text": "someone asks me for a file", "semantic": True, "threshold": 0.95})
    assert strict["matched"] == 0
    fake_prob.value = None                                          # model unavailable: a preview matches nothing either
    assert moments.preview({"sigs": ["slack:dm"], "condition": "It asks for a file."})["matched"] == 0

    assert moments.preview({"sigs": []}) == {"checked": 0, "matched": 0, "samples": []}
    assert moments.preview({"sigs": ["nothing:here"]})["checked"] == 0
    assert moments.list() == []                                     # a preview never creates moments


# ---------- describing kinds ----------
def test_describe_sig(moments, events):
    sig = linear_issue(events, "Export button does nothing on staging")
    assert moments.describe_sig("*:dm") == {"sig": "*:dm", **GROUPS["*:dm"]}
    assert moments.describe_sig(sig) == {"sig": sig, "label": "issue create", "app": "Linear", "direction": "out", "source": "network"}
    assert moments.describe_sig("whatsapp:group") == {"sig": "whatsapp:group", "label": "someone writes in a group chat", "app": "Whatsapp"}
    assert moments.describe_sig("Gone|POST api.gone.example/v1/things")["app"] == "Gone"


# ---------- MCP ----------
def test_mcp_surface(moments, events):
    add(moments, name="Any DM", text="someone DMs me", sigs=["*:dm"])
    dm(events, "can you send me the deck?")
    call = lambda method, **params: mcp_handle(moments, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})  # noqa: E731
    assert call("initialize", protocolVersion="2025-06-18")["result"]["serverInfo"]["name"] == "ambient"
    assert {t["name"] for t in call("tools/list")["result"]["tools"]} == {"list_moments", "wait_for_moment"}
    assert mcp_handle(moments, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    rows = json.loads(call("tools/call", name="list_moments", arguments={})["result"]["content"][0]["text"])
    assert len(rows) == 1 and rows[0]["text"] == "can you send me the deck?" and rows[0]["kind"] == "dm" and "context" not in rows[0]
    waited = json.loads(call("tools/call", name="wait_for_moment", arguments={"after_id": 0, "timeout_seconds": 0.05})["result"]["content"][0]["text"])
    assert [r["id"] for r in waited] == [rows[0]["id"]]
    none = json.loads(call("tools/call", name="wait_for_moment", arguments={"timeout_seconds": 0.05})["result"]["content"][0]["text"])
    assert none == []
    assert call("tools/call", name="nope")["error"]["code"] == -32602 and call("bogus")["error"]["code"] == -32601


# ---------- several automatic triggers, and what pruning must keep ----------
def test_each_further_automatic_trigger_gets_its_own_moment(moments, events):
    a = add(moments, name="First", text="someone DMs me", sigs=["*:dm"], auto=True, instruction="one")
    b = add(moments, name="Second", text="someone DMs me on Slack", sigs=["slack:dm"], auto=True, instruction="two")
    add(moments, name="Ask", text="someone DMs me", sigs=["*:dm"], instruction="three")
    ev = dm(events, "can you send me the deck?")
    wait_for(lambda: all(m["action_status"] == "ok" for m in moments.list()))
    ms = sorted(moments.list(), key=lambda m: m["id"])
    assert [(m["action"], m["trigger_id"], m["status"], m["text"]) for m in ms] == [
        ("First", a, "new", "can you send me the deck?"), ("Second", b, "done", "can you send me the deck?")]
    assert ms[0]["action_result"].endswith("\n\none") and ms[1]["action_result"].endswith("\n\ntwo")
    assert moments.status()["waiting"] == 1                         # one thing happened, one thing waits
    assert [t["fired"] for t in moments.triggers()] == [1, 1, 0]
    moments._handle(ev)                                             # the same event delivered again runs nothing twice
    assert len(moments.list()) == 2
    assert sorted(n["kind"] for n in wait_for(lambda: len(moments.notes_after(0)["notes"]) >= 3 and moments.notes_after(0)["notes"])) \
        == ["offer", "result", "result"]


def test_pruning_never_forgets_a_kind_a_trigger_is_bound_to(moments, events):
    events._clock = 1_760_000_000.0
    bound = linear_issue(events, "Export button does nothing on staging")
    send(events, "Zen", "linear.app", "/api/issue/star", {"starred": True})
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    tid = add(moments, name="New issue", text="I create an issue in Linear", sigs=[bound], enabled=False)
    add(moments, name="Thing", text="anything in Thing", sigs=["Thing|*"])
    events._clock += 100 * 86400
    assert events.keep == moments.bound_sig and events.prune() == 1
    assert sorted(t["app"] for t in events.types.values()) == ["Linear", "Thing"] and bound in events.types
    moments.delete_trigger(tid)                                     # no longer bound: it goes with the next prune
    assert events.prune() == 1 and [t["app"] for t in events.types.values()] == ["Thing"]
