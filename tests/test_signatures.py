"""Signatures: app | METHOD host-template path-template #operation.

A signature has to be stable (the same gesture on another object lands in the same kind) and
specific (different operations on one endpoint are different kinds).
"""
import json

import pytest

from engine.events import host_template, path_template, site_name, words
from helpers import FORM, NAV, UUID, UUID2, kind, kinds, multipart, send, stored, visit


# ---------- path templating ----------
@pytest.mark.parametrize("path, want", [
    ("/api/v1/users/12345/posts", "/api/v1/users/{id}/posts"),
    ("/x/550e8400-e29b-41d4-a716-446655440000/y", "/x/{id}/y"),                     # uuid
    ("/files/d41d8cd98f00b204e9800998ecf8427e", "/files/{id}"),                      # md5
    ("/v1/boards/5f3e2a1b9c8d7e6f5a4b3c2d/lists", "/v1/boards/{id}/lists"),          # mongo object id
    ("/commit/9fceb02d0ae598e95dc970b74767f19372d61af8", "/commit/{id}"),            # git sha
    ("/api/conversations/C024BE91LQ/replies", "/api/conversations/{id}/replies"),    # Slack-style id
    ("/u/alice@example.com/inbox", "/u/{id}/inbox"),                                 # e-mail address
    ("/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit", "/d/{id}/edit"),        # long base64-ish doc id
    ("/track/AbCdEf123456GhIjKl", "/track/{id}"),
    ("/a/b?x=1&y=2", "/a/b"),                                                        # query string dropped
    ("/api/chat.postMessage", "/api/chat.postMessage"),                              # words stay
    ("/calendar/v3/calendars/primary/events", "/calendar/v3/calendars/primary/events"),
    ("", "/"),
    ("/", "/"),
])
def test_path_template(path, want):
    assert path_template(path) == want


def test_path_template_is_bounded():
    assert len(path_template("/" + "/".join(["segment"] * 60))) <= 120


@pytest.mark.parametrize("path, want", [
    ("/rest/api/3/issue/PROJ-123/comment", "/issue/{id}/comment"),       # Jira / Linear / Shortcut ticket keys
    ("/browse/ENG-4521", "/browse/{id}"),
    ("/api/v2/tickets/98231.json", "/api/v2/tickets/{id}"),              # numeric id with a format suffix
])
def test_path_template_ticket_keys_and_suffixed_ids(path, want):
    assert want in path_template(path)


@pytest.mark.parametrize("path", ["/watch/dQw4wXWgXcQ/comments", "/rooms/!OGEhHVWSdvArJzumhm:matrix.org/send",
                                  "/c/aBcDxYzQ/card-title"])
def test_path_template_short_opaque_ids(path):
    assert "{id}" in path_template(path)


def test_host_template_collapses_numbered_shards():
    assert host_template("Clients6.Google.com") == "clientsN.google.com"
    assert host_template("kws2.web.telegram.org") == host_template("kws4.web.telegram.org")


def test_same_gesture_on_two_objects_is_one_kind(events):
    send(events, "Asana", "app.asana.com", "/api/1.0/tasks/1204567890123456/stories", {"data": {"text": "Can you pick this up?"}})
    send(events, "Asana", "app.asana.com", "/api/1.0/tasks/1209876543210987/stories", {"data": {"text": "Done, over to you now"}})
    t = kind(events, "stories")
    assert t["n"] == 2 and t["n_text"] == 2
    assert t["sig"].startswith("Asana|POST app.asana.com/api/1.0/tasks/{id}/stories")
    assert len(stored(events)) == 2


def test_method_is_part_of_the_signature(events):
    send(events, "Thing", "api.example.com", "/v1/notes/42", {"title": "Renamed the note today"}, method="PATCH")
    send(events, "Thing", "api.example.com", "/v1/notes/42", method="DELETE")
    sigs = sorted(t["sig"] for t in kinds(events))
    assert len(sigs) == 2
    assert sigs[0].startswith("Thing|DELETE api.example.com/v1/notes/{id}")
    assert sigs[1].startswith("Thing|PATCH api.example.com/v1/notes/{id}")
    labels = {t["sig"].split("|")[1].split()[0]: t["label"] for t in kinds(events)}
    assert labels["DELETE"].startswith("delete ") and labels["PATCH"].startswith("update ")


