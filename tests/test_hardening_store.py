"""What reaches the disk, how long it stays, and who may read the file.

Credential header values and credential parameters in URLs are replaced before a row is written; rows written
by older versions are rewritten once; raw exchanges older than a few days are deleted (and only those);
the database is private to the user.
"""
import json
import os
import sqlite3
import stat
import sys
import time

import pytest

from engine import store as S
from engine.store import NOT_STORED, REMOVED, Store, mask_headers, mask_url
from helpers import send, wait_for

SECRET = "s3cr3t-" + "Zx9Qw7" * 4           # assembled so no secret-shaped literal sits in the repo
DAY = 86400


def on_disk(store, path) -> bytes:
    """Every byte SQLite has written for this database, after folding the write-ahead log back in."""
    with store.lock:
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return b"".join(open(p, "rb").read() for p in (path, path + "-wal") if os.path.exists(p))


# ---------- masking ----------
@pytest.mark.parametrize("name", [
    "Authorization", "authorization", "Proxy-Authorization", "Cookie", "Set-Cookie", "X-Api-Key", "api-key",
    "X-Auth-Token", "X-CSRF-Token", "X-XSRF-TOKEN", "x-amz-security-token", "X-Client-Secret", "X-Session-Id",
    "x-goog-authenticated-user", "WWW-Authenticate", "X-Goog-Api-Key", "X-Upload-Token", "x-figma-user-session"])
def test_credential_headers_lose_their_value(name):
    assert mask_headers([[name, SECRET]]) == [[name, REMOVED]]


@pytest.mark.parametrize("name", ["Content-Type", "Accept", "User-Agent", "Host", "Content-Length", "Referer",
                                  "Sec-Fetch-Mode", "X-Request-Id", "Cache-Control", "Sec-WebSocket-Key"])
def test_ordinary_headers_are_kept(name):
    assert mask_headers([[name, "value"]]) == [[name, "value"]]


def test_masking_headers_copies_and_is_stable():
    live = [["Authorization", f"Bearer {SECRET}"], ["Accept", "*/*"], ["Cookie", "a=1"], ["Cookie", "b=2"]]
    before = json.loads(json.dumps(live))
    out = mask_headers(live)
    assert live == before                                   # the caller's list is untouched
    assert out == [["Authorization", REMOVED], ["Accept", "*/*"], ["Cookie", REMOVED], ["Cookie", REMOVED]]
    assert mask_headers(out) == out
    assert mask_headers(None) is None and mask_headers([]) == []


@pytest.mark.parametrize("url, want", [
    (f"https://h.example/cb?access_token={SECRET}&x=1", f"https://h.example/cb?access_token={REMOVED}&x=1"),
    (f"/a?token={SECRET}", f"/a?token={REMOVED}"),
    (f"/a?q=cats&key={SECRET}&page=2", f"/a?q=cats&key={REMOVED}&page=2"),
    (f"/a?sig={SECRET}&signature={SECRET}", f"/a?sig={REMOVED}&signature={REMOVED}"),
    (f"/a?password={SECRET}&user=sam", f"/a?password={REMOVED}&user=sam"),
    (f"/a?X-Amz-Signature={SECRET}&X-Amz-Credential={SECRET}&X-Amz-Date=20260101",
     f"/a?X-Amz-Signature={REMOVED}&X-Amz-Credential={REMOVED}&X-Amz-Date=20260101"),
    (f"/a?api_key={SECRET}&client_secret={SECRET}&refresh_token={SECRET}",
     f"/a?api_key={REMOVED}&client_secret={REMOVED}&refresh_token={REMOVED}"),
    (f"wss://h.example/socket?session_id={SECRET}&v=2", f"wss://h.example/socket?session_id={REMOVED}&v=2"),
    (f"/cb#access_token={SECRET}&state=1", f"/cb#access_token={REMOVED}&state=1"),
    ("/search?q=token+ring&author=sam&keyword=key&monkey=1", "/search?q=token+ring&author=sam&keyword=key&monkey=1"),
    ("/plain/path", "/plain/path"), ("/token=notaquery", "/token=notaquery"), ("/a?token=", "/a?token="),
    ("", ""), (None, None),
])
def test_credentials_in_urls_lose_their_value(url, want):
    assert mask_url(url) == want
    assert mask_url(want) == want                           # idempotent


