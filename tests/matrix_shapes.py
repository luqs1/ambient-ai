"""Realistic request / frame shapes from apps the engine has no special code for.

Each shape plays the traffic one user gesture produces (hand-written to mimic the app's public
API or web client; no captured data) and says what a person would expect to see in the catalog.
`run(shape, events)` reports what the engine actually made of it. Run this file directly for the
table:

    uv run python tests/matrix_shapes.py
"""
from __future__ import annotations

import json
import os
import random
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from helpers import FORM, UUID, UUID2, multipart, readable, send  # noqa: E402


def notion(ev, v=0):
    send(ev, "Notion", "www.notion.so", "/api/v3/saveTransactionsFanout", {
        "requestId": UUID, "transactions": [{"id": UUID2, "spaceId": UUID, "debug": {"userAction": "ListItemBlock.handleNativeDrop"},
                                             "operations": [
            {"pointer": {"table": "block", "id": UUID2, "spaceId": UUID}, "path": ["properties", "title"],
             "command": "set", "args": [["Draft the launch plan with the marketing team"]]},
            {"pointer": {"table": "block", "id": UUID2, "spaceId": UUID}, "path": ["last_edited_time"],
             "command": "set", "args": 1731200000000}]}]})


def linear(ev, v=0):
    send(ev, "Zen", "client-api.linear.app", "/graphql", {
        "operationName": "IssueCreate",
        "query": "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier } } }",
        "variables": {"input": {"teamId": UUID, "title": "Login page throws a 500 after password reset",
                                "priority": 2, "stateId": UUID2}}})


def gmail(ev, v=0):
    # Gmail web "send": a positional JSON array (protobuf-as-JSON), no key names at all.
    body = [None, [[["msg-a:r-8123456789012345678", None, None, [["Alice Example", "alice@example.com"]],
                     "Quarterly budget review",
                     "Hi Alice, could you send over the updated budget before Thursday? Thanks", 1731200000000,
                     ["thread-a:r-4455667788990011223"]]]], "ui-2", 3]
    send(ev, "Zen", "mail.google.com", f"/sync/u/{v}/i/s?hl=en&c={14 + v}&rt=r&pt=ji", body, ctype="application/json+protobuf")


def google_calendar(ev, v=0):
    send(ev, "Google Chrome", "calendar.google.com", "/calendar/v3/calendars/primary/events?sendUpdates=all", {
        "summary": "Design review with Priya", "start": {"dateTime": "2026-11-12T15:00:00Z", "timeZone": "Europe/London"},
        "end": {"dateTime": "2026-11-12T15:30:00Z", "timeZone": "Europe/London"},
        "attendees": [{"email": "priya@example.com"}], "reminders": {"useDefault": True}})