# ---------- operation names ----------
def test_graphql_operation_name_and_mutation(events):
    send(events, "Linear", "client-api.linear.app", "/graphql", {
        "operationName": "IssueCreate",
        "query": "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success } }",
        "variables": {"input": {"title": "Login page throws a 500 after password reset", "teamId": UUID}}})
    t = kind(events)
    assert t["sig"].startswith("Linear|POST client-api.linear.app/graphql#IssueCreate")
    assert t["label"] == "issue create" and t["klass"] == "action"
    assert t["example"] == "Login page throws a 500 after password reset"


def test_graphql_query_is_marked_as_a_read(events):
    send(events, "Linear", "client-api.linear.app", "/graphql", {
        "operationName": "IssueList", "query": "query IssueList($first: Int) { issues(first: $first) { nodes { id title } } }",
        "variables": {"first": 50}})
    t = kind(events)
    assert "#get IssueList" in t["sig"] and t["klass"] == "read"
    assert stored(events) == []


def test_graphql_anonymous_query_and_batch_array(events):
    send(events, "Thing", "api.example.com", "/graphql", {"query": "{ viewer { id name } }"})
    assert kind(events, "#get query")["klass"] == "read"
    send(events, "Thing", "api.example.com", "/graphql", [
        {"query": "mutation AddComment($b: String!) { addComment(body: $b) { id } }", "variables": {"b": "Ship it when the tests pass"}},
        {"query": "query Me { viewer { id } }"}])
    t = kind(events, "#AddComment")
    assert t["klass"] == "action" and t["example"] == "Ship it when the tests pass"


def test_graphql_query_and_mutation_on_one_endpoint_are_different_kinds(events):
    for name, kw in (("IssueUpdate", "mutation"), ("IssueSearch", "query")):
        send(events, "Linear", "client-api.linear.app", "/graphql",
             {"operationName": name, "query": f"{kw} {name}($t: String) {{ x(t: $t) {{ id }} }}", "variables": {"t": "export button is broken again"}})
    assert len(kinds(events)) == 2
    assert kind(events, "#IssueUpdate")["klass"] == "action"
    assert kind(events, "IssueSearch")["klass"] == "read"


def test_jsonrpc_method(events):
    send(events, "Notes", "rpc.example.com", "/rpc", {"jsonrpc": "2.0", "id": 7, "method": "notes.create",
                                                      "params": {"title": "Groceries", "body": "Buy oat milk and coffee beans"}})
    t = kind(events)
    assert t["sig"].startswith("Notes|POST rpc.example.com/rpc#notes.create") and t["label"] == "notes create"
    assert t["example"] == "Buy oat milk and coffee beans"


def test_query_string_operation(events):
    send(events, "Thing", "api.example.com", "/api?action=createNote&v=3", {"body": "Remember to call the dentist tomorrow"})
    t = kind(events)
    assert "/api#createNote" in t["sig"] and t["label"] == "create note"
    send(events, "Thing", "api.example.com", "/2.0/?method=track.scrobble&format=json", "artist=Portishead&track=Roads", ctype=FORM)
    assert kind(events, "#track.scrobble")["label"] == "track scrobble"


def test_path_that_names_the_operation_ignores_body_op(events):
    # Slack sends type=message in the body; the path already says chat.postMessage.
    send(events, "Thing", "acme.example.com", "/api/chat.postMessage", "type=message&channel=C024BE91L&text=see+you+at+the+standup", ctype=FORM)
    t = kind(events)
    assert "#" not in t["sig"] and t["label"] == "chat post message"


