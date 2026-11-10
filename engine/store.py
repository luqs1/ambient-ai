"""SQLite store. One row per observed exchange (HTTP flow, WS message, opaque
connection, DNS answer). The UI and any agent read the same file."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL, duration REAL,
  kind TEXT,            -- http | ws | opaque | tls-rejected | tcp | udp | dns | error
  decrypted INTEGER,    -- 1 if we saw plaintext
  app TEXT, pid INTEGER, process_path TEXT,
  client TEXT, server TEXT, host TEXT,
  method TEXT, url TEXT, path TEXT, status INTEGER,
  req_bytes INTEGER, resp_bytes INTEGER,
  req_ctype TEXT, resp_ctype TEXT,
  req_headers TEXT, resp_headers TEXT,
  req_body TEXT, resp_body TEXT,
  note TEXT,
  llm TEXT, mcp TEXT,
  findings TEXT, max_sev TEXT,
  shield TEXT
);
CREATE INDEX IF NOT EXISTS flows_app ON flows(app);
CREATE INDEX IF NOT EXISTS flows_host ON flows(host);
CREATE INDEX IF NOT EXISTS flows_ts ON flows(ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS passthrough (
  app TEXT, host TEXT, reason TEXT, ts REAL, PRIMARY KEY (app, host)  -- host '*' = whole app
);
"""

SEV_RANK = {None: 0, "info": 1, "low": 2, "medium": 3, "high": 4}
LIST_COLS = ("id, ts, duration, kind, decrypted, app, pid, host, method, path, status, "
             "req_bytes, resp_bytes, resp_ctype, note, llm, mcp, max_sev")

# ---------- what never reaches the disk ----------
REMOVED = "[removed]"
NOT_STORED = "[not stored: sign-in or payment request]"
# Raw exchanges are for "details", not an archive: older ones are deleted.
FLOW_DAYS = 1.0
PURGE_EVERY = 3600
MAX_FLOWS = 50_000           # raw rows kept at most, whatever their age
BODIES_REMOVED = "[body not kept]"
SCRUB_FLAG = "credentials_scrubbed_v1"

CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "cookie2", "set-cookie2",
                      "x-api-key", "api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token"}
CREDENTIAL_HEADER_PARTS = ("token", "secret", "session", "auth", "cookie", "api-key", "apikey", "api_key",
                           "password", "credential", "csrf", "xsrf", "signature")
CREDENTIAL_PARAMS = {"key", "sig", "auth", "authorization", "jwt", "pwd", "pass", "passwd", "sid", "apikey"}
CREDENTIAL_PARAM_PARTS = ("token", "secret", "password", "passwd", "signature", "session", "apikey", "credential",
                          "oauth", "auth_", "auth-", "_auth", "-auth")      # not "author", "authuser"
_PARAM = re.compile(r"([^&;=#?]+)=([^&;#]*)")


def credential_header(name: str) -> bool:
    n = str(name).strip().lower()
    return n in CREDENTIAL_HEADERS or any(p in n for p in CREDENTIAL_HEADER_PARTS)


def credential_param(name: str) -> bool:
    n = str(name).strip().lower().replace("%5f", "_").replace("%2d", "-")
    return (n in CREDENTIAL_PARAMS or n.endswith(("_key", "-key", ".key"))
            or any(p in n for p in CREDENTIAL_PARAM_PARTS))


def mask_headers(headers):
    """A copy of [[name, value], ...] with the values of credential-bearing headers replaced. The names stay,
    so the details page still shows the header was there. Never changes its argument."""
    if not headers:
        return headers
    if isinstance(headers, dict):
        return {k: REMOVED if credential_header(k) and v != "" else v for k, v in headers.items()}
    return [[k, REMOVED if credential_header(k) and v != "" else v] for k, v in headers]


def mask_url(url):
    """The same URL (or path) with credential values in its query string or fragment replaced."""
    if not url or not isinstance(url, str) or "=" not in url:
        return url
    cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=-1)
    if cut < 0:
        return url
    return url[:cut] + _PARAM.sub(
        lambda m: f"{m.group(1)}={REMOVED}" if m.group(2) and credential_param(m.group(1)) else m.group(0), url[cut:])


