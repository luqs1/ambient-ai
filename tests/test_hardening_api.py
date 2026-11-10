"""The local API answers this machine's own UI and tools and nobody else.

A web page that re-points its own domain at 127.0.0.1 (DNS rebinding) arrives with its own name in Host;
a page that posts across sites arrives with its own Origin or `Sec-Fetch-Site: cross-site`. All refused,
on every route. curl and the desktop shell (no Origin, a local Host) keep working.
"""
import http.client
import json

import pytest

from engine import ui

ROUTES = ["/", "/panel", "/traffic", "/api/flows", "/api/summary", "/api/apps", "/api/setup", "/api/shield",
          "/api/status", "/api/triggers", "/api/moments", "/api/activity", "/api/events", "/api/flow/1",
          "/api/icon/Nothing.png", "/assets/app.css", "/mcp"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """One real UI server on a free port for the module (stopping one takes half a second)."""
    from engine.events import Events
    from engine.moments import Moments
    from engine.store import Store
    mp = pytest.MonkeyPatch()
    mp.setattr(ui, "setup_status", lambda mode="": {"mode": mode})      # no OS probing in tests
    mp.setattr(ui.osal, "app_icon_png", lambda name: None)
    store = Store(str(tmp_path_factory.mktemp("api") / "ambient.db"))
    events = Events(store)
    m = Moments(store, events)
    for t in m.triggers():
        m.delete_trigger(t["id"])
    srv = ui.serve(store, 0, m, events=events)
    yield srv, m
    srv.shutdown()
    srv.server_close()
    mp.undo()


@pytest.fixture
def moments(server):
    server[1].set_watch("Slack", True)
    return server[1]


@pytest.fixture
def api(server, moments):
    """`api(method, path, ...)` -> (status, parsed body or raw bytes)."""
    port = server[0].server_address[1]

    def call(method, path, *, host="127.0.0.1:{port}", headers=None, body=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            c.putheader("Host", host.format(port=port))
        data = json.dumps(body).encode() if body is not None else b""
        if body is not None:
            c.putheader("content-type", ctype)
            c.putheader("content-length", str(len(data)))
        for k, v in (headers or {}).items():
            c.putheader(k, v.format(port=port))
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    call.port = port
    return call


WRITE = {"X-Ambient": "1"}


# ---------- Host: every route, reads included ----------
@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "LOCALHOST:{port}",
                                  "127.0.0.1"])        # the bare name is what the desktop shell's probe sends
def test_local_names_are_served(api, host):
    assert api("GET", "/api/summary", host=host)[0] == 200
    assert api("GET", "/", host=host)[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1.evil.example:{port}",
                                  "127.0.0.1:1", "localhost.evil.example", "127.0.0.1:{port}.evil.example",
                                  "[::2]:{port}", "0.0.0.0:{port}", "", None])
@pytest.mark.parametrize("path", ROUTES)
def test_a_foreign_host_reads_nothing(api, host, path):
    status, body = api("GET", path, host=host)
    assert status == 403 and body["error"] == "forbidden"


def test_a_foreign_host_cannot_post(api, moments):
    for path in ("/mcp", "/api/watch", "/api/triggers"):
        status, _ = api("POST", path, host="evil.example:{port}", headers=WRITE, body={"app": "Slack", "on": False})
        assert status == 403
    assert moments.watching("Slack")


def test_two_host_headers_are_refused(api):
    c = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
    c.putrequest("GET", "/api/summary", skip_host=True)
    c.putheader("Host", f"127.0.0.1:{api.port}")
    c.putheader("Host", "evil.example")
    c.endheaders()
    assert c.getresponse().status == 403
    c.close()


# ---------- Origin: exact, never a prefix ----------
@pytest.mark.parametrize("origin", ["http://127.0.0.1:{port}", "http://localhost:{port}", "http://[::1]:{port}"])
def test_the_ui_itself_may_write(api, moments, origin):
    status, body = api("POST", "/api/watch", headers={**WRITE, "Origin": origin}, body={"app": "Slack", "on": False})
    assert status == 200 and body == {"ok": True}
    assert not moments.watching("Slack")


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1.evil.example", "http://127.0.0.1:{port}.evil.example", "http://localhost.evil.example:{port}",
    "http://localhostevil.example", "https://127.0.0.1:{port}", "http://127.0.0.1", "http://127.0.0.1:1",
    "http://evil.example", "null", "", "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"])
