"""Print Slack message traffic as ambient records it (sent via API, received via WebSocket)."""
import json
import sqlite3
import sys
import time
from urllib.parse import parse_qs

db = sqlite3.connect(sys.argv[1] if len(sys.argv) > 1 else "data/ambient.db")
last = db.execute("select coalesce(max(id),0) from flows").fetchone()[0]


def text_from_form(body: str) -> dict:
    # chat.postMessage is multipart/form or urlencoded; pull the obvious fields
    out = {}
    if body.lstrip().startswith("{"):
        try:
            return json.loads(body)
        except Exception:
            return {}
    if "Content-Disposition" in body:  # multipart
        for part in body.split("Content-Disposition: form-data; name=\"")[1:]:
            name, _, rest = part.partition("\"")
            out[name] = rest.strip().split("\r\n--")[0].split("\n--")[0].strip()
        return out
    return {k: v[0] for k, v in parse_qs(body).items()}


while True:
    rows = db.execute("""select id, kind, method, path, req_body, resp_body from flows
                         where id > ? and app = 'Slack' order by id""", (last,)).fetchall()
    for fid, kind, method, path, req, resp in rows:
        last = fid
        if kind == "http" and path and ("chat.postMessage" in path or "chat.update" in path):
            f = text_from_form(req or "")
            print(f"[{fid}] SENT via {path.split('?')[0]} channel={f.get('channel')} text={str(f.get('text') or f.get('blocks'))[:200]!r}", flush=True)
        elif kind == "ws" and resp:
            try:
                ev = json.loads(resp)
            except Exception:
                continue
            if ev.get("type") in ("message", "user_typing", "reaction_added", "im_marked", "channel_marked"):
                print(f"[{fid}] RECEIVED ws type={ev.get('type')} subtype={ev.get('subtype')} channel={ev.get('channel')} "
                      f"user={ev.get('user')} text={str(ev.get('text',''))[:200]!r}", flush=True)
    time.sleep(1)