def _mask_headers_json(text):
    """mask_headers for the JSON text a stored row holds. Returns the same object when nothing changes."""
    if not text or not isinstance(text, str):
        return text
    try:
        old = json.loads(text)
        new = mask_headers(old)
    except Exception:
        return text
    return text if new == old else json.dumps(new)


def keep_bodies() -> bool:
    """Raw request and reply bodies are only needed by the Details view (hidden for now) and Veil's history.
    Triggers never read them: the event layer extracts what it needs as traffic passes. Off unless
    AMBIENT_KEEP_BODIES=1."""
    return os.environ.get("AMBIENT_KEEP_BODIES", "").strip().lower() in ("1", "true", "yes")


def flow_days() -> float:
    """How long raw exchanges are kept. AMBIENT_FLOW_DAYS overrides; 0 or less keeps them forever."""
    try:
        return float(os.environ.get("AMBIENT_FLOW_DAYS") or FLOW_DAYS)
    except ValueError:
        return FLOW_DAYS


def private_file(path: str):
    """The database holds what your apps said: only you may read it. Creates the file 0600 (SQLite gives its
    -wal and -shm the same mode) and tightens a file and folder that already exist. The folder becomes 0700
    unless it plainly isn't Ambient's own (not ours, /tmp-like, the home folder, a source checkout).
    Nothing to do on Windows, where the profile folder's ACL already does this."""
    if sys.platform == "win32" or not path or path == ":memory:" or path.startswith("file:"):
        return
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    try:
        st = os.stat(folder)
        shared = (st.st_mode & 0o1000 or os.path.realpath(folder) == os.path.realpath(os.path.expanduser("~"))
                  or os.path.exists(os.path.join(folder, ".git")))
        if st.st_uid == os.getuid() and not shared and st.st_mode & 0o077:
            os.chmod(folder, 0o700)
    except OSError:
        pass
    try:
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    except OSError:
        pass
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            if os.stat(p).st_mode & 0o177:
                os.chmod(p, 0o600)
        except OSError:
            pass


