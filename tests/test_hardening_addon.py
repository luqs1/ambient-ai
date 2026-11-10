"""What the addon stores and what it turns into events, driven with mitmproxy's synthetic flows.

The stored copy of an exchange has its credentials masked while the live flow goes out untouched (so the
app's request works and Veil keeps rewriting it); the reply to a sign-in or payment request is not stored;
and nothing sent to a model provider's own API becomes an event, whether request, stream or socket.
"""
import json

import pytest
from mitmproxy import http
from mitmproxy.test import taddons, tflow

from engine import addon as A
from engine.events import Events
from engine.store import NOT_STORED, REMOVED, Store
from helpers import fake_secrets, kinds

SECRET = "s3cr3t-" + "Zx9Qw7" * 4


class Spy:
    """Stands in for Events / Recognizers: remembers what the addon handed over."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a))

    def named(self, name):
        return [a for n, a in self.calls if n == name]


@pytest.fixture
def addon(tmp_path):
    """An AmbientAddon with its options registered and a temp database, never started (no proxy, no UI)."""
    a = A.AmbientAddon()
    with taddons.context(a):
        a.store = Store(str(tmp_path / "ambient.db"))
        a.events = Spy()
        a.rec = Spy()
        yield a


def exchange(host, path, *, method="POST", req_headers=(), body=b"", resp_headers=(), resp_body=b"{}",
             resp_ctype="application/json", status=200):
    f = tflow.tflow(resp=True)
    f.request = http.Request.make(method, f"https://{host}{path}", body,
                                  {"content-type": "application/json", **dict(req_headers)})
    f.response = http.Response.make(status, resp_body, {"content-type": resp_ctype, **dict(resp_headers)})
    return f


def socket_frame(host, path, text, from_client=True):
    f = tflow.twebsocketflow()
    f.request.host = host
    f.request.headers["host"] = host
    f.request.path = path
    f.websocket.messages[:] = [tflow.twebsocketflow().websocket.messages[0]]
    m = f.websocket.messages[-1]
    m.content, m.from_client = text.encode(), from_client
    return f


def row(addon):
    rows = addon.store.q("SELECT * FROM flows ORDER BY id")
    assert len(rows) == 1
    return rows[0]


# ---------- credentials ----------
def test_stored_copy_is_masked_and_the_live_flow_is_not(addon):
    f = exchange("api.example.com", f"/v1/items?access_token={SECRET}&page=2", method="GET",
                 req_headers={"Authorization": f"Bearer {SECRET}", "Cookie": f"sid={SECRET}", "X-Api-Key": SECRET,
                              "X-CSRF-Token": SECRET, "X-Custom-Session": SECRET, "Accept": "application/json"},
                 resp_headers={"Set-Cookie": f"sid={SECRET}; HttpOnly", "X-Request-Id": "r-1"},
                 resp_body=b'{"items":[]}')
    live_req, live_resp = list(f.request.headers.fields), list(f.response.headers.fields)
    live_url = f.request.pretty_url
    addon._record_http(f)

    r = row(addon)
    req, resp = dict(json.loads(r["req_headers"])), dict(json.loads(r["resp_headers"]))
    for name in ("Authorization", "Cookie", "X-Api-Key", "X-CSRF-Token", "X-Custom-Session"):
        assert req[name] == REMOVED, name                    # the name stays, the value is gone
    assert req["Accept"] == "application/json" and req["content-type"] == "application/json"
    assert resp["Set-Cookie"] == REMOVED and resp["X-Request-Id"] == "r-1"
    assert r["path"] == f"/v1/items?access_token={REMOVED}&page=2"
    assert r["url"] == f"https://api.example.com/v1/items?access_token={REMOVED}&page=2"
    assert r["resp_body"] == '{"items":[]}'
    assert SECRET not in json.dumps({k: v for k, v in r.items() if k != "findings"})

    # what the app really sends and receives is unchanged
    assert list(f.request.headers.fields) == live_req and list(f.response.headers.fields) == live_resp
    assert f.request.pretty_url == live_url and f.request.headers["Authorization"] == f"Bearer {SECRET}"
    assert f.response.headers["Set-Cookie"] == f"sid={SECRET}; HttpOnly"


def test_socket_urls_are_masked_too(addon):
    addon.websocket_message(socket_frame("chat.example.com", f"/socket?token={SECRET}&v=3", '{"type":"hello"}'))
    assert row(addon)["path"] == f"/socket?token={REMOVED}&v=3"


@pytest.mark.parametrize("path", ["/api/login", "/oauth/token", "/v1/payment_intents/pi_1/confirm"])
def test_sign_in_and_payment_replies_are_not_stored(addon, path):
    assert A.SENSITIVE_PATH.search(path)
    f = exchange("app.example.com", path, body=json.dumps({"user": "sam", "password": SECRET}).encode(),
                 resp_body=json.dumps({"session": SECRET, "card": "4242"}).encode(),
                 resp_headers={"Set-Cookie": f"sid={SECRET}"})
    addon._record_http(f)
    r = row(addon)
    assert r["req_body"] == NOT_STORED and r["resp_body"] == NOT_STORED
    assert SECRET not in json.dumps(r)
    assert json.loads(f.response.content)["session"] == SECRET          # the app still gets its reply


def test_ordinary_replies_are_still_stored(addon):
    addon._record_http(exchange("app.example.com", "/api/notes", body=b'{"text":"hi"}', resp_body=b'{"id":7}'))
    r = row(addon)
    assert r["req_body"] == '{"text":"hi"}' and r["resp_body"] == '{"id":7}'


def test_veil_still_rewrites_the_live_request(addon):
    """Masking the stored copy must not get in Veil's way: the secret in a tool result is replaced in the
    request that leaves, the provider key in the header still goes out, and neither is stored."""
    aws = fake_secrets()["aws_access_key"]
    key = fake_secrets()["anthropic_key"]
    body = {"model": "m", "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                      "content": f"AWS_ACCESS_KEY_ID={aws}"}]}]}
    f = exchange("api.anthropic.com", "/v1/messages", req_headers={"x-api-key": key},
                 body=json.dumps(body).encode(), resp_body=b'{"content":[],"usage":{"input_tokens":3}}')
    addon.request(f)
    assert aws not in f.request.get_text() and "[[ambient:" in f.request.get_text()
    assert f.metadata["shield"]["redacted"]
    assert f.request.headers["x-api-key"] == key              # the real request keeps its key
    addon.response(f)
    r = row(addon)
    assert dict(json.loads(r["req_headers"]))["x-api-key"] == REMOVED
    assert aws not in r["req_body"] and key not in json.dumps({k: v for k, v in r.items() if k != "findings"})
    assert json.loads(r["shield"])["redacted"] and json.loads(r["llm"])["provider"] == "anthropic"
    assert f.request.headers["x-api-key"] == key


# ---------- model providers' APIs never become events ----------
PROVIDERS = [
    ("api.openai.com", "/v1/chat/completions"), ("api.openai.com", "/v1/responses"),
    ("api.anthropic.com", "/v1/messages"), ("generativelanguage.googleapis.com", "/v1beta/models/g:generateContent"),
    ("api.x.ai", "/v1/chat/completions"), ("api.mistral.ai", "/v1/chat/completions"),
    ("openrouter.ai", "/api/v1/chat/completions"), ("api.groq.com", "/openai/v1/chat/completions"),
    ("api.together.xyz", "/v1/chat/completions"), ("api.deepseek.com", "/chat/completions"),
    ("chatgpt.com", "/backend-api/codex/responses"),
    ("my-team.openai.azure.com", "/openai/deployments/d/chat/completions"),
    ("bedrock-runtime.us-east-1.amazonaws.com", "/model/m/converse"),
    ("us-central1-aiplatform.googleapis.com", "/v1/projects/p/locations/l/publishers/google/models/g:generateContent"),
]
PROMPT = {"model": "m", "messages": [{"role": "user", "content": "Please draft the quarterly letter to investors"}]}
SSE = b'data: {"type":"content_block_delta","delta":{"text":"Dear investors, here is the letter"}}\n\n'


def test_every_veil_host_counts_as_a_provider():
    assert all(A.provider_api(h) for h in A.SHIELD_HOSTS)
    for h in ("api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "api.x.ai",
              "api.mistral.ai", "openrouter.ai", "api.groq.com", "API.OpenAI.com"):
        assert A.provider_api(h), h
    for h, p in (("chatgpt.com", "/backend-api/conversation"), ("chatgpt.com", "/"), ("claude.ai", "/api/chat"),
                 ("slack.com", "/api/chat.postMessage"), ("notopenai.com", "/v1/chat/completions"),
                 ("api.openai.com.evil.example", "/v1/chat/completions"), ("", "")):
        assert not A.provider_api(h, p), (h, p)


@pytest.mark.parametrize("host, path", PROVIDERS)
def test_provider_requests_and_streams_are_not_events(addon, host, path):
    addon._record_http(exchange(host, path, body=json.dumps(PROMPT).encode(),
                                resp_body=b'{"choices":[{"message":{"content":"Dear investors"}}]}'))
    addon._record_http(exchange(host, path, body=json.dumps({**PROMPT, "stream": True}).encode(),
                                resp_body=SSE, resp_ctype="text/event-stream"))
    assert addon.events.calls == []
    assert len(addon.store.q("SELECT id FROM flows")) == 2          # still recorded under "details"


@pytest.mark.parametrize("host, path", PROVIDERS[:4] + [("chatgpt.com", "/backend-api/codex/realtime")])
def test_provider_sockets_are_not_events(addon, host, path):
    addon.websocket_message(socket_frame(host, path, json.dumps({"type": "input_text", "text": PROMPT["messages"][0]["content"]})))
    addon.websocket_message(socket_frame(host, path, json.dumps({"type": "response.text", "text": "Dear investors"}), from_client=False))
    assert addon.events.calls == []
    assert len(addon.store.q("SELECT id FROM flows")) == 2


def test_other_hosts_still_become_events(addon):
    """The control: the same shapes anywhere else are handed to the event layer, all three ways."""
    addon._record_http(exchange("tracker.example.com", "/api/issues/42/comments", body=b'{"body":"Looks good to me"}'))
    addon._record_http(exchange("chat.example.com", "/api/rooms/7/stream", method="GET", resp_body=SSE,
                                resp_ctype="text/event-stream"))
    addon._record_http(exchange("chatgpt.com", "/backend-api/conversation", body=json.dumps(PROMPT).encode()))
    addon.websocket_message(socket_frame("chat.example.com", "/socket", '{"type":"message","text":"lunch at one?"}'))
    assert [a[1] for a in addon.events.named("http")] == ["tracker.example.com", "chat.example.com", "chatgpt.com"]
    assert [a[1] for a in addon.events.named("sse")] == ["chat.example.com"]
    assert [a[1] for a in addon.events.named("ws")] == ["chat.example.com"]


def test_no_provider_kind_reaches_the_catalog(tmp_path):
    """End to end with the real event layer: after provider traffic of all three kinds, nothing is catalogued."""
    a = A.AmbientAddon()
    with taddons.context(a):
        a.store = Store(str(tmp_path / "ambient.db"))
        a.events = Events(a.store)
        a.rec = Spy()
        for host, path in PROVIDERS:
            for i in range(3):
                body = {**PROMPT, "messages": [{"role": "user", "content": f"Please draft letter number {i} to investors"}]}
                a._record_http(exchange(host, path, body=json.dumps(body).encode()))
                a._record_http(exchange(host, path, body=json.dumps(body).encode(), resp_body=SSE,
                                        resp_ctype="text/event-stream"))
                a.websocket_message(socket_frame(host, path, json.dumps({"type": "message", "text": f"draft {i} please"})))
        a.events.flush()
        assert kinds(a.events) == []
        assert a.store.q("SELECT COUNT(*) n FROM events")[0]["n"] == 0
        assert a.store.q("SELECT COUNT(*) n FROM event_types")[0]["n"] == 0
        # the control, so the assertion above is not vacuous
        for i in range(3):
            a._record_http(exchange("tracker.example.com", "/api/issues/comments",
                                    body=json.dumps({"issue": 40 + i, "body": f"Looks good to me, shipping {i}"}).encode()))
        a.events.flush()
        assert kinds(a.events)
