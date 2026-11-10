"""The MCP server beside Claude and Codex (engine/mcp_app.py): tools, the ui:// view, nothing saved without
confirm, toggling keeps the rest of a trigger, and the same tools over stdio, over the engine's /mcp and
through the HTTP client against a real local API server. Synthetic data only."""
import io
import json

import pytest

from engine import mcp_app, ui
from engine.mcp_app import UI_MIME, UI_URI, Client, HttpClient, LocalClient, handle


class Fake(Client):
    """Today's trigger API, in memory."""

    def __init__(self):
        self.rows = [{"id": 1, "name": "Draft a reply", "text": "someone DMs me", "sigs": ["*:dm"], "action": "draft",
                      "instruction": "", "auto": 0, "enabled": 1, "condition": None, "threshold": 0.5, "semantic": 0,
                      "agent": "", "fired": 3, "last_fired": 1.0,
                      "kinds": [{"sig": "*:dm", "label": "someone DMs me", "app": "your chat apps"}]}]
        self.saved = []

    def triggers(self): return [dict(r) for r in self.rows]
    def suggest(self, text):
        return {"kinds": [{"sig": "*:dm", "label": "someone DMs me", "app": "your chat apps", "picked": True},
                          {"sig": "slack:dm", "label": "someone DMs me", "app": "Slack", "n": 4, "picked": False}],
                "sure": True, "about_content": True, "model": "local"}
    def preview(self, d): return {"checked": 4, "matched": 1, "span_hours": 2.0,
                                  "samples": [{"who": "Priya", "text": "send me the deck?", "passed": True, "p": 0.9}]}
    def save(self, t):
        self.saved.append(t)
        if t.get("id"):
            r = next(r for r in self.rows if r["id"] == t["id"])
            r.update({k: v for k, v in t.items() if k != "id"})
            return {"ok": True, "id": t["id"]}
        self.rows.append({**t, "id": len(self.rows) + 1, "fired": 0, "kinds": []})
        return {"ok": True, "id": len(self.rows)}
    def moments(self, limit): return [{"id": 9, "app": "Slack", "kind": "dm", "who": "Priya", "text": "hi", "status": "new"}]


