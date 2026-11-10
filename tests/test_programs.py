"""Wide events, trigger programs and decision providers. Synthetic traffic only; Jev (the ZDR provider) is
always replaced by a fake HTTP function here, so no test sends anything anywhere."""
import json

import pytest

from engine import deciders
from engine import triggers as P
from engine.events import WIDE_MAX_BYTES, WIDE_MAX_FIELDS, flatten_wide, path_params
from helpers import fake_secrets, send, stored

CAL = "/calendar/v3/calendars/primary/events"


def cal_event(events, title, attendees=(), extra=None):
    body = {"summary": title, "description": f"Agenda for {title.lower()} and next steps",
            "attendees": [{"email": a} for a in attendees], **(extra or {})}
    send(events, "Zen", "www.googleapis.com", CAL, body, resp=json.dumps({"status": "confirmed", "id": "evt_9x8y7z6w5v"}))


def dm(events, text, app="Slack", who="Priya", key=None):
    return events.emit(app=app, sig=f"{app.lower()}:dm", label="someone DMs me", direction="in", klass="incoming",
                       who=who, place=f"DM with {who}", text=text, key=key or f"{app}:{text}",
                       context={"channel": "D024BE91L"})


class FakeJev:
    """Stands in for deciders.http_post. `answer(question) -> probability`; records every request body."""

    def __init__(self, answer=0.9, fail=None):
        self.answer, self.fail, self.bodies = answer, fail, []

    def __call__(self, url, body, key, timeout=10):
        self.bodies.append(json.loads(json.dumps(body)))
        if self.fail:
            raise deciders.Unavailable(self.fail)
        out = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                v = self.answer(qid, q, body["state"]) if callable(self.answer) else self.answer
                out[qid] = {"type": "noul", "noul": v}
            else:
                keys = list(q["criteria"])
                out[qid] = {"type": "choice", "choice": keys[0], "confidence": 0.9,
                            "probabilities": {k: (0.9 if i == 0 else 0.1 / (len(keys) - 1)) for i, k in enumerate(keys)}}
        return {"model": "jev-test", "answers": out, "usage": {"input_tokens": 10, "output_tokens": 1}}