def test_nothing_secret_is_written(tmp_path):
    path = str(tmp_path / "ambient.db")
    store = Store(path)
    row = {"ts": time.time(), "kind": "http", "decrypted": 1, "app": "Zen", "host": "api.example.com", "method": "GET",
           "url": f"https://api.example.com/v1/me?access_token={SECRET}&fields=name",
           "path": f"/v1/me?access_token={SECRET}&fields=name",
           "req_headers": [["Authorization", f"Bearer {SECRET}"], ["Cookie", f"sid={SECRET}"], ["Accept", "*/*"]],
           "resp_headers": [["Set-Cookie", f"sid={SECRET}; HttpOnly"], ["Content-Type", "application/json"]],
           "req_body": "", "resp_body": '{"name":"Sam"}'}
    keep = json.loads(json.dumps(row))
    fid = store.add(row)
    assert row == keep                                       # the caller's row (and its header lists) is untouched
    got = store.get(fid)
    assert json.loads(got["req_headers"]) == [["Authorization", REMOVED], ["Cookie", REMOVED], ["Accept", "*/*"]]
    assert json.loads(got["resp_headers"]) == [["Set-Cookie", REMOVED], ["Content-Type", "application/json"]]
    assert got["url"] == f"https://api.example.com/v1/me?access_token={REMOVED}&fields=name"
    assert got["path"] == f"/v1/me?access_token={REMOVED}&fields=name"
    assert got["resp_body"] == '{"name":"Sam"}'
    store.update(fid, path=f"/v1/me?token={SECRET}")
    assert store.get(fid)["path"] == f"/v1/me?token={REMOVED}"
    assert SECRET.encode() not in on_disk(store, path)


# ---------- the one-time scrub of rows stored before masking ----------
def old_row(store, i, *, sensitive=False, ts=None):
    """A row the way older versions wrote it: straight into the table, credentials and all."""
    with store.lock:
        cur = store.db.execute(
            "INSERT INTO flows (ts, kind, decrypted, app, host, method, url, path, req_headers, resp_headers, "
            "req_body, resp_body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts or time.time(), "http", 1, "Zen", "api.example.com", "POST",
             f"https://api.example.com/items/{i}?access_token={SECRET}", f"/items/{i}?access_token={SECRET}",
             json.dumps([["Authorization", f"Bearer {SECRET}"], ["Accept", "*/*"]]),
             json.dumps([["Set-Cookie", f"sid={SECRET}"], ["Content-Type", "text/plain"]]),
             NOT_STORED if sensitive else f"body {i}", f"session={SECRET}" if sensitive else f"reply {i}"))
        store.db.commit()
        return cur.lastrowid


def test_old_rows_are_scrubbed_once(tmp_path):
    path = str(tmp_path / "ambient.db")
    store = Store(path)
    ids = [old_row(store, i, sensitive=(i == 3)) for i in range(7)]
    with store.lock:        # rows that hold no headers at all (sockets, opaque connections) are left alone
        store.db.execute("INSERT INTO flows (ts, kind, decrypted, host, path, resp_body) VALUES (?,?,?,?,?,?)",
                         (time.time(), "ws", 1, "chat.example.com", f"/socket?token={SECRET}", "hello"))
        store.db.commit()
    assert SECRET.encode() in on_disk(store, path)

    assert store.scrub_stored_credentials(batch=3) == 8            # several batches
    for i, fid in enumerate(ids):
        r = store.get(fid)
        assert json.loads(r["req_headers"]) == [["Authorization", REMOVED], ["Accept", "*/*"]]
        assert json.loads(r["resp_headers"]) == [["Set-Cookie", REMOVED], ["Content-Type", "text/plain"]]
        assert r["url"].endswith(f"/items/{i}?access_token={REMOVED}") and r["path"] == f"/items/{i}?access_token={REMOVED}"
        assert r["resp_body"] == (NOT_STORED if i == 3 else f"reply {i}")      # the reply to a sign-in goes too
        assert r["req_body"] == (NOT_STORED if i == 3 else f"body {i}")
    ws = store.q("SELECT * FROM flows WHERE kind='ws'")[0]
    assert ws["path"] == f"/socket?token={REMOVED}" and ws["resp_body"] == "hello"
    assert SECRET.encode() not in on_disk(store, path)             # gone from the file, not just from the rows
    assert store.meta(S.SCRUB_FLAG) == "done"

    # Once per database: a second call does nothing, even across a restart.
    late = old_row(store, 99)
    assert store.scrub_stored_credentials() == 0
    assert Store(path).scrub_stored_credentials() == 0
    assert SECRET in store.get(late)["req_headers"]