def test_form_body(events):
    send(events, "Zen", "news.ycombinator.com", "/comment",
         "parent=4242424&goto=item%3Fid%3D4242424&text=This+is+a+thoughtful+reply+about+databases", ctype=FORM)
    t = kind(events)
    assert t["app"] == "Hacker News" and t["klass"] == "action"
    assert t["example"] == "This is a thoughtful reply about databases"


def test_form_body_without_content_type_is_still_parsed(events):
    send(events, "Thing", "api.example.com", "/v1/reply", "thread=991&text=sounds+good+to+me", ctype="")
    assert kind(events)["example"] == "sounds good to me"


def test_slack_style_multipart_with_blocks_json(events):
    blocks = json.dumps([{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "text", "text": "sure, I will send the deck tonight"}]}]}])
    body, ctype = multipart({"token": "xoxc-" + "1234567890-abcdefghij", "channel": "D024BE91L", "ts": "1731200000.000100",
                             "type": "message", "blocks": blocks, "client_msg_id": UUID})
    send(events, "Slack", "acme.slack.com", "/api/chat.postMessage?_x_id=noversion-1731200000.123&slack_route=T024BE7LD",
         body, ctype=ctype)
    t = kind(events)
    assert t["sig"].startswith("Slack|POST acme.slack.com/api/chat.postMessage")
    assert t["klass"] == "action" and t["label"] == "chat post message"
    ev = stored(events)[0]
    assert ev["text"] == "sure, I will send the deck tonight"
    assert "xoxc" not in json.dumps(ev)


def test_ndjson_body(events):
    nd = "\n".join(json.dumps(x) for x in [{"index": {"_id": "1"}},
                                           {"title": "Weekly notes", "content": "Shipped the importer and fixed two bugs"}])
    send(events, "Thing", "api.example.com", "/v1/docs/_bulk", nd, ctype="application/x-ndjson")
    assert kind(events)["example"] == "Shipped the importer and fixed two bugs"


def test_batch_array_body(events):
    send(events, "Thing", "api.example.com", "/v1/batch", [
        {"id": UUID, "method": "PATCH", "url": "/tasks/1", "body": {"completed": True}},
        {"id": UUID2, "method": "POST", "url": "/tasks/1/comments", "body": {"text": "Finished this one, please verify"}}])
    assert stored(events)[0]["text"] == "Finished this one, please verify"


@pytest.mark.parametrize("sep", ["\n", ""])
def test_google_xssi_prefix(events, sep):
    body = ")]}'" + sep + json.dumps([[["wrb.fr", "AbC12d", json.dumps([None, "Lunch with Dana moved to one thirty"])]]])
    send(events, "Google Chrome", "calendar.google.com", "/_/CalendarUi/data/batchexecute?rpcids=AbC12d&rt=c", body,
         ctype="application/json")
    t = kind(events)
    assert t["app"] == "Google Calendar" and "#AbC12d" in t["sig"]
    assert t["example"] == "Lunch with Dana moved to one thirty"


# ---------- who did it ----------
def test_browser_traffic_is_attributed_to_the_site(events):
    send(events, "Zen", "mail.google.com", "/mail/u/0/x", {"subject": "Hello there my friend"})
    send(events, "Google Chrome", "alive.github.com", "/_sockets/x", {"title": "Opened a pull request"})
    send(events, "Firefox", "app.acme-widgets.io", "/api/notes", {"text": "Order more widgets next week"})
    apps = {t["app"]: t["via"] for t in kinds(events)}
    assert apps == {"Gmail": "Zen", "GitHub": "Google Chrome", "Acme-widgets": "Firefox"}


def test_native_app_keeps_its_own_name(events):
    send(events, "Slack", "edgeapi.slack.com", "/cache/T024BE7LD/users/search", {"query": "priya from design"})
    send(events, "gh", "api.github.com", "/repos/acme/api/issues/42/comments", {"body": "This is fixed in the latest build"})
    apps = {t["app"]: t["via"] for t in kinds(events)}
    assert apps == {"Slack": None, "GitHub CLI": None}


