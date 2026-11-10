"""Classification: action / incoming / signal / background / read, what gets stored, and what never does."""
import json
import time

import pytest

from engine.events import Events
from helpers import FORM, UUID, fake_secrets, kind, kinds, send, stored, visit

T0 = 1_760_000_000.0


def at(events, t):
    events._clock = t


# ---------- background: telemetry, heartbeats, presence ----------
@pytest.mark.parametrize("host, path", [
    ("api.example.com", "/api/v1/telemetry"), ("api.example.com", "/v2/track"), ("api.example.com", "/collect?v=2"),
    ("o450123.ingest.sentry.io", "/api/4501234/envelope/"), ("api.example.com", "/heartbeat"),
    ("api.example.com", "/api/metrics"), ("api.example.com", "/v1/events/batch"), ("browser-intake-datadoghq.com", "/api/v2/rum"),
    ("api.example.com", "/api/presence.set"), ("api.example.com", "/api/v1/feature-flags/evaluate"),
    ("api.segment.io", "/v1/batch"), ("api.example.com", "/log_event"),
])
def test_telemetry_is_background_and_never_stored(events, host, path):
    send(events, "Thing", host, path, {"event": "button_clicked", "message": "user clicked the big red button", "session_id": UUID})
    t = kind(events)
    assert t["klass"] == "background" and t["n"] == 1
    assert stored(events) == []


@pytest.mark.parametrize("frame", [
    {"type": "ping"}, {"type": "pong", "reply_to": 4}, {"type": "presence_change", "user": "U024BE7LH", "presence": "away"},
    {"type": "user_typing", "channel": "C024BE91L", "user": "U024BE7LH"}, {"op": 0, "t": "TYPING_START", "d": {"user_id": "1"}},
    {"op": 0, "t": "PRESENCE_UPDATE", "d": {"status": "online"}}, {"event": "heartbeat", "ref": "12"},
    {"type": "hello", "region": "eu-west-2"},
])
def test_websocket_chatter_is_background(events, frame):
    for incoming in (True, False):
        events.ws("Thing", "ws.example.com", "/socket", json.dumps(frame), not incoming)
    assert {t["klass"] for t in kinds(events)} == {"background"}
    assert stored(events) == []


@pytest.mark.parametrize("host, path, body", [
    ("api.staybooker.com", "/v1/hotels/88213/reviews", {"text": "Lovely stay, the staff were great"}),
    ("api.example.com", "/api/collections", {"title": "Reading list for the offsite"}),
    ("api.example.com", "/v1/shipments/5512/tracking/notes", {"note": "Left with the neighbour at number twelve"}),
    ("api.example.com", "/v1/playlists/3cEYpjA9oz9GiPac4AsH4n/tracks", {"description": "Songs for the long drive north"}),
])
def test_ordinary_words_that_contain_a_telemetry_word(events, host, path, body):
    send(events, "Thing", host, path, body)
    assert kind(events)["klass"] == "action"


# ---------- read ----------
def test_get_is_read_and_not_stored(events):
    for i in range(3):
        send(events, "Thing", "api.example.com", f"/v1/notes/{100 + i}?expand=author", method="GET")
    send(events, "Thing", "api.example.com", "/v1/notes", method="HEAD")
    send(events, "Thing", "api.example.com", "/v1/notes", method="OPTIONS")
    assert {t["klass"] for t in kinds(events)} == {"read"}
    assert kind(events, "GET")["n"] == 3
    assert stored(events) == []


@pytest.mark.parametrize("path, body, ctype", [
    ("/api/conversations.history", "channel=C024BE91L&limit=28&ignore_replies=true", FORM),
    ("/api/users.info", "user=U024BE7LH", FORM),
    ("/api/client.counts", "thread_counts_by_channel=true", FORM),
    ("/rpc", {"jsonrpc": "2.0", "id": 3, "method": "getUser", "params": {"id": 4412}}, "application/json"),
    ("/rpc", {"jsonrpc": "2.0", "id": 4, "method": "notes.list", "params": {"folder": "inbox"}}, "application/json"),
    ("/graphql", {"operationName": "InboxQuery", "query": "query InboxQuery($n: Int) { inbox(first: $n) { id } }", "variables": {"n": 20}}, "application/json"),
    ("/api/v3/syncRecordValues", {"requests": [{"pointer": {"table": "block", "id": UUID}, "version": -1}]}, "application/json"),
    ("/api/autocomplete", {"q": "quarterly budget review"}, "application/json"),
    ("/api/v1/suggestions/fetch", {"prefix": "how do I reset my"}, "application/json"),
])
def test_post_that_only_fetches_is_read(events, path, body, ctype):
    send(events, "Thing", "api.example.com", path, body, ctype=ctype)
    assert kind(events)["klass"] == "read"
    assert stored(events) == []