def test_scrub_carries_on_where_it_stopped(tmp_path):
    store = Store(str(tmp_path / "ambient.db"))
    ids = [old_row(store, i) for i in range(6)]
    assert store.scrub_stored_credentials(batch=2) == 6 and store.meta(S.SCRUB_FLAG + ":upto") == str(ids[-1])
    # a database whose scrub was interrupted after the fourth row
    store.set_meta(S.SCRUB_FLAG, "")
    store.set_meta(S.SCRUB_FLAG + ":upto", str(ids[3]))
    more = [old_row(store, i) for i in range(6, 9)]
    assert store.scrub_stored_credentials(batch=2) == 3
    assert all(SECRET not in store.get(f)["req_headers"] for f in more)
    assert store.meta(S.SCRUB_FLAG) == "done"


def test_scrub_on_an_empty_database_just_sets_the_flag(store):
    assert store.scrub_stored_credentials() == 0 and store.meta(S.SCRUB_FLAG) == "done"


def test_scrub_leaves_unreadable_header_text_alone(store):
    with store.lock:
        store.db.execute("INSERT INTO flows (ts, kind, req_headers) VALUES (?,?,?)", (time.time(), "http", "not json"))
        store.db.commit()
    assert store.scrub_stored_credentials() == 0
    assert store.q("SELECT req_headers FROM flows")[0]["req_headers"] == "not json"


# ---------- retention ----------
def flow(store, age_days, **kw):
    return store.add({"ts": time.time() - age_days * DAY, "kind": "http", "decrypted": 1, "app": "Zen",
                      "host": "example.com", "path": "/x", "req_body": "a", "resp_body": "b", **kw})


def counts(store) -> dict:
    tables = ("triggers", "moments", "event_types", "events", "path_shapes", "settings", "names", "passthrough")
    return {t: store.q(f"SELECT COUNT(*) n FROM {t}")[0]["n"] for t in tables}


def test_raw_flows_older_than_three_days_are_deleted(store, monkeypatch):
    monkeypatch.setenv("AMBIENT_FLOW_DAYS", "3")
    assert S.flow_days() == 3
    old = [flow(store, d) for d in (3.1, 4, 30, 400)]
    new = [flow(store, d) for d in (0, 1, 2.9)]
    assert store.purge_old_flows() == 4
    assert [r["id"] for r in store.q("SELECT id FROM flows ORDER BY id")] == new
    assert all(store.get(i) is None for i in old)
    assert store.purge_old_flows() == 0


def test_retention_is_done_in_batches(store):
    for _ in range(25):
        flow(store, 10)
    keep = flow(store, 0)
    assert store.purge_old_flows(batch=4) == 25
    assert [r["id"] for r in store.q("SELECT id FROM flows")] == [keep]


def test_retention_window_can_be_changed(store, monkeypatch):
    a, b, c = flow(store, 0.5), flow(store, 2), flow(store, 10)
    monkeypatch.setenv("AMBIENT_FLOW_DAYS", "30")
    assert store.purge_old_flows() == 0
    monkeypatch.setenv("AMBIENT_FLOW_DAYS", "0")                # 0 = keep everything
    assert store.purge_old_flows() == 0
    monkeypatch.setenv("AMBIENT_FLOW_DAYS", "not a number")     # falls back to the default, one day
    assert store.purge_old_flows() == 2 and store.get(c) is None and store.get(b) is None
    monkeypatch.setenv("AMBIENT_FLOW_DAYS", "1")
    assert store.purge_old_flows() == 0
    assert [r["id"] for r in store.q("SELECT id FROM flows")] == [a]


def test_retention_only_touches_raw_flows(store, events, moments):
    """Triggers, moments, the event catalog and settings outlive the exchanges they were learned from."""
    r = moments.save_trigger({"name": "Tell me", "instruction": "summarise it", "sigs": ["*"]})
    assert "error" not in r, r
    moments.set_setting("some_setting", "kept")
    for i in range(4):
        send(events, "Zen", "tracker.example.com", "/api/issues/comments",
             {"issue": 40 + i, "body": f"Looks good to me, shipping it today {i}"})
    events.flush()
    with store.lock:
        store.db.execute("INSERT INTO moments (ts, app, kind, key, title, text) VALUES (?,?,?,?,?,?)",
                         (time.time() - 30 * DAY, "Zen", "event", "k1", "An old moment", "kept"))
        store.db.execute("UPDATE events SET ts = ?", (time.time() - 30 * DAY,))
        store.db.execute("INSERT OR IGNORE INTO passthrough VALUES (?,?,?,?)", ("App", "h.example", "pinned", 1.0))
        store.db.commit()
    before = counts(store)
    assert before["triggers"] and before["moments"] and before["event_types"] and before["events"] and before["settings"]
    for _ in range(5):
        flow(store, 9)
    assert store.purge_old_flows() == 5
    assert counts(store) == before
    assert store.q("SELECT COUNT(*) n FROM flows")[0]["n"] == 0