@pytest.mark.parametrize("host, want", [
    ("mail.google.com", "Gmail"), ("alive.github.com", "GitHub"), ("client-api.linear.app", "Linear"),
    ("www.bbc.co.uk", "Bbc"), ("acme.zendesk.com", "Zendesk"), ("news.google.com", "Google"),
    ("www.google.com", "Google Search"), ("example.com", "Example"), ("LOCALHOST", "Localhost"),
])
def test_site_name(host, want):
    assert site_name(host) == want


def test_site_name_of_an_ip_address_is_not_a_digit():
    assert site_name("192.168.1.50") == "192.168.1.50"


# ---------- page visits ----------
def test_navigation_becomes_one_visit_kind_per_site(events):
    visit(events, "Zen", "github.com", "/acme/api/pull/12?diff=split", "Fix flaky test by sam &middot; Pull Request #12 &middot; acme/api")
    visit(events, "Zen", "github.com", "/acme/api/issues", "\n   Issues ·\n acme/api  ")
    t = kind(events)
    assert t["sig"] == "GitHub|VISIT" and t["klass"] == "action" and t["label"] == "open a page" and t["n"] == 2
    rows = stored(events)
    assert rows[0]["text"] == "Fix flaky test by sam · Pull Request #12 · acme/api"      # entities decoded
    assert rows[0]["fields"] == {"url": "https://github.com/acme/api/pull/12"}          # no query string
    assert rows[1]["text"] == "Issues · acme/api"                                        # whitespace collapsed


def test_visit_is_per_site(events):
    visit(events, "Zen", "github.com", "/acme", "acme")
    visit(events, "Zen", "linear.app", "/acme/inbox", "Inbox · Linear")
    assert sorted(t["sig"] for t in kinds(events)) == ["GitHub|VISIT", "Linear|VISIT"]


def test_subresources_failed_and_untitled_navigations_are_not_visits(events):
    send(events, "Zen", "github.com", "/assets/app.js", method="GET", headers={"sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"})
    send(events, "Zen", "github.com", "/frame", method="GET", headers={"sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"},
         resp="<title>Embedded</title>")
    visit(events, "Zen", "github.com", "/missing", "Page not found", status=404)
    send(events, "Zen", "github.com", "/raw", method="GET", headers=NAV, resp="plain text, no title")
    assert not any(t["sig"].endswith("|VISIT") for t in kinds(events))
    assert stored(events) == []


# ---------- git ----------
def test_git_push(events):
    send(events, "git-remote-https", "github.com", "/acme/api.git/git-receive-pack", b"0000PACK\x00\x00\x00\x02\x00\x00",
         ctype="application/x-git-receive-pack-request")
    t = kind(events)
    assert t["sig"] == "git|PUSH github.com" and t["klass"] == "action" and t["label"] == "git push"
    ev = stored(events)[0]
    assert ev["text"] == "pushed commits to acme/api on github.com" and ev["fields"] == {"repo": "acme/api"}


def test_git_fetch_is_not_an_event(events):
    send(events, "git-remote-https", "github.com", "/acme/api.git/info/refs?service=git-upload-pack", method="GET")
    send(events, "git-remote-https", "github.com", "/acme/api.git/git-upload-pack", b"0032want 9fceb02d0ae598e95dc970b74767f19372d61af8\n",
         ctype="application/x-git-upload-pack-request")
    assert {t["klass"] for t in kinds(events)} <= {"read", "background"}
    assert stored(events) == []


# ---------- labels ----------
@pytest.mark.parametrize("s, want", [
    ("chat.postMessage", "chat post message"), ("MESSAGE_CREATE", "message create"),
    ("CreateIssueMutation", "create issue"), ("saveTransactionsFanout", "save transactions fanout"),
    ("/v1.0/me/sendMail", "me send mail"), ("get IssueQuery", "get issue"),
])
def test_words(s, want):
    assert words(s) == want


def test_labels_do_not_contain_the_id_placeholder(events):
    send(events, "Figma", "www.figma.com", "/api/file/hV0d8QmJbL1cXz3yT5uKaN/comments", {"message": "Can we make this button bigger?"})
    assert "id" not in kind(events)["label"].split()


