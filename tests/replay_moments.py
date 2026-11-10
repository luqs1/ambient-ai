"""Replay recorded Slack/Granola flows from an Ambient DB through the Moments recognizers
into a copy, so recognizers can be tested without running capture.

    uv run python tests/replay_moments.py data/ambient.db /tmp/replay.db
"""
import shutil
import sqlite3
import sys

sys.path.insert(0, ".")
from engine.moments import Moments  # noqa: E402
from engine.store import Store  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
shutil.copy(src, dst)
st = Store(dst)
with st.lock:
    st.db.executescript("DROP TABLE IF EXISTS moments; DROP TABLE IF EXISTS names; DROP TABLE IF EXISTS settings;")
m = Moments(st)
db = sqlite3.connect(src)
last_sock = {}
for fid, app, kind, host, path, method, req, resp in db.execute(
        "select id, app, kind, host, path, method, req_body, resp_body from flows "
        "where app in ('Slack','Granola') and kind in ('http','ws') order by id"):
    if kind == "http":
        m.on_http(app, host, path, req, resp)
    elif host == "streaming.us.assemblyai.com":
        sock = path  # one socket per URL in practice
        if '"type":"Begin"' in (resp or "") and sock in last_sock:
            m.on_ws_end(host, sock)
        last_sock[sock] = 1
        m.on_ws(app, host, sock, resp or req or "", method == "WS↑")
    else:
        m.on_ws(app, host, path, resp or req or "", method == "WS↑")
for sock in list(m.meetings):
    m.on_ws_end("streaming.us.assemblyai.com", sock)
for r in m.list(limit=30):
    print(r["id"], r["kind"], r["who"], "|", r["place"], "|", (r["text"] or "")[:80].replace("\n", " "))
