"""Replay recorded flows through the universal event layer and print the catalog.
    uv run python tests/replay_events.py <source.db> [<out.db>] [--quiet]
"""
import json, os, sqlite3, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.store import Store
from engine.events import Events

src = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else os.path.join(tempfile.mkdtemp(), "ev.db")
ev = Events(Store(out))
db = sqlite3.connect(f"file:{src}?mode=ro", uri=True); db.row_factory = sqlite3.Row
t0 = time.time(); n = 0
for r in db.execute("SELECT * FROM flows WHERE decrypted=1 AND kind IN ('http','ws') ORDER BY id"):
    n += 1
    if r["kind"] == "http":
        hdr = {k.lower(): v for k, v in json.loads(r["req_headers"] or "[]")}
        ev.http(r["app"], r["host"], r["method"] or "GET", r["path"] or "/", r["req_ctype"] or "", r["req_body"],
                hdr, r["resp_body"] or "", r["status"])
    else:
        up = (r["method"] or "").endswith("↑")
        ev.ws(r["app"], r["host"], r["path"] or "", r["req_body"] if up else r["resp_body"], up)
ev.flush()
print(f"{n} flows in {time.time()-t0:.1f}s -> {len(ev.types)} kinds; stored events:",
      ev.store.q("select count(*) n from events")[0]["n"])
from collections import Counter
print("by class:", dict(Counter(t["klass"] for t in ev.types.values())))
for a in ev.catalog(include_quiet="--quiet" in sys.argv):
    print(f"\n== {a['app']}" + (f" (via {', '.join(a['via'])})" if a['via'] else "") + f"  [{a['quiet']} quiet kinds hidden]")
    for k in a["kinds"][:14]:
        arrow = "←" if k["direction"] == "in" else "→"
        print(f"   {arrow} {k['klass'][:6]:6} {k['n']:5}  {k['label'][:42]:42} | {(k['example'] or '')[:70]!r}")