class Store:
    def __init__(self, path: str):
        private_file(path)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        if self.db.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")   # takes effect after one VACUUM (see compact)
        self.db.execute("PRAGMA secure_delete=ON")     # deleted and rewritten rows are zeroed, not left in free pages
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(flows)")}
        if "shield" not in cols:
            self.db.execute("ALTER TABLE flows ADD COLUMN shield TEXT")
        private_file(path)                              # the -wal and -shm exist now
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._maint: threading.Thread | None = None

    @staticmethod
    def _clean(row: dict) -> dict:
        """Credentials never go to disk: header values and URL parameters that carry them are replaced."""
        for k in ("req_headers", "resp_headers"):
            v = row.get(k)
            if isinstance(v, str):
                row[k] = _mask_headers_json(v)
            elif v:
                row[k] = mask_headers(v)
        for k in ("url", "path"):
            if row.get(k):
                row[k] = mask_url(row[k])
        return row

    def add(self, row: dict) -> int:
        row = self._clean(dict(row))
        if not keep_bodies():
            for k in ("req_body", "resp_body"):
                if row.get(k) not in (None, ""):
                    row[k] = BODIES_REMOVED
        for k in ("req_headers", "resp_headers", "llm", "mcp", "findings", "shield"):
            if k in row and row[k] is not None and not isinstance(row[k], str):
                row[k] = json.dumps(row[k])
        cols = ", ".join(row)
        qs = ", ".join("?" * len(row))
        with self.lock:
            cur = self.db.execute(f"INSERT INTO flows ({cols}) VALUES ({qs})", tuple(row.values()))
            self.db.commit()
            return cur.lastrowid

    def update(self, fid: int, **kw):
        kw = self._clean(kw)
        sets = ", ".join(f"{k}=?" for k in kw)
        with self.lock:
            self.db.execute(f"UPDATE flows SET {sets} WHERE id=?", (*kw.values(), fid))
            self.db.commit()

    def update_table(self, table: str, rid: int, **kw):
        sets = ", ".join(f"{k}=?" for k in kw)
        with self.lock:
            self.db.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*kw.values(), rid))
            self.db.commit()

    def shield_stats(self, since: float = 0):
        rows = self.q("""SELECT app, shield, ts FROM flows WHERE shield IS NOT NULL AND ts >= ?""", (since,))
        by_app: dict[str, dict] = {}
        secrets: set[str] = set()
        for r in rows:
            sh = json.loads(r["shield"])
            a = by_app.setdefault(r["app"], {"app": r["app"], "requests": 0, "redactions": 0, "restored": 0, "kinds": {}})
            a["requests"] += 1
            a["restored"] += sh.get("restored", 0)
            for h in sh.get("redacted") or []:
                a["redactions"] += 1
                a["kinds"][h["kind"]] = a["kinds"].get(h["kind"], 0) + 1
                secrets.add(h["placeholder"])
        return {"secrets": len(secrets), "apps": sorted(by_app.values(), key=lambda a: -a["redactions"])}

    # --- housekeeping: old raw exchanges go, and rows stored before credentials were masked are rewritten ---
    def meta(self, k: str) -> str | None:
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0]["v"] if r else None

    def set_meta(self, k: str, v: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            self.db.commit()

    def purge_old_flows(self, days: float | None = None, now: float | None = None, batch: int = 500) -> int:
        """Delete raw exchanges older than `days` (default: flow_days()). Only the flows table is touched:
        triggers, moments, the event catalog and settings stay. In batches, so recording never waits long."""
        days = flow_days() if days is None else days
        if days <= 0:
            return 0
        cutoff = (now or time.time()) - days * 86400
        total = 0
        while not self._stop.is_set():
            with self.lock:
                ids = [r[0] for r in self.db.execute("SELECT id FROM flows WHERE ts < ? LIMIT ?", (cutoff, batch))]
                if ids:
                    self.db.execute(f"DELETE FROM flows WHERE id IN ({','.join('?' * len(ids))})", ids)
                    self.db.commit()
            total += len(ids)
            if len(ids) < batch:
                break
            time.sleep(0.005)
        return total

    def trim_flows(self, keep: int = MAX_FLOWS, batch: int = 2000) -> int:
        """Beyond `keep` raw rows, the oldest go, however recent."""
        total = 0
        while not self._stop.is_set():
            with self.lock:
                top = self.db.execute("SELECT MAX(id) FROM flows").fetchone()[0] or 0
                ids = [r[0] for r in self.db.execute("SELECT id FROM flows WHERE id <= ? LIMIT ?", (top - keep, batch))]
                if ids:
                    self.db.execute(f"DELETE FROM flows WHERE id IN ({','.join('?' * len(ids))})", ids)
                    self.db.commit()
            total += len(ids)
            if len(ids) < batch:
                break
            time.sleep(0.005)
        return total

    def drop_old_bodies(self, batch: int = 1000) -> int:
        """Once per database, when bodies aren't kept: blank the ones stored by earlier versions."""
        flag = "bodies_dropped_v1"
        if keep_bodies() or self.meta(flag) == "done":
            return 0
        last, changed = 0, 0
        while not self._stop.is_set():
            with self.lock:
                rows = self.db.execute("SELECT id FROM flows WHERE id > ? AND (length(req_body) > 20 OR length(resp_body) > 20) "
                                       "ORDER BY id LIMIT ?", (last, batch)).fetchall()
                if rows:
                    ids = [r[0] for r in rows]
                    self.db.execute(f"UPDATE flows SET req_body=CASE WHEN req_body IS NULL OR req_body='' THEN req_body "
                                    f"ELSE ? END, resp_body=CASE WHEN resp_body IS NULL OR resp_body='' THEN resp_body ELSE ? END "
                                    f"WHERE id IN ({','.join('?' * len(ids))})", (BODIES_REMOVED, BODIES_REMOVED, *ids))
                    self.db.commit()
                    last, changed = ids[-1], changed + len(ids)
            if len(rows) < batch:
                break
            time.sleep(0.005)
        if not self._stop.is_set():
            with self.lock:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (flag, "done"))
                self.db.commit()
        return changed

    def compact(self) -> None:
        """Give freed space back to the disk. The first time (a database made before incremental vacuum was
        on) this is one full VACUUM; after that, a cheap incremental step."""
        with self.lock:
            mode = self.db.execute("PRAGMA auto_vacuum").fetchone()[0]
            free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
            if mode != 2:
                self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                self.db.execute("VACUUM")
            elif free > 256:
                self.db.execute("PRAGMA incremental_vacuum").fetchall()     # runs one page per row fetched
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass

    def scrub_stored_credentials(self, batch: int = 500) -> int:
        """Once per database: apply today's masking to rows stored by older versions (credential headers,
        credentials in URLs, the reply to a sign-in or payment request). Resumable and in small batches;
        returns how many rows changed."""
        if self.meta(SCRUB_FLAG) == "done":
            return 0
        last = int(self.meta(SCRUB_FLAG + ":upto") or 0)
        changed = 0
        while not self._stop.is_set():
            with self.lock:
                rows = self.db.execute("SELECT id, url, path, req_headers, resp_headers FROM flows "
                                       "WHERE id > ? ORDER BY id LIMIT ?", (last, batch)).fetchall()
                if rows:
                    upto = rows[-1]["id"]
                    # the reply to a sign-in or payment request used to be kept
                    touched = {r[0] for r in self.db.execute(
                        "SELECT id FROM flows WHERE id > ? AND id <= ? AND req_body = ? "
                        "AND resp_body IS NOT NULL AND resp_body NOT IN ('', ?)", (last, upto, NOT_STORED, NOT_STORED))}
                    for r in rows:
                        new = {"url": mask_url(r["url"]), "path": mask_url(r["path"]),
                               "req_headers": _mask_headers_json(r["req_headers"]),
                               "resp_headers": _mask_headers_json(r["resp_headers"])}
                        new = {k: v for k, v in new.items() if v != r[k]}
                        if r["id"] in touched:
                            new["resp_body"] = NOT_STORED
                        if new:
                            self.db.execute(f"UPDATE flows SET {', '.join(f'{k}=?' for k in new)} WHERE id=?",
                                            (*new.values(), r["id"]))
                            changed += 1
                    last = upto
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (SCRUB_FLAG + ":upto", str(last)))
                    self.db.commit()
            if len(rows) < batch:
                break
            time.sleep(0.005)
        else:
            return changed          # stopped half way: carries on from `upto` next start
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (SCRUB_FLAG, "done"))
            self.db.commit()
            if changed:
                try:        # the old values are still in the write-ahead log until it is folded back and emptied
                    self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
        return changed

    def scrub_sensitive_bodies(self, is_sensitive, batch: int = 500) -> int:
        """Once per database: blank what was sent to, and came back from, sign-in and payment endpoints in
        rows stored before such bodies were kept off disk (tokens from OAuth token endpoints, for one).
        `is_sensitive(host, path)` is the same test new traffic goes through."""
        flag = "sensitive_bodies_scrubbed_v1"
        if self.meta(flag) == "done":
            return 0
        last, changed = 0, 0
        while not self._stop.is_set():
            with self.lock:
                rows = self.db.execute("SELECT id, host, path, req_body, resp_body FROM flows WHERE id > ? "
                                       "ORDER BY id LIMIT ?", (last, batch)).fetchall()
                for r in rows:
                    if not is_sensitive(r["host"] or "", r["path"] or ""):
                        continue
                    new = {k: NOT_STORED for k in ("req_body", "resp_body") if r[k] not in (None, "", b"", NOT_STORED)}
                    if new:
                        self.db.execute(f"UPDATE flows SET {', '.join(f'{k}=?' for k in new)} WHERE id=?",
                                        (*new.values(), r["id"]))
                        changed += 1
                if rows:
                    last = rows[-1]["id"]
                    self.db.commit()
            if len(rows) < batch:
                break
            time.sleep(0.005)
        else:
            return changed
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (flag, "done"))
            self.db.commit()
            if changed:
                try:
                    self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
        return changed

    def start_maintenance(self, log=None, sensitive=None):
        """Background housekeeping for a running engine: purge old raw exchanges now and about hourly, and do
        the one-time credential scrub. Never started by tests or scripts that only open a Store."""
        if self._maint and self._maint.is_alive():
            return

        def say(msg: str):
            try:
                if log:
                    log(msg)
            except Exception:
                pass

        def run():
            first = True
            while not self._stop.is_set():
                try:            # housekeeping must never take the engine down
                    gone = self.purge_old_flows() + self.trim_flows()
                    fixed = self.scrub_stored_credentials() if first else 0
                    if first:
                        fixed += self.drop_old_bodies()
                    if first and sensitive:
                        fixed += self.scrub_sensitive_bodies(sensitive)
                    if gone or fixed:
                        say(f"ambient: removed {gone} raw exchanges (older than {flow_days():g} days or beyond "
                            f"{MAX_FLOWS}), cleaned {fixed} stored earlier")
                    if gone or fixed or first:
                        self.compact()
                except Exception as e:
                    say(f"ambient: housekeeping failed: {e!r}")
                first = False
                self._stop.wait(PURGE_EVERY)

        self._maint = threading.Thread(target=run, daemon=True, name="ambient-store-maintenance")
        self._maint.start()

    def stop_maintenance(self):
        self._stop.set()

    # --- learned passthrough: (app, host) pairs whose app rejected our CA ---
    def passthrough_pairs(self) -> list[tuple[str, str]]:
        with self.lock:
            return [(r[0], r[1]) for r in self.db.execute("SELECT app, host FROM passthrough")]

    def add_passthrough(self, app: str, host: str, reason: str, ts: float):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO passthrough VALUES (?,?,?,?)", (app, host, reason, ts))
            self.db.commit()

    # --- reads for the UI ---
    def q(self, sql: str, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def list_flows(self, after=0, app=None, only=None, search=None, limit=500):
        where, args = ["id > ?"], [after]
        if app:
            where.append("app = ?"); args.append(app)
        if only == "findings":
            where.append("max_sev IN ('low','medium','high')")
        elif only == "llm":
            where.append("(llm IS NOT NULL OR mcp IS NOT NULL)")
        elif only == "opaque":
            where.append("decrypted = 0")
        if search:
            where.append("(host LIKE ? OR path LIKE ? OR req_body LIKE ? OR resp_body LIKE ?)")
            args += [f"%{search}%"] * 4
        sql = (f"SELECT {LIST_COLS} FROM flows WHERE {' AND '.join(where)} "
               f"ORDER BY id DESC LIMIT ?")
        return self.q(sql, (*args, limit))

    def get(self, fid: int):
        r = self.q("SELECT * FROM flows WHERE id=?", (fid,))
        return r[0] if r else None

    def apps(self):
        return self.q("""
          SELECT app, COUNT(*) n, COUNT(DISTINCT host) hosts,
                 SUM(COALESCE(req_bytes,0)) up, SUM(COALESCE(resp_bytes,0)) down,
                 SUM(decrypted = 0) opaque,
                 SUM(max_sev IN ('medium','high')) findings,
                 SUM(llm IS NOT NULL) llm, MAX(ts) last
          FROM flows WHERE kind != 'dns' GROUP BY app ORDER BY last DESC""")

    def summary(self):
        tot = self.q("""SELECT COUNT(*) n, SUM(decrypted=1) decrypted, SUM(decrypted=0) opaque,
                        SUM(max_sev='high') high, SUM(max_sev='medium') medium,
                        SUM(COALESCE(req_bytes,0)) up, SUM(COALESCE(resp_bytes,0)) down
                        FROM flows WHERE kind != 'dns'""")[0]
        llm = self.q("""SELECT app, json_extract(llm,'$.provider') provider,
                        json_extract(llm,'$.model') model, COUNT(*) calls,
                        SUM(json_extract(llm,'$.input_tokens')) input_tokens,
                        SUM(json_extract(llm,'$.output_tokens')) output_tokens
                        FROM flows WHERE llm IS NOT NULL GROUP BY 1,2,3 ORDER BY calls DESC""")
        return {"totals": tot, "llm": llm, "passthrough": self.q("SELECT * FROM passthrough")}