def rpc(client, method, **params):
    return handle(client, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def call(client, name, **args):
    return rpc(client, "tools/call", name=name, arguments=args)["result"]


def test_tools_point_at_the_view_and_the_view_is_served():
    c = Fake()
    init = rpc(c, "initialize", protocolVersion="2025-06-18")["result"]
    assert init["protocolVersion"] == "2025-06-18" and "io.modelcontextprotocol/ui" in init["capabilities"]["extensions"]
    tools = {t["name"]: t for t in rpc(c, "tools/list")["result"]["tools"]}
    assert set(tools) == {"list_triggers", "create_trigger", "toggle_trigger", "recent_moments"}
    for n in ("list_triggers", "create_trigger", "toggle_trigger"):
        assert tools[n]["_meta"]["ui"]["resourceUri"] == UI_URI
    assert rpc(c, "resources/list")["result"]["resources"][0]["mimeType"] == UI_MIME
    page = rpc(c, "resources/read", uri=UI_URI)["result"]["contents"][0]
    assert page["mimeType"] == UI_MIME and page["text"].startswith("<!doctype html>")
    assert "ui/initialize" in page["text"] and "ui/notifications/tool-result" in page["text"]
    assert "const top" not in page["text"]                  # window.top is read-only: a global `top` breaks the page
    assert "error" in rpc(c, "resources/read", uri="ui://other")
    assert handle(c, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_list_triggers_says_what_each_one_watches():
    r = call(Fake(), "list_triggers")
    t = r["structuredContent"]["triggers"][0]
    assert t == {**t, "when": "someone DMs me", "on": True, "mode": "ask first", "fired": 3,
                 "watches": [{"app": "your chat apps", "label": "someone DMs me"}],
                 "checked": "every time it happens"}
    assert "fired 3x" in r["content"][0]["text"]


def test_create_trigger_saves_nothing_without_confirm():
    c = Fake()
    r = call(c, "create_trigger", intent="someone asks me for a file")
    v = r["structuredContent"]
    assert c.saved == [] and v["saved"] is None and not r["isError"]
    assert v["draft"]["sigs"] == ["*:dm"] and v["content_check"] and v["backtest"]["matched"] == 1
    assert "NOT SAVED" in r["content"][0]["text"] and "*:dm" in r["content"][0]["text"]


def test_create_trigger_with_confirm_saves_the_chosen_kinds():
    c = Fake()
    r = call(c, "create_trigger", intent="When someone asks me for a file", then="Find it.", kinds=["slack:dm"],
             automatic=True, confirm=True)
    assert r["structuredContent"]["saved"] == 2
    assert c.saved == [{**c.saved[0], "sigs": ["slack:dm"], "text": "When someone asks me for a file",
                        "instruction": "Find it.", "auto": True, "action": "ask", "semantic": True}]


def test_create_trigger_refuses_an_empty_sentence_and_an_unsure_guess():
    c = Fake()
    assert call(c, "create_trigger", intent="  ")["isError"]
    c.suggest = lambda text: {"kinds": [{"sig": "slack:dm", "label": "x", "app": "Slack", "picked": False}], "sure": False}
    r = call(c, "create_trigger", intent="something odd happens", confirm=True)
    assert r["isError"] and c.saved == []


def test_toggle_keeps_the_rest_of_the_trigger():
    c = Fake()
    r = call(c, "toggle_trigger", id=1)
    assert not r["isError"] and r["structuredContent"]["triggers"][0]["on"] is False
    sent = c.saved[-1]
    assert sent["enabled"] is False and sent["sigs"] == ["*:dm"] and sent["action"] == "draft" and sent["id"] == 1
    assert call(c, "toggle_trigger", id=1, on=True)["structuredContent"]["triggers"][0]["on"] is True
    assert call(c, "toggle_trigger", id=99)["isError"]


def test_a_stopped_app_is_an_error_not_a_crash():
    r = call(HttpClient("http://127.0.0.1:9"), "list_triggers")
    assert r["isError"] and "isn't answering" in r["content"][0]["text"]


def test_stdio_one_message_per_line():
    inp = io.StringIO("\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "recent_moments", "arguments": {}}},
    ]) + "\nnot json\n")
    out = io.StringIO()
    mcp_app.serve_stdio(Fake(), inp, out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [x.get("id") for x in lines] == [1, 2, None]
    assert "Priya" in lines[1]["result"]["content"][0]["text"] and lines[2]["error"]["code"] == -32700


# ---------- against a real engine: the in-process /mcp and the HTTP client over the local API ----------
@pytest.fixture
def served(moments, events, monkeypatch):
    monkeypatch.setattr(ui, "setup_status", lambda mode="": {"mode": mode})
    srv = ui.serve(moments.store, 0, moments, events=events)
    yield srv, moments
    srv.shutdown()
    srv.server_close()


def test_engine_mcp_has_the_trigger_tools_and_the_moment_tools(moments):
    names = {t["name"] for t in mcp_app.engine_handle(moments, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
             ["result"]["tools"]}
    assert {"list_triggers", "create_trigger", "toggle_trigger", "list_moments", "wait_for_moment"} <= names
    r = mcp_app.engine_handle(moments, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "list_moments", "arguments": {}}})
    assert r["result"]["content"][0]["text"] == "[]"


def test_http_client_creates_and_toggles_through_the_local_api(served):
    srv, m = served
    c = HttpClient(f"http://127.0.0.1:{srv.server_address[1]}")
    r = call(c, "create_trigger", intent="I create an issue in Linear", kinds=["Linear|POST /graphql#IssueCreate"])
    assert m.triggers() == []                                   # a draft is not a trigger
    r = call(c, "create_trigger", intent="I create an issue in Linear", kinds=["Linear|POST /graphql#IssueCreate"],
             confirm=True)
    tid = r["structuredContent"]["saved"]
    t = next(t for t in m.triggers() if t["id"] == tid)
    assert t["enabled"] == 1 and t["auto"] == 0 and t["sigs"] == ["Linear|POST /graphql#IssueCreate"]
    call(c, "toggle_trigger", id=tid)
    assert next(t for t in m.triggers() if t["id"] == tid)["enabled"] == 0
    assert call(c, "list_triggers")["structuredContent"]["triggers"][0]["on"] is False


def test_engine_mcp_endpoint_serves_the_view(served):
    srv, _ = served
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": UI_URI}})
    conn.request("POST", "/mcp", body, {"content-type": "application/json"})
    r = json.loads(conn.getresponse().read())
    assert r["result"]["contents"][0]["mimeType"] == UI_MIME


def test_local_client_is_the_same_api(moments):
    c = LocalClient(moments)
    r = call(c, "create_trigger", intent="someone DMs me", kinds=["*:dm"], confirm=True)
    assert r["structuredContent"]["saved"] and [t["text"] for t in moments.triggers()] == ["someone DMs me"]