def test_read_looking_name_with_a_write_in_it_stays_an_action(events):
    send(events, "Todoist", "api.todoist.com", "/sync/v9/sync", "commands=" + json.dumps(
        [{"type": "item_add", "uuid": UUID, "args": {"content": "Book the dentist for next week"}}]), ctype=FORM)
    t = kind(events)
    assert t["klass"] == "action" and "Book the dentist" in t["example"]


def test_create_endpoint_named_with_a_read_word(events):
    send(events, "Thing", "api.example.com", "/v1/comments/8812/replies", {"body": "Agreed, let's ship it on Monday"})
    assert kind(events)["klass"] == "action"


# ---------- by rate ----------
def test_steady_textless_chatter_becomes_background(events):
    for i in range(14):
        at(events, T0 + i * 2)
        send(events, "Thing", "api.example.com", "/v1/cursor/position", {"x": i, "y": 4, "doc": UUID})
    t = kind(events)
    assert t["klass"] == "background" and t["n"] == 14
    before = len(stored(events))
    at(events, T0 + 30)
    send(events, "Thing", "api.example.com", "/v1/cursor/position", {"x": 99, "y": 4, "doc": UUID})
    assert len(stored(events)) == before                 # once it is background nothing more is kept


def test_occasional_textless_write_is_a_signal(events):
    for i in range(14):
        at(events, T0 + i * 90)
        send(events, "Thing", "api.example.com", "/v1/items/7781/star", {"starred": True})
    t = kind(events)
    assert t["klass"] == "signal" and t["n_text"] == 0
    rows = stored(events)
    assert len(rows) == 14 and all(r["text"] == "" for r in rows)


def test_fast_writes_with_text_stay_actions(events):
    for i in range(20):
        at(events, T0 + i)
        send(events, "Thing", "api.example.com", "/v1/chat/send", {"text": f"message number {i} in a quick burst"})
    t = kind(events)
    assert t["klass"] == "action" and len(stored(events)) == 20


# ---------- action / incoming ----------
def test_write_with_text_is_an_action(events):
    seen = []
    events.on_event = seen.append
    send(events, "Thing", "api.example.com", "/v1/notes", {"title": "Groceries", "body": "Buy oat milk and coffee beans"})
    t = kind(events)
    assert t["klass"] == "action" and t["direction"] == "out" and t["n_text"] == 1
    assert len(seen) == 1
    ev = seen[0]
    assert ev["sig"] == t["sig"] and ev["app"] == "Thing" and ev["klass"] == "action" and ev["label"] == t["label"]
    assert "Buy oat milk and coffee beans" in ev["text"] and ev["id"] == stored(events)[0]["id"]


def test_pushed_text_is_incoming(events):
    seen = []
    events.on_event = seen.append
    events.ws("Thing", "ws.example.com", "/ws", json.dumps({"type": "comment.created", "comment": {"body": "Looks right to me, merging"}}), False)
    t = kind(events)
    assert t["klass"] == "incoming" and t["direction"] == "in"
    assert [e["text"] for e in seen] == ["Looks right to me, merging"]


def test_incoming_kind_does_not_store_its_textless_frames(events):
    for body in ("Looks right to me, merging", None, "One more thing before you go"):
        events.ws("Thing", "ws.example.com", "/ws", json.dumps({"type": "comment.created", "comment": {"body": body, "n": 1}}), False)
    assert kind(events)["n"] == 3
    assert [r["text"] for r in stored(events)] == ["Looks right to me, merging", "One more thing before you go"]


def test_textless_pushes_go_quiet_after_a_while(events):
    for i in range(16):
        at(events, T0 + i * 120)
        events.ws("Thing", "ws.example.com", "/ws", json.dumps({"type": "doc.synced", "rev": i}), False)
        if i == 5:
            assert kind(events)["klass"] == "signal"
    assert kind(events)["klass"] == "background"


def test_messages_on_an_array_framed_socket_survive_other_traffic(events):
    def frame(event, payload):
        return json.dumps(["3", None, "room:lobby", event, payload])
    at(events, T0)
    events.ws("Campfire", "rt.example.com", "/socket/websocket", frame("new_msg", {"body": "anyone around for a quick call?"}), False)
    for i in range(30):
        at(events, T0 + 60 + i * 45)
        events.ws("Campfire", "rt.example.com", "/socket/websocket", frame("presence_diff", {"joins": {}, "leaves": {}}), False)
    at(events, T0 + 3600)
    events.ws("Campfire", "rt.example.com", "/socket/websocket", frame("new_msg", {"body": "never mind, found the answer"}), False)
    assert [r["text"] for r in stored(events) if r["text"]] == ["anyone around for a quick call?", "never mind, found the answer"]


# ---------- never an event: sign-in and payment ----------
CREDS = {"username": "sam@example.com", "password": "correct-horse-battery", "note": "this is a note about things"}


