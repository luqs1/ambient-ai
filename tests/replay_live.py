"""End-to-end check without capture: replay recorded flows through the same objects the addon wires
together (events + recognizers + moments + triggers), with a stub agent.
    AMBIENT_AGENT_CMD=cat uv run python tests/replay_live.py <source.db> <out.db>
"""
import json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.store import Store
from engine.events import Events
from engine.moments import Moments
from engine.wsdecode import SocketDecoder

src, out = sys.argv[1], sys.argv[2]
st = Store(out); ev = Events(st); mo = Moments(st, ev)
db = sqlite3.connect(f"file:{src}?mode=ro", uri=True); db.row_factory = sqlite3.Row
n = 0
for r in db.execute("SELECT * FROM flows WHERE decrypted=1 AND kind IN ('http','ws') ORDER BY id"):
    n += 1
    if r["kind"] == "http":
        hdr = {k.lower(): v for k, v in json.loads(r["req_headers"] or "[]")}
        mo.rec.on_http(r["app"], r["host"], r["path"] or "/", r["req_body"] or "", r["resp_body"] or "")
        ev.http(r["app"], r["host"], r["method"] or "GET", r["path"] or "/", r["req_ctype"] or "", r["req_body"],
                hdr, r["resp_body"] or "", r["status"])
    else:
        up = (r["method"] or "").endswith("↑")
        text = r["req_body"] if up else r["resp_body"]
        mo.rec.on_ws(r["app"], r["host"], "s", text, None, up)
        ev.ws(r["app"], r["host"], r["path"] or "", text, up)
ev.flush(); time.sleep(3)
print(f"{n} flows -> {len(ev.types)} kinds, {st.q('select count(*) n from events')[0]['n']} events, "
      f"{st.q('select count(*) n from moments')[0]['n']} moments")
for m in mo.list(limit=12):
    print("  moment", m["id"], m["app"], m["kind"], "|", (m["who"] or ""), "|", m["text"][:50], "|", m["status"], m["action"], m["action_status"], m["gate"])
print("triggers:", [(t["name"], t["sigs"], t["checked"], t["matched"], t["fired"]) for t in mo.triggers()])