def test_ui_api_behaves_when_a_flow_is_gone(store, events, moments, monkeypatch):
    """Nothing in moments or events points at a flow id; the details API answers 404 for one that was purged."""
    import http.client
    from engine import ui
    monkeypatch.setattr(ui, "setup_status", lambda mode="": {"mode": mode})
    gone, kept = flow(store, 9), flow(store, 0)
    assert store.purge_old_flows() == 1
    srv = ui.serve(store, 0, moments, events=events)
    port = srv.server_address[1]

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        out = r.status, json.loads(r.read())
        c.close()
        return out
    try:
        assert get(f"/api/flow/{gone}") == (404, {"error": "not found"})
        assert get(f"/api/flow/{kept}")[0] == 200
        assert [f["id"] for f in get("/api/flows")[1]] == [kept]
        for path in ("/api/moments", "/api/events", "/api/activity", "/api/summary", "/api/apps", "/api/shield",
                     "/api/triggers", "/api/status"):
            assert get(path)[0] == 200, path
    finally:
        srv.shutdown()
        srv.server_close()
    for table in ("moments", "events", "event_types", "triggers"):
        cols = {r["name"] for r in store.q(f"PRAGMA table_info({table})")}
        assert not {c for c in cols if "flow" in c}, (table, cols)


def test_maintenance_purges_and_scrubs_in_the_background(tmp_path, monkeypatch):
    monkeypatch.delenv("AMBIENT_FLOW_DAYS", raising=False)
    store = Store(str(tmp_path / "ambient.db"))
    stale, fresh = old_row(store, 1, ts=time.time() - 10 * DAY), old_row(store, 2)
    said = []
    store.start_maintenance(log=said.append)
    store.start_maintenance()                                   # a second call does not start a second thread
    try:
        assert wait_for(lambda: store.meta(S.SCRUB_FLAG) == "done")
        assert store.get(stale) is None
        assert SECRET not in json.dumps(store.get(fresh))
        assert wait_for(lambda: said) and "removed 1 raw" in said[0]
    finally:
        store.stop_maintenance()
    assert wait_for(lambda: not store._maint.is_alive())


def test_opening_a_store_starts_no_housekeeping(tmp_path):
    """Tests and replay scripts open recorded databases; only a running engine deletes anything."""
    store = Store(str(tmp_path / "ambient.db"))
    old = flow(store, 100)
    again = Store(str(tmp_path / "ambient.db"))
    assert again._maint is None and again.get(old) is not None


# ---------- file permissions ----------
unix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; Windows relies on the profile folder's ACL")


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@unix
def test_a_new_database_is_private(tmp_path):
    folder = tmp_path / "Ambient"
    folder.mkdir(mode=0o755)
    path = str(folder / "ambient.db")
    old = os.umask(0o022)
    try:
        store = Store(path)
        store.add({"ts": time.time(), "kind": "dns", "host": "example.com"})
    finally:
        os.umask(old)
    assert mode(path) == 0o600 and mode(folder) == 0o700
    for extra in ("-wal", "-shm"):
        assert os.path.exists(path + extra) and mode(path + extra) == 0o600


@unix
def test_an_existing_database_is_tightened_on_open(tmp_path):
    folder = tmp_path / "Ambient"
    folder.mkdir(mode=0o755)
    path = str(folder / "ambient.db")
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (1)")
    db.commit()
    for p in (path, path + "-wal", path + "-shm"):
        os.chmod(p, 0o644)
    os.chmod(folder, 0o755)
    Store(path)
    assert [mode(p) for p in (path, path + "-wal", path + "-shm")] == [0o600] * 3 and mode(folder) == 0o700
    db.close()


@unix
def test_folders_that_are_not_ambients_own_are_left_alone(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    (repo / ".git").mkdir(parents=True)
    os.chmod(repo, 0o755)
    Store(str(repo / "ambient.db"))
    assert mode(repo) == 0o755 and mode(repo / "ambient.db") == 0o600      # a source checkout: file only

    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("HOME", str(home))
    Store(str(home / "ambient.db"))
    assert mode(home) == 0o755 and mode(home / "ambient.db") == 0o600

    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o1777)                                    # like /tmp
    Store(str(shared / "ambient.db"))
    assert mode(shared) == 0o1777 and mode(shared / "ambient.db") == 0o600


def test_private_file_is_a_no_op_on_windows(tmp_path, monkeypatch):
    folder = tmp_path / "w"
    folder.mkdir()
    before = mode(folder)
    monkeypatch.setattr(S.sys, "platform", "win32")
    S.private_file(str(folder / "ambient.db"))
    assert not (folder / "ambient.db").exists() and mode(folder) == before