@pytest.mark.parametrize("host, path", [
    ("api.example.com", "/login"), ("api.example.com", "/api/v1/auth/login"), ("api.example.com", "/oauth/token"),
    ("oauth2.googleapis.com", "/token"), ("api.example.com", "/oauth2/v4/token"), ("api.example.com", "/api/checkout/confirm"),
    ("api.example.com", "/users/password"), ("api.example.com", "/account/sign-in"), ("api.stripe.com", "/v1/payment_intents"),
    ("api.example.com", "/api/billing/portal"), ("api.example.com", "/signup"), ("api.example.com", "/api/2fa/verify"),
    ("api.example.com", "/api/tokens.refresh"), ("api.example.com", "/auth/v1/token?grant_type=password"),
    ("login.microsoftonline.com", "/common/GetCredentialType"), ("accounts.google.com", "/v3/signin/_/AccountsSignInUi/data/batchexecute"),
    ("api.example.com", "/sso/saml/acs"), ("api.example.com", "/webauthn/assertion"), ("api.example.com", "/v1/cards"),
])
def test_sensitive_requests_leave_no_trace(events, host, path):
    seen = []
    events.on_event = seen.append
    send(events, "Zen", host, path, CREDS)
    send(events, "Thing", host, path, "username=sam&password=correct-horse-battery", ctype=FORM)
    assert kinds(events) == [] and stored(events) == [] and seen == []
    events.flush()
    assert events.store.q("SELECT * FROM event_types") == []


@pytest.mark.parametrize("host, path", [
    ("api.example.com", "/auth"), ("api.example.com", "/api/v1/sessions"), ("api.example.com", "/api/register"),
    ("identitytoolkit.googleapis.com", "/v1/accounts:signInWithPassword?key=k"), ("accounts.example.com", "/ServiceLogin"),
    ("cognito-idp.eu-west-2.amazonaws.com", "/"),
])
def test_sign_in_requests_the_path_rule_misses(events, host, path):
    send(events, "Thing", host, path, CREDS)
    assert kinds(events) == []


@pytest.mark.parametrize("key", ["password", "current_password", "newPassword", "PASSWORD", "passwd", "passcode", "client_secret",
                                 "otp", "cvv", "Authorization"])
def test_credentials_in_an_ordinary_request_are_not_kept(events, key):
    for value in ("hunter2xyz", "correct horse battery staple"):
        send(events, "Thing", "api.example.com", "/api/users/me", {"display_name": "Sam", key: value,
                                                                  "bio": "Writes tests late at night"})
    events.flush()
    dump = json.dumps(events.store.q("SELECT * FROM events") + events.store.q("SELECT * FROM event_types"))
    assert "Writes tests late at night" in dump
    assert "hunter2xyz" not in dump and "battery staple" not in dump


def test_kanban_cards_are_not_payment_cards(events):
    send(events, "Zen", "trello.com", "/1/cards/60d5ec49f1b2c8001f8e4c3a/actions/comments", {"text": "Moved this to next sprint, blocked on design"})
    assert kind(events)["klass"] == "action"


# ---------- secrets ----------
def test_secrets_are_masked_in_stored_text_and_examples(events):
    s = fake_secrets()
    seen = []
    events.on_event = seen.append
    send(events, "Thing", "api.example.com", "/v1/notes", {"text": f"use {s['aws_access_key']} for the deploy, "
                                                                   f"the slack bot is {s['slack_token']} ok"})
    send(events, "Thing", "api.example.com", "/v1/notes", {"text": f"the old one was {s['github_token']} I think"})
    events.flush()
    dump = json.dumps(events.store.q("SELECT * FROM events") + events.store.q("SELECT * FROM event_types") + seen)
    for value in s.values():
        assert value not in dump
    assert seen[0]["text"] == "use [aws_access_key] for the deploy, the slack bot is [slack_token] ok"
    assert seen[1]["text"] == "the old one was [github_token] I think"          # a long token no longer hides its sentence
    assert kind(events, "+text")["example"] == seen[1]["text"]                  # a kind's example is its latest text


def test_secrets_are_masked_in_stored_fields(events):
    s = fake_secrets()
    send(events, "Thing", "api.example.com", "/v1/notes", {
        "text": "Here are the deploy notes for tonight, read them before you start please",
        "description": f"backup token {s['github_token']} for deploys", "region": s["aws_access_key"]})
    events.flush()
    dump = json.dumps(events.store.q("SELECT * FROM events") + events.store.q("SELECT * FROM event_types"))
    assert s["github_token"] not in dump and s["aws_access_key"] not in dump


# ---------- muting ----------
def test_muted_kind_is_counted_but_not_stored(events):
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    t = kind(events)
    events.flush()
    events.mute(t["sig"], True)
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "And some bread for the weekend"})
    assert t["n"] == 2 and len(stored(events)) == 1
    assert events.store.q("SELECT muted FROM event_types WHERE sig=?", (t["sig"],)) == [{"muted": 1}]
    assert Events(events.store).types[t["sig"]]["muted"] == 1            # and it survives a restart
    events.mute(t["sig"], False)
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Never mind the bread, got some"})
    assert len(stored(events)) == 2