def test_terse_path_still_gets_a_readable_label(events):
    send(events, "Zen", "mail.google.com", "/sync/u/0/i/s?hl=en&c=14", [[["msg-a:r-812345", "Hi Alice, could you send the budget before Thursday?"]]])
    assert len(kind(events)["label"]) >= 4


def test_free_text_value_is_not_an_operation_name(events):
    send(events, "Thing", "api.example.com", "/v1/events", {"name": "Dinner with Dana", "start": "2026-11-12T19:00:00Z"})
    send(events, "Thing", "api.example.com", "/v1/events", {"name": "Team offsite planning", "start": "2026-11-13T09:00:00Z"})
    send(events, "Thing", "api.example.com", "/api/users/42", {"name": "Sam Example"}, method="PATCH")
    send(events, "Thing", "api.example.com", "/api/users/42", {"name": "Samuel Example"}, method="PATCH")
    assert len(kinds(events)) == 2 and not any("Dana" in t["sig"] or "Sam" in t["sig"] for t in kinds(events))


def test_websocket_frames_use_the_same_app_name_as_requests(events):
    send(events, "Code Helper", "api.example.com", "/v1/chat", {"text": "explain this function to me"})
    events.ws("Code Helper", "api.example.com", "/ws", json.dumps({"type": "note", "text": "explain this function to me"}), True)
    assert {t["app"] for t in kinds(events)} == {"Visual Studio Code"}


# ---------- operation names from frames ----------
def test_one_word_title_under_name_is_not_an_operation(events):
    for name in ("Groceries", "Standup"):
        send(events, "Thing", "api.example.com", "/v1/events", {"name": name, "start": "2026-11-12T19:00:00Z"})
    assert kind(events)["n"] == 2 and "#" not in kind(events)["sig"]
    send(events, "Thing", "api.example.com", "/v1/events", {"name": "page_view", "props": {"path": "/pricing"}})      # an event name is
    assert kind(events, "#page_view")["n"] == 1


def test_array_framed_and_signalr_frames_are_named_by_their_event(events):
    for event, payload in (("new_msg", {"body": "anyone around for a quick call?"}), ("phx_reply", {"status": "ok"}), ("heartbeat", {})):
        events.ws("Campfire", "rt.example.com", "/socket/websocket", json.dumps(["3", "17", "room:4410", event, payload]), False)
    events.ws("Zen", "hub.example.com", "/chathub", json.dumps({"type": 1, "target": "ReceiveMessage", "arguments": ["sam", "Are we still on for lunch on Friday?"]}) + "\x1e", False)
    events.ws("Zen", "hub.example.com", "/chathub", json.dumps({"type": 3, "invocationId": "17", "result": None}) + "\x1e", False)
    got = {t["sig"].split("|", 1)[1]: (t["label"], t["klass"]) for t in kinds(events)}
    assert got == {"WS↓ rt.example.com#new_msg": ("new message", "incoming"), "WS↓ rt.example.com#phx_reply": ("phx reply", "signal"),
                   "WS↓ rt.example.com#heartbeat": ("heartbeat", "background"),
                   "WS↓ hub.example.com#ReceiveMessage": ("receive message", "incoming"), "WS↓ hub.example.com#message": ("message", "signal")}


def test_short_ids_are_told_from_camelcase_names():
    for seg in ("saveTransactionsFanout", "getUserByIdOrName", "CreateTweet", "AccountsSignInUi", "isOkToGoNow", "McDonaldsMenu", "chat.postMessage",
                "JohnSmith", "create_comment"):
        assert path_template(f"/x/{seg}/y") == f"/x/{seg}/y"
    for seg in ("dQw4w9WgXcQ", "aBcDxYzQ", "V1StGXR8_Z", "@alice:matrix.org", "$Rqnc-F-dvnEYJTyHq_iKxU2bZ1CI92-kuZq3a5lr5Zg"):
        assert path_template(f"/x/{seg}/y") == "/x/{id}/y"