def jira(ev, v=0):
    send(ev, "Zen", "acme.atlassian.net", f"/rest/api/3/issue/{('PROJ-123', 'PROJ-987')[v]}/comment", {"body": {
        "type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "Reproduced on staging, the export button does nothing."}]}]}})


def trello(ev, v=0):
    send(ev, "Zen", "trello.com", f"/1/cards/{('60d5ec49f1b2c8001f8e4c3a', '61a7bd02c4e9f3002b7d5a19')[v]}/actions/comments",
         {"text": "Moved this to next sprint, blocked on design", "dsc": "9f8e7d6c5b4a39281706f5e4d3c2b1a0" * 2})


def asana(ev, v=0):
    send(ev, "Asana", "app.asana.com", f"/api/1.0/tasks/{('1204567890123456', '1209876543210987')[v]}/stories",
         {"data": {"text": "Can you pick this up after the release?"}})


def figma(ev, v=0):
    send(ev, "Figma", "www.figma.com", f"/api/file/{('hV0d8QmJbL1cXz3yT5uKaN', 'Qp7RkT2mWx9ZbY4cLs6DfG')[v]}/comments", {
        "message": "Can we make this button bigger?", "message_meta": [{"t": "Can we make this button bigger?"}],
        "client_meta": {"x": 120.5, "y": 88.1, "node_id": "12:345", "node_offset": {"x": 10, "y": 4}}, "uuid": UUID})


def telegram_web(ev, v=0):
    # Telegram Web speaks MTProto inside a binary WebSocket: encrypted before TLS, so there is no text to find.
    rnd = random.Random(7)
    ev.ws("Zen", "kws2.web.telegram.org", "/apiws", rnd.randbytes(280), True)
    ev.ws("Zen", "kws2.web.telegram.org", "/apiws", rnd.randbytes(412), False)


def trpc(ev, v=0):
    send(ev, "Zen", "app.cal.com", "/api/trpc/bookings/confirm?batch=1",
         {"0": {"json": {"bookingId": 4412, "confirmed": True, "reason": "Works for me, see you then"}}})


def socketio(ev, v=0):
    path = "/socket.io/?EIO=4&transport=websocket&sid=Lbo5JLzTotvW3g2LAAAB"
    ev.ws("Zen", "chat.example.com", path, '42["chat message",{"room":"general","text":"is the deploy finished yet?","from":"u_18273645"}]', False)


def sse_chat(ev, v=0):
    # The request that starts a streamed answer in a ChatGPT-style web app.
    send(ev, "Zen", "chat.example.ai", "/backend-api/conversation", {
        "action": "next", "model": "auto", "parent_message_id": UUID2,
        "messages": [{"id": UUID, "author": {"role": "user"},
                      "content": {"content_type": "text", "parts": ["What is a good name for a bakery in Leeds?"]}}]},
         headers={"accept": "text/event-stream"})


def sse_push(ev, v=0):
    # What the other side says arrives as a server-sent-events response, not as a request or a WebSocket frame.
    send(ev, "Zen", "app.example.com", f"/api/rooms/{(4410, 4411)[v]}/stream", method="GET", headers={"accept": "text/event-stream"},
         resp='event: message\ndata: {"from": "priya", "text": "Could you look at the invoice before noon?"}\n\n')


def ms_graph_sendmail(ev, v=0):
    send(ev, "Microsoft Outlook", "graph.microsoft.com", "/v1.0/me/sendMail", {
        "message": {"subject": "Contract renewal",
                    "body": {"contentType": "Text", "content": "Hi Sam, attaching the renewal terms we discussed on Monday."},
                    "toRecipients": [{"emailAddress": {"address": "sam@example.com"}}]},
        "saveToSentItems": True})


def zendesk(ev, v=0):
    send(ev, "Zen", "acme.zendesk.com", f"/api/v2/tickets/{(98231, 98877)[v]}.json", method="PUT", body={
        "ticket": {"comment": {"body": "Thanks for reaching out, we have refunded the duplicate charge.", "public": True},
                   "status": "solved"}})


def teams_web(ev, v=0):
    send(ev, "Microsoft Edge", "teams.microsoft.com",
         "/api/chatsvc/emea/v1/users/ME/conversations/19%3Ameeting_"
         + ("NzQ5ZWI0YjUtZTM0LTQ2ZjktOTc3OC1jZTRlMjY4ZTU5Y2Yw", "TWpBeU5pMHhNUzB4TUZRd09Eb3dNRG93TUM0d01EQmE0")[v]
         + "%40thread.v2/messages", {
             "content": "<p>Can you join the standup five minutes early?</p>", "messagetype": "RichText/Html",
             "contenttype": "text", "clientmessageid": "5234981273645098123", "imdisplayname": "Sam Example"})


def github_web_comment(ev, v=0):
    body, ctype = multipart({"authenticity_token": "kq3Jb0aT9x" * 8, "issue": "42",
                             "comment[body]": "This is fixed in the latest build, closing the issue."})
    send(ev, "Zen", "github.com", "/acme/api/issue_comments", body, ctype=ctype)


def phoenix_channels(ev, v=0):
    ev.ws("Campfire", "realtime.example.com", "/socket/websocket?vsn=2.0.0", json.dumps(
        ["3", None, "room:lobby", "new_msg", {"body": "anyone around for a quick call?", "user_id": "usr_9182736455"}]), False)


def todoist(ev, v=0):
    cmds = [{"type": "item_add", "temp_id": UUID, "uuid": UUID2,
             "args": {"content": "Book the dentist for next week", "project_id": "2203306141"}}]
    send(ev, "Todoist", "api.todoist.com", "/sync/v9/sync",
         "resource_types=%5B%22all%22%5D&sync_token=VRyFHr0Qo3Hr--pzINyT6nax4vW7X2YG5RQlw3lB&commands="
         + json.dumps(cmds).replace(" ", "%20").replace('"', "%22"), ctype=FORM)


def mastodon(ev, v=0):
    send(ev, "Zen", "mastodon.social", "/api/v1/statuses",
         {"status": "Shipping the new importer today, feedback welcome", "visibility": "public", "media_ids": []})


def reddit(ev, v=0):
    send(ev, "Zen", "www.reddit.com", "/api/comment",
         "api_type=json&thing_id=t3_1abcd2e&text=This+matches+what+we+saw+after+the+upgrade&uh=f0f1f2f3f4f5f6f7f8f9", ctype=FORM)


def x_post(ev, v=0):
    send(ev, "Zen", "x.com", "/i/api/graphql/a1b2C3d4E5f6G7h8I9j0Kl/CreateTweet", {
        "variables": {"tweet_text": "Writing tests at midnight so the morning is calm", "dark_request": False,
                      "media": {"media_entities": [], "possibly_sensitive": False}},
        "features": {"responsive_web_edit_tweet_api_enabled": True}, "queryId": "a1b2C3d4E5f6G7h8I9j0Kl"})


def mattermost_push(ev, v=0):
    post = {"id": "k8q1w2e3r4t5y6u7i8o9p0a1s2", "channel_id": "z9y8x7w6v5u4t3s2r1q0p9o8n7", "user_id": "a1b2c3d4e5f6g7h8i9j0k1l2m3",
            "message": "Could you review my pull request today?", "type": ""}
    ev.ws("Mattermost", "chat.acme.dev", "/api/v4/websocket", json.dumps({
        "event": "posted", "seq": 12, "broadcast": {"channel_id": post["channel_id"]},
        "data": {"channel_display_name": "Platform", "sender_name": "@priya", "post": json.dumps(post)}}), False)


def signalr(ev, v=0):
    ev.ws("Zen", "hub.example.com", "/chathub?id=Gk2vU7p0c8wqZ1", json.dumps(
        {"type": 1, "target": "ReceiveMessage", "arguments": ["sam", "Are we still on for lunch on Friday?"]}) + "\x1e", False)


def actioncable(ev, v=0):
    ev.ws("Zen", "app.example.com", "/cable", json.dumps({
        "identifier": json.dumps({"channel": "RoomChannel", "room_id": 42}),
        "message": {"body": "Standup moved to ten thirty", "sender": "Lee"}}), False)


def youtube_comment(ev, v=0):
    send(ev, "Zen", "www.youtube.com", "/youtubei/v1/comment/create_comment?prettyPrint=false", {
        "context": {"client": {"hl": "en-GB", "clientName": "WEB", "clientVersion": "2.20261109.01.00",
                               "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",
                               "timeZone": "Europe/London"}},
        "createCommentParams": "EgtkUXc0dzlXZ1hjUSoCCABQBw%3D%3D",
        "commentText": "Great walkthrough, the part about caching saved me an afternoon"})


def matrix_element(ev, v=0):
    send(ev, "Element", "matrix-client.matrix.org",
         f"/_matrix/client/v3/rooms/{('!OGEhHVWSdvArJzumhm', '!kXJmpqLcBnRtYwZaPd')[v]}:matrix.org/send/m.room.message/m173120000{v}123.4",
         {"msgtype": "m.text", "body": "On my way, be there in ten minutes"}, method="PUT")


def supabase_insert(ev, v=0):
    send(ev, "Zen", "xyzcompanyabc.supabase.co", "/rest/v1/messages?select=*",
         {"room_id": 3, "content": "Uploaded the invoices for October", "sender": UUID})


def rocketchat_ddp(ev, v=0):
    ev.ws("Rocket.Chat", "open.rocket.chat", "/websocket", json.dumps({
        "msg": "method", "method": "sendMessage", "id": "17",
        "params": [{"_id": "Jk3nd8sHq2Lm9xPz4", "rid": "GENERAL", "msg": "Deploy is done, please check staging"}]}), True)


def jsonrpc_app(ev, v=0):
    send(ev, "Notes", "rpc.example.com", "/rpc",
         {"jsonrpc": "2.0", "id": 7, "method": "notes.create", "params": {"title": "Groceries", "body": "Buy oat milk and coffee beans"}})


# name, player, what a person expects: (klass, a phrase the stored text must contain)
SHAPES = [
    ("Notion saveTransactions", notion, "action", "launch plan"),
    ("Linear GraphQL mutation", linear, "action", "Login page throws a 500"),
    ("Gmail web send (positional batch)", gmail, "action", "updated budget"),
    ("Google Calendar event create", google_calendar, "action", "Design review with Priya"),
    ("Jira comment (REST, ADF body)", jira, "action", "export button does nothing"),
    ("Trello card comment", trello, "action", "next sprint"),
    ("Asana task comment", asana, "action", "pick this up"),
    ("Figma comment", figma, "action", "button bigger"),
    ("Telegram Web (MTProto over WS)", telegram_web, "incoming", ""),
    ("tRPC app (cal.com-style)", trpc, "action", "Works for me"),
    ("socket.io chat push", socketio, "incoming", "deploy finished"),
    ("SSE chat app prompt", sse_chat, "action", "bakery in Leeds"),
    ("SSE chat app incoming message", sse_push, "incoming", "invoice before noon"),
    ("Microsoft Graph sendMail", ms_graph_sendmail, "action", "renewal terms"),
    ("Zendesk ticket reply", zendesk, "action", "refunded the duplicate charge"),
    ("Microsoft Teams web send (HTML body)", teams_web, "action", "join the standup"),
    ("GitHub web issue comment (multipart)", github_web_comment, "action", "fixed in the latest build"),
    ("Phoenix channels push", phoenix_channels, "incoming", "quick call"),
    ("Todoist add task (sync commands)", todoist, "action", "Book the dentist"),
    ("Mastodon post", mastodon, "action", "new importer"),
    ("Reddit comment (form)", reddit, "action", "after the upgrade"),
    ("X post (GraphQL by path)", x_post, "action", "tests at midnight"),
    ("Mattermost push (JSON in a string)", mattermost_push, "incoming", "review my pull request"),
    ("SignalR hub push", signalr, "incoming", "lunch on Friday"),
    ("ActionCable push", actioncable, "incoming", "Standup moved"),
    ("YouTube comment", youtube_comment, "action", "caching saved me"),
    ("Matrix / Element send", matrix_element, "action", "be there in ten minutes"),
    ("Supabase (PostgREST) insert", supabase_insert, "action", "invoices for October"),
    ("Rocket.Chat DDP send", rocketchat_ddp, "action", "check staging"),
    ("JSON-RPC notes app", jsonrpc_app, "action", "oat milk"),
]


def run(shape, events) -> dict:
    """Play one shape into a fresh Events and say what came out."""
    name, play, want_klass, want_text = shape
    play(events, 0)
    play(events, 1)          # the same gesture on a second object: must land in the same kind
    rows = events.store.q("SELECT * FROM events ORDER BY id")
    types = list(events.types.values())
    hit = next((r for r in rows if want_text and want_text.lower() in (r["text"] or "").lower()), None)
    if hit is None:
        cand = sorted(types, key=lambda t: (t["klass"] not in ("action", "incoming"), -t["n_text"]))
        t = cand[0] if cand else None
        text = next((r["text"] for r in rows if t and r["sig"] == t["sig"]), "")
    else:
        t, text = events.types[hit["sig"]], hit["text"]
    out = {"name": name, "want": want_klass, "kinds": len(types), "sig": t["sig"] if t else None,
           "app": t["app"] if t else None, "klass": t["klass"] if t else None, "label": t["label"] if t else None,
           "text": text, "problems": []}
    if t is None:
        out["problems"].append("no event kind at all")
        return out
    if t["klass"] != want_klass:
        out["problems"].append(f"classified {t['klass']}, expected {want_klass}")
    if not want_text or hit is None:
        out["problems"].append("human text not found" if want_text else "no readable text (encrypted payload)")
    if hit is not None and t["n"] < 2:
        out["problems"].append("every object gets its own kind (an id in the path is not templated)")
    if not readable(t["label"]):
        out["problems"].append(f"label {t['label']!r} is not readable")
    return out


if __name__ == "__main__":
    import tempfile

    from engine.events import Events
    from engine.store import Store
    ok = 0
    for shape in SHAPES:
        ev = Events(Store(os.path.join(tempfile.mkdtemp(), "m.db")))
        r = run(shape, ev)
        ok += not r["problems"]
        verdict = "ok  " if not r["problems"] else "FAIL"
        print(f"{verdict} {r['name'][:38]:38} | {r['app'] or '-':18} | {r['klass'] or '-':10} | {r['label'] or '-':34} | "
              f"{(r['text'] or '')[:46]!r}" + (f"\n       -> {'; '.join(r['problems'])}   [{r['sig']}]" if r["problems"] else ""))
    print(f"\n{ok}/{len(SHAPES)} shapes give a usable kind")