def test_muted_app_is_counted_but_not_stored(events):
    seen = []
    events.on_event = seen.append
    events.muted_apps.add("Thing")
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    send(events, "Other", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    assert kind(events, "Thing|")["n"] == 1
    assert [r["app"] for r in stored(events)] == ["Other"] and [e["app"] for e in seen] == ["Other"]


def test_muted_app_leaves_no_text_in_the_catalog(events):
    events.muted_apps.add("Thing")
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    events.flush()
    assert "oat milk" not in json.dumps(events.store.q("SELECT * FROM event_types"))


# ---------- curated emit ----------
def test_emit_keeps_its_class_and_dedupes_by_key(events):
    kw = dict(app="Slack", sig="slack:dm", label="someone DMs me", direction="in", klass="incoming", who="Priya",
              place="DM with Priya", context={"channel": "D1"})
    a = events.emit(text="can you send me the deck?", key="slack:D1:1.1", **kw)
    b = events.emit(text="can you send me the deck?", key="slack:D1:1.1", **kw)        # same message seen twice
    c = events.emit(text="", key="slack:D1:1.2", **kw)
    assert a and a["who"] == "Priya" and a["context"] == {"channel": "D1"} and b is None and c
    t = kind(events)
    assert t["curated"] == 1 and t["klass"] == "incoming" and t["n"] == 3 and len(stored(events)) == 2
    for i in range(40):                                  # a burst does not demote a curated kind to background
        at(events, T0 + i)
        events.emit(text="", key=f"k{i}", **kw)
    assert t["klass"] == "incoming"


def test_on_event_errors_do_not_break_recording(events, capsys):
    def boom(ev):
        raise RuntimeError("consumer failed")
    events.on_event = boom
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    assert len(stored(events)) == 1 and "consumer failed" in capsys.readouterr().out


def test_bad_input_never_raises(events):
    events.http("Thing", "api.example.com", "POST", "/v1/x", "application/json", b"{not json", None, "", None)
    events.http("Thing", "api.example.com", "POST", "/v1/x", "application/json", b"\xff\xfe\x00", {}, "", 500)
    events.http("Thing", "", "post", "", "", None, {}, "", None)
    events.ws("Thing", "ws.example.com", "/", None, True)
    events.ws("Thing", "ws.example.com", "/", b"", False)
    events.ws("Thing", "ws.example.com", "/", "not json at all", False)
    assert all(r["text"] in ("", "not json at all") for r in stored(events))


def test_own_and_loopback_traffic_is_ignored(events):
    send(events, "Ambient", "updates.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    send(events, "Zen", "127.0.0.1", "/api/trigger", {"text": "someone asks me for a file"})
    send(events, "Zen", "localhost", "/api/trigger", {"text": "someone asks me for a file"})
    assert kinds(events) == []


def test_own_and_loopback_websocket_traffic_is_ignored(events):
    events.ws("Zen", "localhost", "/ws", json.dumps({"type": "update", "text": "hot module reload of the page"}), False)
    events.ws("Ambient", "api.example.com", "/ws", json.dumps({"type": "update", "text": "hot module reload of the page"}), False)
    assert kinds(events) == []


def test_text_pushed_over_server_sent_events_is_incoming(events):
    send(events, "Zen", "app.example.com", "/api/stream?channel=general", method="GET", headers={"accept": "text/event-stream"},
         resp='event: message\ndata: {"text": "Could you review my pull request today?"}\n\n')
    assert any(t["klass"] == "incoming" for t in kinds(events))


# ---------- catalog ----------
def test_catalog_groups_by_app_and_hides_quiet_kinds(events):
    send(events, "Zen", "linear.app", "/graphql", {"query": "mutation IssueCreate { x }", "variables": {"title": "Export button does nothing"}})
    visit(events, "Zen", "linear.app", "/acme/inbox", "Inbox · Linear")
    send(events, "Zen", "linear.app", "/api/issues", method="GET")
    send(events, "Zen", "linear.app", "/api/telemetry", {"n": 1})
    send(events, "Zen", "linear.app", "/api/issue/star", {"starred": True})
    send(events, "Thing", "api.example.com", "/v1/items", method="GET")
    cat = events.catalog()
    assert [a["app"] for a in cat] == ["Linear"]                      # an app with only reads is not listed
    lin = cat[0]
    assert lin["via"] == ["Zen"] and lin["sources"] == ["network"] and lin["quiet"] == 3
    assert sorted(k["klass"] for k in lin["kinds"]) == ["action", "action"]
    assert {"sig", "label", "direction", "klass", "n", "n_text", "example"} <= set(lin["kinds"][0])
    quiet = {a["app"]: a for a in events.catalog(include_quiet=True)}
    assert set(quiet) == {"Linear", "Thing"}
    assert sorted(k["klass"] for k in quiet["Linear"]["kinds"]) == ["action", "action", "signal"]


def test_recent_filters_by_sig_and_pattern(events):
    for i in range(3):
        events.emit(app="Slack", sig="slack:dm", label="someone DMs me", direction="in", klass="incoming", text=f"dm {i} here", key=f"a{i}")
        events.emit(app="WhatsApp", sig="whatsapp:dm", label="someone DMs me", direction="in", klass="incoming", text=f"wa {i} here", key=f"b{i}")
        events.emit(app="Slack", sig="slack:sent", label="I send a message", direction="out", klass="action", text=f"sent {i} here", key=f"c{i}")
    assert [r["text"] for r in events.recent(sig="slack:dm")] == ["dm 2 here", "dm 1 here", "dm 0 here"]
    rows = events.recent(sigs=["*:dm"], limit=4)
    assert len(rows) == 4 and {r["sig"] for r in rows} == {"slack:dm", "whatsapp:dm"}
    assert rows[0]["label"] == "someone DMs me" and rows[0]["klass"] == "incoming" and rows[0]["fields"] == {}
    last = events.recent(limit=1)[0]["id"]
    assert events.recent(after=last) == []


# ---------- persistence ----------
def test_catalog_survives_a_restart(events, store):
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    send(events, "Thing", "api.example.com", "/v1/notes", method="GET")
    events.flush()
    again = Events(store)
    assert {s: (t["klass"], t["n"]) for s, t in again.types.items()} == {s: (t["klass"], t["n"]) for s, t in events.types.items()}
    send(again, "Thing", "api.example.com", "/v1/notes", {"body": "And some bread for the weekend"})
    assert kind(again, "POST")["n"] == 2 and kind(again, "POST")["klass"] == "action"


def test_backfill_builds_the_catalog_without_firing(store):
    rows = [
        dict(kind="http", app="Thing", host="api.example.com", method="POST", path="/v1/notes", req_ctype="application/json",
             req_body=json.dumps({"body": "Buy oat milk and coffee beans"}), req_headers=[["Content-Type", "application/json"]], status=201),
        dict(kind="http", app="Zen", host="github.com", method="GET", path="/acme/api/pull/12", req_headers=[["Sec-Fetch-Mode", "navigate"]],
             resp_body="<title>Fix flaky test · Pull Request #12</title>", status=200),
        dict(kind="ws", app="Thing", host="ws.example.com", method="WS↓", path="/ws",
             resp_body=json.dumps({"type": "comment.created", "body": "Looks right to me, merging"})),
        dict(kind="ws", app="Thing", host="ws.example.com", method="WS↑", path="/ws", req_body=json.dumps({"type": "ping"})),
        dict(kind="opaque", app="Thing", host="pinned.example.com"),
    ]
    for i, r in enumerate(rows):
        store.add({"ts": T0 + i, "decrypted": 0 if r["kind"] == "opaque" else 1, **r})
    seen = []
    ev = Events(store, on_event=seen.append)
    assert ev.backfill() == 4
    assert seen == [] and ev.on_event is not None
    by = {t["sig"].split("|")[1].split(" ")[0]: t for t in ev.types.values()}
    assert by["POST"]["klass"] == "action" and by["VISIT"]["klass"] == "action"
    assert by["WS↓"]["klass"] == "incoming" and by["WS↑"]["klass"] == "background"
    assert by["POST"]["first_ts"] == T0                                 # the recorded time, not now
    assert len(stored(ev)) == 3


# ---------- "show me once" ----------
def test_recording_reports_what_happened_while_it_ran(events):
    at(events, time.time() - 7200)                       # an old, steady kind
    for i in range(40):
        send(events, "Thing", "api.example.com", "/v1/autosave", {"text": f"draft number {i} of the note"})
    at(events, None)
    tok = events.record_start(30)
    send(events, "Thing", "api.example.com", "/v1/autosave", {"text": "draft number 41 of the note"})
    send(events, "Zen", "linear.app", "/graphql", {"query": "mutation IssueCreate { x }", "variables": {"title": "Export button does nothing"}})
    send(events, "Zen", "linear.app", "/api/issues", method="GET")
    send(events, "Zen", "linear.app", "/api/telemetry", {"n": 1})
    r = events.record_read(tok)
    assert r["active"] and 0 < r["left"] <= 30
    assert [k["label"] for k in r["kinds"]] == ["issue create", "autosave"]            # the new thing first, no reads or telemetry
    first = r["kinds"][0]
    assert first["new"] is True and first["n"] == 1 and first["app"] == "Linear" and first["example"] == "Export button does nothing"
    assert r["kinds"][1]["new"] is False

    r = events.record_read(tok, stop=True)
    assert r["active"] is False and r["left"] == 0
    send(events, "Zen", "linear.app", "/graphql", {"query": "mutation IssueCreate { x }", "variables": {"title": "Another one after stopping"}})
    assert events.record_read(tok)["kinds"][0]["n"] == 1                               # nothing counted after stop


def test_recording_tokens(events):
    a, b = events.record_start(), events.record_start()
    assert a != b and events.record_read("nope") == {"error": "no such recording"}
    assert events.record_start(10_000) and max(r["until"] - r["start"] for r in events.recordings.values()) <= 300.5


# ---------- analytics SDK calls ----------
@pytest.mark.parametrize("path, body", [
    ("/v1/identify", {"userId": "u_18273645", "traits": {"plan": "Acme Dashboard Pro"}}),
    ("/page", {"name": "Acme Dashboard Pricing"}), ("/v1/track", {"event": "Signed Up For Trial"}),
    ("/api/v1/group", {"traits": {"name": "Acme Dashboard Team"}}), ("/v1/alias", {"previousId": "anon-1", "userId": "u_18273645"}),
])
def test_analytics_sdk_calls_are_background(events, path, body):
    send(events, "Zen", "events.acme-shop.com", path, body)
    assert kind(events)["klass"] == "background" and stored(events) == []


def test_a_longer_name_that_starts_with_an_analytics_word_is_a_real_operation(events):
    send(events, "Thing", "api.example.com", "/v1/identify/speaker", {"text": "who is talking in this recording"})
    send(events, "Thing", "api.example.com", "/v1/pages/42", {"title": "Notes from the offsite"}, method="PATCH")
    assert sorted((t["label"], t["klass"]) for t in kinds(events)) == [("identify speaker", "action"), ("update pages", "action")]


def test_chatter_words_only_count_as_the_operation_itself(events):
    send(events, "Thing", "api.example.com", "/api/typing-tests/55/results", {"note": "Ninety words a minute, a new record"})
    send(events, "Thing", "api.example.com", "/api/channels/55/typing", {"note": "Ninety words a minute, a new record"})
    assert kind(events, "results")["klass"] == "action" and kind(events, "/typing+")["klass"] == "background"


# ---------- server-sent events ----------
def test_sse_message_is_an_incoming_kind_named_by_its_event(events):
    seen = []
    events.on_event = seen.append
    events.sse("Zen", "app.example.com", "/api/rooms/4410/stream?since=9",
               'retry: 3000\n\nevent: message\nid: 7\ndata: {"from": "priya", "text": "Could you look at the invoice before noon?"}\n\n'
               ': keep-alive\n\nevent: room.renamed\ndata: {"title": "Invoices and other paperwork"}\n\n')
    events.sse("Zen", "app.example.com", "/api/rooms/4411/stream", b'event: message\r\ndata: {"text": "Second room, same kind of thing"}\r\n\r\n')
    t = kind(events, "#message")
    assert t["sig"] == "Example|SSE↓ app.example.com/api/rooms/{id}/stream#message"
    assert (t["klass"], t["direction"], t["label"], t["n"], t["via"]) == ("incoming", "in", "message", 2, "Zen")
    assert kind(events, "#room.renamed")["label"] == "room renamed"
    assert [e["text"] for e in seen] == ["Could you look at the invoice before noon?", "Invoices and other paperwork",
                                         "Second room, same kind of thing"]
    assert seen[0]["fields"] == {"from": "priya"} and seen[0]["direction"] == "in"


def test_sse_without_event_names_uses_the_payload_or_plain_text(events):
    events.sse("Thing", "api.example.com", "/v1/stream",
               'data: {"type": "comment.created", "comment": {"body": "Looks right to me, merging"}}\n\n'
               'data: Deploy finished, all checks are green\n\ndata: [DONE]\n\ndata: 17\n\n')
    assert kind(events, "#comment.created")["example"] == "Looks right to me, merging"
    assert kind(events, "#message")["example"] == "Deploy finished, all checks are green" and len(kinds(events)) == 2
    assert kind(events, "#message")["app"] == "Thing" and kind(events, "#message")["n"] == 1      # "[DONE]" and "17" are not events


def test_sse_token_stream_becomes_one_message(events):
    words_ = "Sure, here is a short plan for the launch: write the notes, tell the team, ship it on Friday.".split(" ")
    openai = "".join('data: ' + json.dumps({"id": "c1", "object": "chat.completion.chunk",
                                            "choices": [{"index": 0, "delta": {"content": ("" if i == 0 else " ") + w}}]}) + "\n\n"
                     for i, w in enumerate(words_)) + "data: [DONE]\n\n"
    events.sse("Thing", "api.llm.example", "/v1/chat/completions", openai)
    rows = stored(events)
    assert len(rows) == 1 and rows[0]["text"] == " ".join(words_) and kind(events)["n"] == 1
    # named events (the other common wire shape): only the deltas carry text; the bookkeeping events are quiet kinds
    named = 'event: message_start\ndata: {"type": "message_start", "message": {"id": "m1", "content": []}}\n\n' + "".join(
        'event: content_block_delta\ndata: ' + json.dumps({"type": "content_block_delta", "index": 0,
                                                            "delta": {"type": "text_delta", "text": w + " "}}) + "\n\n"
        for w in words_) + 'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    events.sse("Other", "api.llm.example", "/v1/messages", named)
    assert [r["text"] for r in stored(events) if r["app"] == "Other" and r["text"]] == [" ".join(words_)]
    assert kind(events, "Other|SSE↓ api.llm.example/v1/messages#content_block_delta")["klass"] == "incoming"


def test_sse_separate_short_messages_are_not_glued(events):
    body = "".join(f'event: message\ndata: {json.dumps({"text": m})}\n\n' for m in
                   ["are you there?", "ok", "call me when you can", "never mind, sorted", "thanks anyway"])
    events.sse("Thing", "api.example.com", "/v1/stream", body)
    assert [r["text"] for r in stored(events)] == ["are you there?", "ok", "call me when you can", "never mind, sorted", "thanks anyway"]


def test_sse_follows_the_same_privacy_rules(events):
    s = fake_secrets()
    msg = 'event: message\ndata: {"text": "Could you look at the invoice before noon?"}\n\n'
    events.sse("Ambient", "app.example.com", "/stream", msg)
    events.sse("Zen", "localhost", "/stream", msg)
    events.sse("Zen", "login.example.com", "/stream", msg)
    events.sse("Zen", "app.example.com", "/oauth/token/stream", msg)
    assert kinds(events) == []
    events.muted_apps.add("Thing")
    events.sse("Thing", "app.example.com", "/stream", msg)
    assert kind(events)["n"] == 1 and kind(events)["example"] is None and stored(events) == []
    events.sse("Other", "app.example.com", "/stream", f'data: {{"text": "the key is {s["github_token"]} ok", "note": "{s["aws_access_key"]}"}}\n\n')
    events.flush()
    dump = json.dumps(events.store.q("SELECT * FROM events") + events.store.q("SELECT * FROM event_types"))
    assert "[github_token]" in dump and s["github_token"] not in dump and s["aws_access_key"] not in dump


def test_sse_bad_input_never_raises(events):
    for data in (None, "", b"", "not an event stream at all", "data: {broken json\n\n", b"\xff\xfe\x00data: x\n\n", "data:\n\n", "event: only\n\n",
                 "data: " + "x" * 700_000):
        events.sse("Thing", "api.example.com", "/v1/stream", data)
    assert stored(events) == []


def test_sse_response_handed_to_http_is_read_once(events):
    # Events.http reads an event-stream response itself when it is given one; the addon calls Events.sse instead.
    send(events, "Zen", "chat.example.ai", "/backend-api/conversation", {"messages": [{"content": "What is a good name for a bakery?"}]},
         headers={"accept": "text/event-stream"}, resp='data: {"message": "How about Rise and Shine?"}\n\n')
    assert sorted((t["klass"], t["direction"]) for t in kinds(events)) == [("action", "out"), ("incoming", "in")]
    assert [r["text"] for r in stored(events)] == ["How about Rise and Shine?", "What is a good name for a bakery?"]
    send(events, "Zen", "chat.example.ai", "/page", method="GET", headers={"accept": "text/html"}, resp="data: not a stream, just a page\n\n")
    assert len(stored(events)) == 2


# ---------- more of what never becomes an event ----------
@pytest.mark.parametrize("path, body", [
    ("/v1/cards", {"number": "4242424242424242", "exp_month": 12, "cvc": "123", "name": "Sam Example of London"}),
    ("/v1/customers/88213/cards", "card[number]=4242424242424242&card[exp_year]=2030&name=Sam+Example+of+London"),
    ("/api/cards/88213", {"cardNumber": "4242424242424242", "holder": "Sam Example of London"}),
    ("/api/payment-cards/88213/notes", {"note": "this one is the company card"}),
])
def test_payment_cards_are_told_from_kanban_cards_by_what_they_carry(events, path, body):
    send(events, "Zen", "api.example.com", path, body, ctype=FORM if isinstance(body, str) else "application/json")
    assert kinds(events) == []


def test_creating_and_editing_a_kanban_card_is_an_event(events):
    send(events, "Zen", "trello.com", "/1/cards", {"name": "Write the launch notes", "desc": "Draft them before the Friday review", "idList": "60d5ec49f1b2c8001f8e4c3a"})
    send(events, "Zen", "trello.com", "/1/cards/60d5ec49f1b2c8001f8e4c3a", {"desc": "Draft them before the Thursday review instead"}, method="PUT")
    assert sorted(t["klass"] for t in kinds(events)) == ["action", "action"]


def test_sensitive_path_keeps_working_as_a_pattern_for_the_addon():
    from engine.events import SENSITIVE_PATH, sensitive
    assert SENSITIVE_PATH.search("/v1/accounts:signInWithPassword") and SENSITIVE_PATH.search("/oauth/token") and SENSITIVE_PATH.search("/1/cards/9/x")
    assert not SENSITIVE_PATH.search("/api/authors/12/posts") and not SENSITIVE_PATH.search("/v1/sessions/9/messages") and not SENSITIVE_PATH.search("")
    assert sensitive("cognito-idp.eu-west-2.amazonaws.com", "/") and sensitive("api.example.com", "/api/v1/sessions?next=/home")
    assert not sensitive("api.example.com", "/api/blog/tokenizer/posts") and not sensitive("trello.com", "/1/cards/9/x", b'{"text": "hello"}')


# ---------- keeping the catalog small ----------
DAY = 86400.0


def test_prune_forgets_quiet_kinds_after_two_weeks_and_actions_after_ninety_days(events):
    at(events, T0)
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})          # an old action
    send(events, "Thing", "api.example.com", "/v1/notes", method="GET")                                      # an old read
    send(events, "Thing", "api.example.com", "/v1/items/7781/star", {"starred": True})                       # an old signal, muted
    events.emit(app="Slack", sig="slack:dm", label="someone DMs me", direction="in", klass="incoming", text="an old curated kind", key="k1")
    events.flush()
    events.mute(kind(events, "/star")["sig"], True)
    at(events, T0 + 13 * DAY)
    send(events, "Thing", "api.example.com", "/v1/telemetry", {"n": 1})
    assert events.prune() == 0 and len(kinds(events)) == 5                                                   # nothing is two weeks old yet
    at(events, T0 + 15 * DAY)
    send(events, "Thing", "api.example.com", "/v1/tasks", {"title": "Call the dentist on Monday"})
    assert events.prune() == 1                                   # the old read goes; the old action is kept for 90 days
    assert kind(events, "/v1/notes+text")["n"] == 1
    at(events, T0 + 92 * DAY)
    send(events, "Thing", "api.example.com", "/v1/tasks", {"title": "Call the dentist on Monday"})
    assert events.prune() == 2                                   # now the old action too, and the stale telemetry kind
    left = sorted(t["sig"] for t in kinds(events))
    assert left == ["Thing|POST api.example.com/v1/items/{id}/star", "Thing|POST api.example.com/v1/tasks+text", "slack:dm"]
    events.flush()
    assert sorted(r["sig"] for r in events.store.q("SELECT sig FROM event_types")) == left                   # gone from disk too
    assert Events(events.store).types[left[0]]["muted"] == 1
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "And some bread for the weekend"})          # a forgotten kind just starts again
    assert kind(events, "/v1/notes+text")["n"] == 1