@pytest.fixture
def jev(monkeypatch):
    monkeypatch.setenv("AMBIENT_TYPESAFE_KEY", "test-not-a-key")
    fake = FakeJev()
    monkeypatch.setattr(deciders, "http_post", fake)
    return fake


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("AMBIENT_TYPESAFE_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(deciders, "http_post", lambda *a, **k: pytest.fail("Jev must not be called"))


# ---------- wide events ----------
def test_a_request_becomes_a_wide_event(events):
    cal_event(events, "Quarterly planning", ["dana@acme.example", "lee@acme.example"],
              extra={"password": "hunter2", "apiKey": fake_secrets()["github_token"]})
    r = events.recent(limit=1)[0]
    w = r["wide"]
    assert w["app"] == "Googleapis" and w["via"] == "Zen" and w["direction"] == "out" and w["method"] == "POST"
    assert w["host"] == "www.googleapis.com" and w["path_template"] == CAL and w["transport"] == "http"
    assert w["title"] == "Quarterly planning" and w["recipients"] == "dana@acme.example, lee@acme.example"
    assert w["recipients.count"] == 2 and w["fields.attendees.count"] == 2 and w["resp.status"] == "confirmed"
    assert w["text"].startswith("Agenda for") and w["kind"] == r["sig"] and w["status"] == 200
    assert not any("password" in k.lower() or "apikey" in k.lower() for k in w)          # credential keys dropped
    assert fake_secrets()["github_token"] not in json.dumps(w)


def test_secrets_in_values_are_masked_and_rows_are_bounded(events):
    body = {f"field{i}": f"value number {i} " + "x" * 400 for i in range(120)}
    body["note"] = "deploy key " + fake_secrets()["aws_access_key"] + " please rotate"
    send(events, "Thing", "api.thing.example", "/v1/notes", body)
    row = events.store.q("SELECT wide FROM events ORDER BY id DESC LIMIT 1")[0]
    assert len(row["wide"]) <= WIDE_MAX_BYTES + 200                 # bytes are counted before UTF-8 escaping
    w = json.loads(row["wide"])
    assert len(w) <= WIDE_MAX_FIELDS and all(len(v) <= 160 for v in w.values() if isinstance(v, str))
    assert fake_secrets()["aws_access_key"] not in row["wide"]


def test_flatten_wide_shapes():
    w = flatten_wide({"event": {"attendees": [{"email": "a@x.example"}, {"email": "b@x.example"}], "allDay": False,
                                "reminders": {"overrides": []}, "tags": ["work", "q3"]},
                      "users": {"U024BE7LH": {"name": "Priya"}}, "token": "abc", "n": 3,
                      "q": "query Foo { bar }", "blob": "QmFzZTY0QmxvYkV4YW1wbGVUaGF0SXNMb25n"})
    assert w["fields.event.attendees.count"] == 2 and w["fields.event.attendees[].email"] == "a@x.example, b@x.example"
    assert w["fields.event.allDay"] is False and w["fields.event.reminders.overrides.count"] == 0
    assert w["fields.event.tags"] == "work, q3" and w["fields.event.tags.count"] == 2 and w["fields.n"] == 3
    assert w["fields.users.*.name"] == "Priya"                        # a key that is data becomes *
    assert "fields.token" not in w and "fields.q" not in w and "fields.blob" not in w


def test_path_params_are_named_after_their_segment():
    assert path_params("/repos/acme/api/issues/42", "/repos/{name}/{name}/issues/{id}") == {
        "path.repo": "acme", "path.repo_2": "api", "path.issue": "42"}
    assert path_params("/a/b", "/a/b/c") == {}


def test_kinds_learn_their_schema(events):
    cal_event(events, "Planning", ["a@x.example"])
    cal_event(events, "Retro", [])
    k = next(k for k in events.schemas() if k["sig"].startswith("Googleapis|POST"))
    f = k["fields"]
    assert f["fields.attendees.count"]["type"] == "number" and f["fields.attendees.count"]["n"] == 2
    assert f["title"]["type"] == "text" and f["text"]["n"] == 2 and k["direction"] == "out"


def test_curated_events_are_wide_too(events):
    dm(events, "can you send me the deck?")
    w = events.recent(limit=1)[0]["wide"]
    assert (w["who"], w["place"], w["op"], w["transport"], w["direction"]) == ("Priya", "DM with Priya", "dm", "curated", "in")
    assert w["fields.context.channel"] == "D024BE91L"


def test_paused_apps_store_no_wide_event(moments, events):
    moments.set_watch("Googleapis", False)
    cal_event(events, "Planning", ["a@x.example"])
    assert stored(events) == []


def test_sign_in_requests_never_become_wide_events(events):
    send(events, "Zen", "api.thing.example", "/v1/auth/login", {"username": "dana", "password": "hunter2"})
    send(events, "Zen", "api.thing.example", "/v1/payments/charge", {"amount": 1200, "note": "monthly plan for the team"})
    assert stored(events) == []


# ---------- programs: parsing, matching, explaining ----------
def test_conditions_parse_from_short_strings():
    assert P.parse_cond("direction=out") == {"field": "direction", "op": "eq", "value": "out"}
    assert P.parse_cond("fields.event.attendees.count>0") == {"field": "fields.event.attendees.count", "op": "gt", "value": 0}
    assert P.parse_cond("op~create") == {"field": "op", "op": "contains", "value": "create"}
    assert P.parse_cond("text contains invoice") == {"field": "text", "op": "contains", "value": "invoice"}
    assert P.parse_cond("who exists") == {"field": "who", "op": "exists", "value": None}
    assert P.parse_cond("app in Gmail, Outlook") == {"field": "app", "op": "in", "value": ["Gmail", "Outlook"]}
    assert P.parse_cond("nonsense") is None and P.parse_cond({"field": "x", "op": "rm"}) is None


def test_matching_over_wide_fields():
    prog = P.normalize({"match": {"where": ["direction=out", "fields.*attendees.count>0"], "any": ["content contains planning", "title contains retro"]}})
    w = {"direction": "out", "fields.attendees.count": 2, "title": "Q3 planning", "kind": "Cal|POST x"}
    assert P.matches(prog, w)
    assert not P.matches(prog, {**w, "fields.attendees.count": 0})
    assert not P.matches(prog, {**w, "direction": "in"})
    assert not P.matches(prog, {**w, "title": "lunch"})
    only = P.normalize({"match": {"kinds": ["Cal|*"], "apps": ["cal"]}})
    assert P.matches(only, {"kind": "Cal|POST x", "app": "Cal"}) and not P.matches(only, {"kind": "Other|POST x", "app": "Cal"})


def test_a_program_round_trips_and_explains_itself():
    raw = {"when": "a customer emails about a refund",
           "match": {"apps": ["Gmail"], "where": ["direction=in"]},
           "check": {"condition": "the email asks for a refund", "threshold": 0.7, "run": "zdr"},
           "do": {"instruction": "draft a reply and file a ticket"}, "junk": 1}
    p = P.normalize(raw)
    assert "junk" not in p and P.normalize(json.dumps(p)) == p
    assert p["do"]["steps"] == ["draft a reply", "file a ticket"]
    assert p["explain"] == ("Fires on anything in Gmail where it reaches you, if “the email asks for a refund” holds "
                            "(checked by Jev, zero data retention, at 70% or more); then your agent will draft a reply "
                            "and file a ticket.")
    assert P.normalize({"check": {"condition": "x", "run": "openai"}})["check"]["run"] == "device"


def test_one_prompt_split():
    assert P.split_intent("Whenever a customer emails about a refund, draft a reply and file a ticket.") == (
        "a customer emails about a refund", "draft a reply and file a ticket")
    assert P.split_intent("when someone asks me for a file") == ("someone asks me for a file", "")
    assert P.split_intent("if I create an invoice then remind me in a week") == ("I create an invoice", "remind me in a week")
    assert P.says_any_app("in any app: I send something about an invoice")
    assert P.do_from("draft a reply") == {"action": "draft", "instruction": "", "steps": []}


def test_check_state_holds_only_the_named_fields():
    p = P.normalize({"match": {"apps": ["X"]}, "check": {"condition": "c", "fields": ["text", "fields.*.count"]}})
    st = P.check_state(p, {"text": "hello", "who": "Dana", "recipients": "a@x", "fields.a.count": 2, "host": "h"})
    assert st == {"text": "hello", "fields.a.count": 2}


# ---------- compiling ----------
def test_compile_on_this_device_never_calls_jev(moments, events, no_key):
    cal_event(events, "Planning", ["a@x.example"])
    r = moments.compile("whenever I create a calendar event with attendees, tell me who is coming")
    assert r["written_by"] == "device" and r["written_by_label"] == "written on this device"
    p = r["program"]
    assert {"field": "fields.*attendees.count", "op": "gt", "value": 0} in p["match"]["where"]
    assert p["do"]["instruction"] == "Tell me who is coming." and r["explain"].startswith("Fires on ")


def test_compile_can_express_kinds_never_seen(moments, events, no_key):
    r = moments.compile("in any app, I send something that mentions an invoice")
    p = r["program"]
    assert p["match"]["kinds"] == [] and p["match"]["apps"] == []
    assert {"field": "direction", "op": "eq", "value": "out"} in p["match"]["where"]
    assert p["check"]["condition"] == "I send something that mentions an invoice" and p["check"]["run"] == "device"
    r2 = moments.compile("I close an issue in Linear")                 # Linear never seen: still only Linear
    assert r2["program"]["match"]["apps"] == ["Linear"]


def test_compile_with_jev_sends_masked_examples_only(moments, events, jev):
    cal_event(events, "Board meeting with Dana Whitfield", ["dana.whitfield@acme.example"])
    jev.answer = lambda qid, q, st: 0.95 if ".k." in qid and "calendar" in json.dumps(st["kinds"][qid.split(".k.")[1]]).lower() else 0.1
    r = moments.compile("whenever I create a calendar event, summarise it", using="zdr")
    assert r["written_by"] == "zdr" and r["written_by_label"] == "written with Jev, zero data retention"
    sent = json.dumps(jev.bodies)
    assert "Dana" not in sent and "dana.whitfield" not in sent and "Board meeting" not in sent     # values masked
    assert "<email>" in sent or "<text" in sent
    assert r["program"]["match"]["kinds"] and all(k.startswith("Googleapis|") for k in r["program"]["match"]["kinds"])
    assert len(jev.bodies) == 1                                        # one request for the whole compile


def test_compile_falls_back_to_the_device_when_jev_is_down(moments, events, jev):
    jev.fail = "network: URLError"
    r = moments.compile("someone asks me for a file", using="zdr")
    assert r["written_by"] == "device" and "unreachable" in r["note"]
    assert r["program"]["match"]["kinds"]


# ---------- running programs ----------
def save(moments, prog, **kw):
    r = moments.save_trigger({"name": "T", "action": "ask", "instruction": "Summarise.", "program": prog, **kw})
    assert r.get("ok"), r
    return r["id"]


def test_program_trigger_fires_on_unseen_kinds_by_fields(moments, events, fake_prob):
    save(moments, {"match": {"where": ["direction=out", "fields.*attendees.count>0"]}})
    cal_event(events, "Retro", [])
    cal_event(events, "Planning", ["a@x.example"])
    send(events, "Zen", "api.othercal.example", "/v2/events", {"title": "Sync", "attendees": [{"name": "Lee"}],
                                                                 "notes": "weekly sync with the team"})
    assert sorted(m["text"][:20] for m in moments.list()) == ["Agenda for planning ", "weekly sync with the"]
    assert fake_prob.calls == []


def test_device_check_records_who_checked(moments, events, fake_prob, no_key):
    tid = save(moments, {"match": {"kinds": ["*:dm"]}, "check": {"condition": "someone asks me for a file", "threshold": 0.6}})
    fake_prob.value = 0.9
    dm(events, "can you send me the deck?")
    g = moments.list()[0]["gate"][0]
    assert g["passed"] and g["run"] == "device" and g["by"] == "checked on this device" and g["local"] is True
    t = next(t for t in moments.triggers() if t["id"] == tid)
    assert t["checked_by"] == "checked on this device" and t["check_on"] == "device" and t["explain"]


def test_zdr_check_sends_only_the_named_fields(moments, events, jev, fake_prob):
    tid = save(moments, {"match": {"kinds": ["*:dm"]},
                         "check": {"condition": "someone asks me for a file", "run": "zdr", "fields": ["text"]}})
    jev.answer = 0.88
    dm(events, "can you send me the deck?", who="Priya Raman")
    g = moments.list()[0]["gate"][0]
    assert g["run"] == "zdr" and g["by"] == "checked by Jev, zero data retention" and g["p"] == 0.88 and not g["local"]
    assert len(jev.bodies) == 1 and jev.bodies[0]["state"] == {"event": {"text": "can you send me the deck?"}}
    assert "Priya" not in json.dumps(jev.bodies) and fake_prob.calls == []
    assert next(t for t in moments.triggers() if t["id"] == tid)["checked_by"] == "checked by Jev, zero data retention"


def test_zdr_unreachable_fails_closed_by_default(moments, events, jev, fake_prob):
    save(moments, {"match": {"kinds": ["*:dm"]}, "check": {"condition": "someone asks me for a file", "run": "zdr"}}, auto=True)
    jev.fail = "HTTP 503"
    dm(events, "can you send me the deck?")
    assert moments.list() == [] and fake_prob.calls == []
    c = moments.checks[0]
    assert c["passed"] is False and c["p"] is None and "HTTP 503" in c["error"] and c["run"] == "zdr"


def test_zdr_unreachable_can_fall_back_to_the_device(moments, events, jev, fake_prob):
    moments.set_setting("zdr_fallback", "device")
    save(moments, {"match": {"kinds": ["*:dm"]}, "check": {"condition": "someone asks me for a file", "run": "zdr"}})
    jev.fail = "network: timeout"
    fake_prob.value = 0.9
    dm(events, "can you send me the deck?")
    g = moments.list()[0]["gate"][0]
    assert g["run"] == "device" and g["by"] == "checked on this device (Jev unreachable)" and g["passed"]


def test_no_key_means_zdr_checks_fail_closed(moments, events, no_key, fake_prob):
    save(moments, {"match": {"kinds": ["*:dm"]}, "check": {"condition": "someone asks me for a file", "run": "zdr"}})
    dm(events, "can you send me the deck?")
    assert moments.list() == [] and "no TypeSafe key" in moments.checks[0]["error"]
    assert "fail closed" in moments.save_trigger({"action": "draft", "program": {"match": {"apps": ["X"]}, "check": {"condition": "c", "run": "zdr"}}})["warning"]


def test_paused_apps_are_never_sent_to_jev(moments, events, jev):
    p = P.normalize({"match": {"kinds": ["*:dm"]}, "check": {"condition": "x", "run": "zdr"}})
    moments.events.muted_apps.add("Slack")
    t = {"id": 1, "_program": p}
    out = moments._check_programs({"sig": "slack:dm", "app": "Slack", "text": "hi there", "direction": "in"}, [t])
    assert out[1].p is None and out[1].error == "paused" and jev.bodies == []


def test_sentence_triggers_can_choose_where_the_check_runs(moments, events, jev, fake_prob):
    tid = moments.save_trigger({"name": "Files", "text": "someone asks me for a file", "sigs": ["*:dm"], "semantic": True,
                                "action": "draft", "check_on": "zdr"})["id"]
    t = next(t for t in moments.triggers() if t["id"] == tid)
    assert t["check_on"] == "zdr" and t["program"]["check"]["condition"] == "someone asks me for a file"
    dm(events, "can you send me the deck?")
    assert moments.list()[0]["gate"][0]["run"] == "zdr" and fake_prob.calls == []


def test_resaving_without_the_program_keeps_it(moments, events):
    tid = save(moments, {"match": {"where": ["direction=out", "fields.*attendees.count>0"]}})
    moments.save_trigger({"id": tid, "name": "T", "action": "ask", "instruction": "Summarise.", "sigs": ["*"], "enabled": False})
    t = next(t for t in moments.triggers() if t["id"] == tid)
    assert t["enabled"] == 0 and t["program"]["match"]["where"][1]["field"] == "fields.*attendees.count"


def test_a_program_that_matches_everything_is_refused(moments):
    assert "everything" in moments.save_trigger({"action": "draft", "program": {"match": {}}})["error"]


def test_old_sentence_triggers_are_unchanged(moments, events, fake_prob):
    tid = moments.save_trigger({"name": "Files", "text": "someone asks me for a file", "sigs": ["*:dm"], "semantic": True,
                                "action": "draft"})["id"]
    t = next(t for t in moments.triggers() if t["id"] == tid)
    assert t["program"] is None and t["check_on"] == "device" and t["checked_by"] == "checked on this device"


# ---------- backtests ----------
def test_preview_program_on_device(moments, events, fake_prob):
    for t in ("can you send me the deck?", "haha nice", "send the invoice pdf please"):
        dm(events, t)
    cal_event(events, "Planning", ["a@x.example"])
    fake_prob.value = lambda ctx, hyp: 0.9 if ("deck" in ctx or "invoice" in ctx) else 0.1
    r = moments.preview({"program": {"match": {"where": ["direction=in"]}, "check": {"condition": "someone asks me for a file"}}})
    assert (r["checked"], r["matched"], r["by"], r["run"]) == (3, 2, "checked on this device", "device")
    plain = moments.preview({"program": {"match": {"where": ["fields.*attendees.count>0"]}}})
    assert (plain["checked"], plain["matched"]) == (1, 1) and "attendees" in plain["explain"]
    assert moments.list() == []


def test_preview_program_on_jev_is_one_bounded_request(moments, events, jev):
    for i in range(30):
        dm(events, f"message {i} about the deck", key=f"k{i}")
    jev.answer = 0.8
    r = moments.preview({"program": {"match": {"kinds": ["*:dm"]}, "check": {"condition": "someone asks me for a file",
                                                                             "run": "zdr", "fields": ["text"]}}})
    assert r["checked"] == moments.ZDR_PREVIEW_MAX and r["matched"] == moments.ZDR_PREVIEW_MAX and len(jev.bodies) == 1
    assert len(jev.bodies[0]["questions"]) == moments.ZDR_PREVIEW_MAX and r["by"] == "checked by Jev, zero data retention"


# ---------- providers ----------
def test_key_is_read_from_a_file_and_never_shown(tmp_path, monkeypatch, moments):
    monkeypatch.delenv("AMBIENT_TYPESAFE_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("OTHER=1\nTYPESAFE_API_KEY=ts_test_value_123\n")
    moments.set_setting("zdr_key_file", str(f))
    assert deciders.Jev(moments.setting).available()
    st = deciders.status(moments.setting)
    assert "ts_test_value_123" not in json.dumps(st) and st["providers"][1]["available"] is True
    assert st["fallback"] == "closed" and st["providers"][0]["default"] is True


def test_mask_example():
    m = deciders.mask_example
    assert (m("recipients", "a@b.example"), m("x", "https://x.example/a"), m("x", 3), m("method", "POST"),
            m("title", "Board meeting with Dana"), m("resp.id", "evt_9x8y7z6w5v")) == (
        "<email>", "<url>", "<number>", "POST", "<text, 4 words>", "<id>")


def test_api_routes(moments, events, no_key):
    from engine import triggers_api
    cal_event(events, "Planning", ["a@x.example"])
    body, code = triggers_api.get("/api/schema", {}, moments, events)
    assert code == 200 and body["kinds"][0]["fields"]
    body, code = triggers_api.post("/api/triggers/compile", {"text": "when I create a calendar event with attendees",
                                                             "preview": True}, moments, events)
    assert code == 200 and body["preview"]["checked"] >= 1 and body["program"]["v"] == 1
    body, _ = triggers_api.post("/api/triggers/explain", {"program": {"match": {"apps": ["Gmail"]}}}, moments, events)
    assert body["explain"] == "Fires on anything in Gmail."
    body, _ = triggers_api.post("/api/deciders", {"fallback": "device", "compile_with": "nonsense"}, moments, events)
    assert body["fallback"] == "device" and body["compile_with"] == "device"
    assert triggers_api.post("/api/triggers/compile", {}, moments, events)[1] == 400
    assert triggers_api.get("/api/nope", {}, moments, events) is None


def test_jev_compile_keeps_to_the_named_app_and_to_fields_its_kinds_carry(moments, events, jev):
    send(events, "Notion", "www.notion.so", "/api/v3/saveTransactions",
         {"operations": [{"command": "set", "args": {"title": "Meeting notes for the hiring plan"}}]})
    for t in ("the bot is down again", "can you send me the deck?"):
        events.emit(app="Discord", sig="discord:dm", label="someone DMs me", direction="in", klass="incoming",
                    who="kai", text=t, key=t)

    def answer(qid, q, st):
        if ".k." in qid:
            kind = st["kinds"][qid.split(".k.")[1]]
            return 0.9 if kind["app"].lower() == "discord" else 0.85 if kind["app"] == "your chat apps" else 0.1
        if ".a" in qid:
            return 0.95 if q["instructions"]["app"] == "Discord" else 0.05
        if ".f." in qid:
            return 0.95                                   # Jev says yes to the Notion count field: must not stick
        return 0.1
    jev.answer = answer
    p = moments.compile("someone messages me on Discord", using="zdr")["program"]
    assert p["match"]["kinds"] and all(k.startswith("discord:") for k in p["match"]["kinds"])
    assert p["match"]["where"] == []