def test_another_origin_may_not_write(api, moments, origin):
    status, body = api("POST", "/api/watch", headers={**WRITE, "Origin": origin}, body={"app": "Slack", "on": False})
    assert status == 403 and body["error"] == "forbidden"
    assert moments.watching("Slack")                 # nothing changed
    assert api("POST", "/mcp", headers={"Origin": origin},
               body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 403


def test_another_origin_may_not_read_either(api):
    assert api("GET", "/api/flows", headers={"Origin": "http://evil.example"})[0] == 403
    assert api("GET", "/api/flows", headers={"Origin": "http://127.0.0.1:{port}"})[0] == 200


# ---------- Sec-Fetch-Site ----------
@pytest.mark.parametrize("site", ["cross-site", "same-site", "Cross-Site"])
@pytest.mark.parametrize("path", [p for p in ROUTES if p.startswith(("/api/", "/mcp"))])
def test_cross_site_fetches_of_the_api_are_refused(api, site, path):
    assert api("GET", path, headers={"Sec-Fetch-Site": site})[0] == 403


def test_cross_site_post_is_refused(api, moments):
    status, _ = api("POST", "/api/watch", headers={**WRITE, "Sec-Fetch-Site": "cross-site"},
                    body={"app": "Slack", "on": False})
    assert status == 403 and moments.watching("Slack")


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_the_pages_own_fetches_pass(api, site):
    assert api("GET", "/api/summary", headers={"Sec-Fetch-Site": site})[0] == 200


def test_a_link_from_elsewhere_may_still_open_the_page(api):
    # e.g. the shell's bundled loading page sending its window to the engine
    status, body = api("GET", "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    assert status == 200 and b"<html" in body.lower()


# ---------- what must keep working ----------
def test_curl_style_calls_still_work(api, moments):
    """No Origin, no Sec-Fetch-*, Host 127.0.0.1:port: what the scripts in tests/*.sh and scripts/live.sh send."""
    assert api("GET", "/api/status")[0] == 200
    assert api("GET", "/api/triggers")[0] == 200
    assert isinstance(api("GET", "/api/flows")[1], list)
    assert api("POST", "/api/watch", headers=WRITE, body={"app": "Slack", "on": False}) == (200, {"ok": True})
    assert not moments.watching("Slack")
    assert api("GET", "/api/summary", host="localhost:{port}")[0] == 200       # tests/smoke.sh


def test_writes_still_need_the_ambient_header(api, moments):
    status, body = api("POST", "/api/watch", body={"app": "Slack", "on": False})
    assert status == 403 and "X-Ambient" in body["error"]
    assert moments.watching("Slack")


def test_mcp_still_answers_a_local_agent(api):
    status, body = api("POST", "/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert status == 200 and body["id"] == 1 and "result" in body
    assert api("POST", "/mcp", host="localhost:{port}", body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0] == 200


def test_mcp_wants_real_json(api):
    """A cross-site form post can say `text/plain; x=json` without a preflight; that is not JSON."""
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert api("POST", "/mcp", body=msg, ctype="text/plain; x=json")[0] == 415
    assert api("POST", "/mcp", body=msg, ctype="application/json; charset=utf-8")[0] == 200


def test_no_cors_preflight_is_answered(api):
    status, _ = api("OPTIONS", "/api/watch", headers={"Origin": "http://evil.example",
                                                      "Access-Control-Request-Method": "POST"})
    assert status == 501


def test_refusal_rules_directly():
    from email.message import Message

    def why(path="/api/flows", **h):
        m = Message()
        for k, v in h.items():
            m[k.replace("_", "-")] = v
        return ui.refusal(m, path, 8787)

    assert why(Host="127.0.0.1:8787") is None
    assert why(Host="localhost:8787", Origin="http://localhost:8787") is None
    assert why(Host="127.0.0.1:8788")
    assert why(Host="127.0.0.1:8787", Origin="http://127.0.0.1:8787.evil.example")
    assert why(Host="127.0.0.1:8787", Sec_Fetch_Site="cross-site")
    assert why("/", Host="127.0.0.1:8787", Sec_Fetch_Site="cross-site") is None
    assert "http://127.0.0.1" not in ui.local_origins(8787) and "http://127.0.0.1" in ui.local_origins(80)