@pytest.mark.parametrize("keep", [lambda sig: sig.endswith("/v1/notes+text"), {"Thing|POST api.example.com/v1/notes*"}, ["*notes+text"]])
def test_prune_leaves_alone_what_keep_names(events, keep):
    at(events, T0)
    send(events, "Thing", "api.example.com", "/v1/notes", {"body": "Buy oat milk and coffee beans"})
    send(events, "Thing", "api.example.com", "/v1/tasks", {"title": "Call the dentist on Monday"})
    events.keep = keep
    at(events, T0 + 100 * DAY)
    assert events.prune() == 1 and [t["sig"] for t in kinds(events)] == ["Thing|POST api.example.com/v1/notes+text"]


def test_prune_caps_the_catalog_quiet_and_oldest_first(events, monkeypatch):
    monkeypatch.setattr(Events, "MAX_TYPES", 6)
    for i in range(5):
        at(events, T0 + i)
        send(events, "Thing", "api.example.com", f"/v1/read{'abcde'[i]}", method="GET")
    for i in range(5):
        at(events, T0 + 10 + i)
        send(events, "Thing", "api.example.com", f"/v1/write{'abcde'[i]}", {"body": "Buy oat milk and coffee beans"})
    assert events.prune() == 4
    assert sorted(t["sig"].split("/v1/")[1] for t in kinds(events)) == ["reade", "writea+text", "writeb+text", "writec+text", "writed+text", "writee+text"]


