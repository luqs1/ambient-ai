"""Local-storage fallback: for apps whose traffic can't be read (end-to-end encrypted or
certificate-pinned), use their network activity as the signal that something happened, then
read what happened from the app's own database on this machine.

Two layers, same idea as the network side:
  generic   any SQLite database an app keeps: remember the highest row id per table, and when
            the app talks to the network, turn new rows with human text into events. No
            knowledge of the app needed.
  curated   adapters that know one app's schema and give richer events (WhatsApp: who, which
            chat, sent or received).
Databases are opened read-only. Nothing is copied except the text of new rows, into the same
local events table everything else uses.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

from engine import osal
from engine.events import BROWSERS, harvest, words

APPLE_EPOCH = 978307200
SKIP_APPS = BROWSERS | {"1Password", "Bitwarden", "Keychain Access", "Ambient", "unknown", "Claude", "claude"}
SKIP_TABLES = re.compile(r"^(sqlite_|z_|Z_METADATA|Z_PRIMARYKEY|Z_MODELCACHE|ACHANGE|ATRANSACTION|meta$|schema_|"
                         r"migrations?$|cache|kv$|keyvalue|blob|thumb|props|settings|prefs|ZWAZ)", re.I)
MAX_DB_BYTES = 4 * 1024 ** 3
BATCH = 20                                  # rows read per table per look
# Apps whose syncing is done by system daemons (push, iCloud), so the app itself never shows up on
# the network. Their stores are checked whenever those daemons talk, and on a slow timer.
# Only the location is listed here: rows are read generically, with no per-app parsing.
SYSTEM_SOURCES = {
    "darwin": {
        "Calendar": ["Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"],
        "Notes": ["Library/Group Containers/group.com.apple.notes/NoteStore.sqlite"],
        "Reminders": ["Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/*.sqlite"],
        "Messages": ["Library/Messages/chat.db"],
        "Mail": ["Library/Mail/V*/MailData/Envelope Index"],
    },
    "windows": {},
    "linux": {},
}
PUSH_DAEMONS = {"apsd", "cloudd", "imagent", "identityservicesd", "remindd", "calaccessd", "dataaccessd",
                "accountsd", "bird", "nsurlsessiond", "CalendarAgent", "Mail"}


def store_name(f: Path) -> str:
    """A stable prefix for a store file. Per-account files named with an id ('Data-F55E…') all hold
    the same kind of thing, so they share one kind instead of one per account."""
    stem = f.stem
    return "" if re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}|[0-9a-f]{16,}|\d{6,}", stem) else stem + "."


def table_label(tb: str) -> str:
    """'ZREMCDREMINDER' / 'CalendarItem' / 'messages' -> a readable noun."""
    t = tb
    if t.isupper() and t.startswith("Z"):                # Core Data: ZREMCDREMINDER -> reminder
        t = re.sub(r"^Z(REMCD|IC|WA|CK|NS|AB)?", "", t).lower()
    return words(t) or tb.lower()


def _ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.4)
    db.row_factory = sqlite3.Row
    return db


class WhatsAppAdapter:
    """WhatsApp for Mac keeps messages in ChatStorage.sqlite (Core Data)."""
    app = "WhatsApp"

    def __init__(self):
        self.path = (osal.HOME / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite")
        self.cursor: int | None = None

    def available(self) -> bool:
        return osal.SYSTEM == "darwin" and self.path.exists()

    def poll(self, emit) -> int:
        db = _ro(self.path)
        try:
            top = db.execute("SELECT MAX(Z_PK) FROM ZWAMESSAGE").fetchone()[0] or 0
            if self.cursor is None:
                self.cursor = top                    # first look: remember where we are, emit nothing
                return 0
            if top <= self.cursor:
                return 0
            rows = db.execute(
                "SELECT m.Z_PK id, m.ZISFROMME me, m.ZTEXT text, m.ZPUSHNAME push, m.ZSTANZAID sid, "
                "       s.ZPARTNERNAME chat, s.ZSESSIONTYPE stype, g.ZCONTACTNAME member, g.ZFIRSTNAME first "
                "FROM ZWAMESSAGE m LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION "
                "LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER "
                "WHERE m.Z_PK > ? ORDER BY m.Z_PK LIMIT 50", (self.cursor,)).fetchall()
        finally:
            db.close()
        n = 0
        for r in rows:
            self.cursor = max(self.cursor, r["id"])
            text = (r["text"] or "").strip()
            if not text:
                continue
            group = r["stype"] == 1
            chat = r["chat"] or "a chat"
            if r["me"]:
                kind, who, place = "sent", "you", (chat if group else f"DM with {chat}")
            elif group:
                kind, who, place = "group", (r["member"] or r["first"] or r["push"] or "someone"), chat
            else:
                kind, who, place = "dm", chat, f"DM with {chat}"
            emit(app="WhatsApp", kind=kind, who=who, place=place, text=text, key=f"whatsapp:{r['sid'] or r['id']}",
                 context={"channel": chat})
            n += 1
        return n


class GenericSQLite:
    """Any app: watch its SQLite files for new rows that carry human text."""

    def __init__(self, app: str, exe_path: str = "", patterns: list[str] | None = None):
        self.app = app
        self.exe_path = exe_path
        self.patterns = patterns            # fixed locations (system-synced apps); None = discover
        self.files: list[Path] = []
        self.scanned = 0.0
        self.cursors: dict[tuple[str, str], int] = {}
        self.mtimes: dict[str, float] = {}
        self.behind: set[str] = set()       # files with more new rows than one scan reads: look again next poll

    def _discover(self):
        self.scanned = time.time()
        found: list[Path] = []
        if self.patterns is not None:
            for pat in self.patterns:
                found += [f for f in sorted(osal.HOME.glob(pat)) if f.is_file()][:6]
            self.files = found
            return
        for root in osal.app_data_roots(self.app, self.exe_path):
            try:
                for pat in ("*.sqlite", "*.sqlite3", "*.db", "*.sqlitedb", "*/*.sqlite", "*/*.db", "*/*/*.sqlite",
                            "*/*/*.db", "*/*/*/*.sqlite", "*/*/*/*.db"):
                    for f in root.glob(pat):
                        if f.is_file() and 0 < f.stat().st_size < MAX_DB_BYTES and f not in found:
                            found.append(f)
                        if len(found) >= 12:
                            break
            except Exception:
                continue
        self.files = found

    def poll(self, emit_generic) -> int:
        if time.time() - self.scanned > 600:
            self._discover()
        n = 0
        for f in self.files:
            try:
                wal = Path(str(f) + "-wal")
                m = max(f.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)
            except Exception:
                continue
            if self.mtimes.get(str(f)) == m and str(f) not in self.behind:
                continue
            first = str(f) not in self.mtimes
            self.mtimes[str(f)] = m
            try:
                n += self._scan(f, emit_generic, first)
            except Exception:
                continue
        return n

    def _scan(self, f: Path, emit_generic, first: bool) -> int:
        db = _ro(f)
        n = 0
        more = False
        try:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")][:80]
            for tb in tables:
                if SKIP_TABLES.search(tb) or not re.fullmatch(r"[\w ]+", tb):
                    continue
                key = (str(f), tb)
                try:
                    top = db.execute(f'SELECT MAX(rowid) FROM "{tb}"').fetchone()[0] or 0
                except Exception:
                    continue
                prev = self.cursors.get(key)
                if prev is None or first or top <= prev:
                    self.cursors[key] = top           # first look, nothing new, or rows were deleted
                    continue
                # The cursor only moves past rows that were read: a burst bigger than one batch is
                # picked up over the next polls instead of being skipped.
                rows = db.execute(f'SELECT rowid, * FROM "{tb}" WHERE rowid > ? ORDER BY rowid LIMIT {BATCH}',
                                  (prev,)).fetchall()
                more = more or len(rows) == BATCH
                for row in rows:
                    self.cursors[key] = row[0]
                    obj = {k: row[k] for k in row.keys()[1:] if isinstance(row[k], (str, bytes)) and row[k]}
                    text, fields, _ = harvest(obj, limit=400)
                    if not text and not fields:
                        continue                      # bookkeeping row: nothing a person would recognise
                    emit_generic(self.app, f"{store_name(f)}{tb}", table_label(tb), text, fields)
                    n += 1
        finally:
            db.close()
        (self.behind.add if more else self.behind.discard)(str(f))
        return n


class LocalStores:
    def __init__(self, events, recognizers=None, watching=lambda app: True, enabled: bool = True):
        self.events, self.rec, self.watching = events, recognizers, watching
        self.enabled = enabled
        self.generic: dict[str, GenericSQLite] = {}
        self.pending: dict[str, float] = {}
        self.lock = threading.Lock()
        self.stats: dict[str, dict] = {}
        self.curated = {}
        self.system: dict[str, GenericSQLite] = {}
        if not enabled:            # test/proxy modes never touch other apps' data (and never trigger OS prompts)
            return
        self.curated = {a.app: a for a in (WhatsAppAdapter(),) if a.available()}
        self.system = {app: GenericSQLite(app, patterns=pats)
                       for app, pats in SYSTEM_SOURCES.get(osal.SYSTEM, {}).items()}
        self.generic: dict[str, GenericSQLite] = {}
        self.pending: dict[str, float] = {}
        self.lock = threading.Lock()
        self.stats: dict[str, dict] = {}
        threading.Thread(target=self._loop, daemon=True).start()

    def poke(self, app: str, exe_path: str = ""):
        """The app just talked to the network and we couldn't read it: look at its local data soon."""
        if not self.enabled or not app:
            return
        if app in PUSH_DAEMONS:                         # a push or sync just happened: check system-synced apps
            with self.lock:
                for a in self.system:
                    self.pending.setdefault(a, time.time() + 1.5)
            return
        if app in SKIP_APPS:
            return
        with self.lock:
            if app not in self.curated and app not in self.generic:
                if not exe_path or (osal.SYSTEM == "darwin" and ".app/" not in exe_path + "/") or (
                        osal.SYSTEM == "windows" and not osal.is_user_app(exe_path)):
                    return                              # only apps a person installed, not system daemons
                self.generic[app] = GenericSQLite(app, exe_path)
            self.pending.setdefault(app, time.time() + 0.8)     # let the app finish writing

    def _emit_chat(self, *, app, kind, who, place, text, key, context):
        from engine.recognizers import CHAT_LABELS
        self.events.emit(app=app, sig=f"{app.lower()}:{kind}", label=CHAT_LABELS[kind],
                         direction="out" if kind == "sent" else "in",
                         klass="action" if kind == "sent" else "incoming", who=who, place=place, text=text,
                         key=key, context=context, source="local")

    def _emit_generic(self, app, table, label, text, fields):
        # A table is worth showing once any of its rows has carried text, and stays so: the class follows
        # the table, not the row. The reader can't tell who wrote a row, so the label says only "new …".
        sig = f"{app}|LOCAL {table}"
        shown = bool(text) or bool((self.events.type(sig) or {}).get("n_text"))
        self.events.emit(app=app, sig=sig, label=f"new {label}", direction="in",
                         klass="incoming" if shown else "signal", text=text, fields=fields, source="local")

    def poll(self, app: str) -> int:
        if not self.watching(app):
            return 0
        n = 0
        try:
            if app in self.curated:
                n = self.curated[app].poll(self._emit_chat)
            elif app in self.system:
                n = self.system[app].poll(self._emit_generic)
            elif app in self.generic:
                n = self.generic[app].poll(self._emit_generic)
            g = self.system.get(app) or self.generic.get(app)
            if g is not None and g.behind:              # more rows than one look reads: come back for the rest
                with self.lock:
                    self.pending.setdefault(app, time.time() + 0.5)
        except Exception as e:
            print(f"ambient localstore: {app}: {e!r}", flush=True)
        st = self.stats.setdefault(app, {"app": app, "polls": 0, "events": 0, "last": 0, "curated": app in self.curated})
        st["polls"] += 1; st["events"] += n; st["last"] = time.time()
        return n

    def _loop(self):
        last_sweep = 0.0
        while True:
            time.sleep(0.4)
            now = time.time()
            with self.lock:
                due = [a for a, t in self.pending.items() if t <= now]
                for a in due:
                    self.pending.pop(a, None)
            for a in due:
                self.poll(a)
            if now - last_sweep > 20:                   # backstop: push notifications can arrive via system daemons
                last_sweep = now
                for a in list(self.curated) + list(self.system):
                    self.poll(a)

    def status(self) -> list[dict]:
        out = []
        for app, a in self.curated.items():
            out.append({"app": app, "kind": "curated", "path": str(a.path), **self.stats.get(app, {})})
        for app, g in {**self.system, **self.generic}.items():
            out.append({"app": app, "kind": "system" if app in self.system else "generic",
                        "files": [f.name for f in g.files], **self.stats.get(app, {})})
        return out