def test_prune_runs_from_flush_now_and_then(events):
    at(events, T0)
    send(events, "Thing", "api.example.com", "/v1/notes", method="GET")
    at(events, T0 + 30 * DAY)
    send(events, "Thing", "api.example.com", "/v1/tasks", method="GET")
    events.flush()
    assert len(kinds(events)) == 2                               # not on every flush
    events.last_prune = 0
    send(events, "Thing", "api.example.com", "/v1/tasks", method="GET")
    events.flush()
    assert [t["sig"] for t in kinds(events)] == ["Thing|GET api.example.com/v1/tasks"]


def test_kinds_recorded_for_ambient_itself_are_dropped_at_startup(store):
    ev = Events(store)
    ev.emit(app="Slack", sig="slack:dm", label="someone DMs me", direction="in", klass="incoming", text="hello there you", key="k1")
    for app in ("Ambient", "ambient-engine"):                    # as an older build recorded them
        ev.emit(app=app, sig=f"{app}|POST updates.example.com/v1/check+text", label="check", direction="out", klass="action", text="checking for updates")
    ev.flush()
    assert len(store.q("SELECT * FROM event_types")) == 3 and len(store.q("SELECT * FROM events")) == 3
    again = Events(store)
    assert list(again.types) == ["slack:dm"]
    assert [r["app"] for r in store.q("SELECT app FROM event_types")] == ["Slack"] and [r["app"] for r in store.q("SELECT app FROM events")] == ["Slack"]
