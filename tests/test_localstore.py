"""Local-storage fallback, against temp SQLite files built here (never an app's real database)."""
import os
import sqlite3

import pytest

from engine import localstore, osal
from engine.localstore import GenericSQLite, LocalStores, WhatsAppAdapter
from helpers import kind, kinds, stored

WA_SCHEMA = """
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZSESSIONTYPE INTEGER, ZPARTNERNAME VARCHAR, ZCONTACTJID VARCHAR);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZCHATSESSION INTEGER, ZCONTACTNAME VARCHAR, ZFIRSTNAME VARCHAR, ZMEMBERJID VARCHAR);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZISFROMME INTEGER, ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER,
                         ZMESSAGEDATE TIMESTAMP, ZTEXT VARCHAR, ZPUSHNAME VARCHAR, ZSTANZAID VARCHAR, ZMESSAGETYPE INTEGER);
INSERT INTO ZWACHATSESSION VALUES (1, 4, 0, 'Dana Whitfield', '447700900001@s.whatsapp.net');
INSERT INTO ZWACHATSESSION VALUES (2, 4, 1, 'Flat 12', '120363000000000001@g.us');
INSERT INTO ZWAGROUPMEMBER VALUES (1, 5, 2, 'Lee Park', 'Lee', '447700900002@s.whatsapp.net');
INSERT INTO ZWAGROUPMEMBER VALUES (2, 5, 2, NULL, NULL, '447700900003@s.whatsapp.net');
INSERT INTO ZWAMESSAGE VALUES (1, 9, 0, 1, NULL, 752000000, 'an old message from before Ambient looked', 'Dana', 'OLD1', 0);
"""


@pytest.fixture
def wa(tmp_path, monkeypatch):
    """A WhatsApp-shaped ChatStorage.sqlite under a temp HOME, and an adapter pointed at it."""
    monkeypatch.setattr(osal, "HOME", tmp_path)
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    adapter = WhatsAppAdapter()
    assert str(adapter.path).startswith(str(tmp_path)) and not adapter.available()
    adapter.path.parent.mkdir(parents=True)
    db = sqlite3.connect(adapter.path)
    db.executescript(WA_SCHEMA)
    db.commit()
    assert adapter.available()

    def say(text, *, me=0, chat=1, member=None, push=None, sid=None):
        cur = db.execute("INSERT INTO ZWAMESSAGE (Z_ENT, ZISFROMME, ZCHATSESSION, ZGROUPMEMBER, ZMESSAGEDATE, ZTEXT, ZPUSHNAME, ZSTANZAID, ZMESSAGETYPE) "
                         "VALUES (9,?,?,?,752000100,?,?,?,0)", (me, chat, member, text, push, sid))
        db.commit()
        return cur.lastrowid
    adapter.say = say
    yield adapter
    db.close()


def stores(events, adapter=None, watching=lambda app: True):
    ls = LocalStores(events, None, watching, enabled=False)        # enabled=False: no background thread, no real adapters
    if adapter is not None:
        ls.curated = {adapter.app: adapter}
    return ls


# ---------- WhatsApp ----------
def test_whatsapp_first_look_is_only_a_baseline(wa, events):
    ls = stores(events, wa)
    assert ls.poll("WhatsApp") == 0 and wa.cursor == 1
    assert ls.poll("WhatsApp") == 0 and kinds(events) == []


def test_whatsapp_dm_group_and_sent(wa, events):
    ls = stores(events, wa)
    ls.poll("WhatsApp")
    wa.say("are you around this afternoon?", sid="3EB0AAA1")
    wa.say("bins go out tonight, don't forget", chat=2, member=1, sid="3EB0AAA2")
    wa.say("who has the spare key?", chat=2, member=2, push="Moz", sid="3EB0AAA3")
    wa.say("on my way now", me=1, sid="3EB0AAA4")
    wa.say("I'll bring the key down", me=1, chat=2, sid="3EB0AAA5")
    wa.say(None, sid="3EB0AAA6")                                    # a photo: no text
    wa.say("   ", sid="3EB0AAA7")
    assert ls.poll("WhatsApp") == 5
    got = [(r["sig"], r["direction"], r["who"], r["place"], r["text"]) for r in stored(events)]
    assert got == [
        ("whatsapp:dm", "in", "Dana Whitfield", "DM with Dana Whitfield", "are you around this afternoon?"),
        ("whatsapp:group", "in", "Lee Park", "Flat 12", "bins go out tonight, don't forget"),
        ("whatsapp:group", "in", "Moz", "Flat 12", "who has the spare key?"),
        ("whatsapp:sent", "out", "you", "DM with Dana Whitfield", "on my way now"),
        ("whatsapp:sent", "out", "you", "Flat 12", "I'll bring the key down")]
    t = kind(events, "whatsapp:group")
    assert (t["app"], t["source"], t["klass"], t["curated"], t["label"]) == ("WhatsApp", "local", "incoming", 1, "someone writes in a group chat")
    assert kind(events, "whatsapp:sent")["klass"] == "action"
    assert ls.poll("WhatsApp") == 0 and len(stored(events)) == 5     # nothing new, nothing repeated
    assert ls.stats["WhatsApp"]["polls"] == 3 and ls.stats["WhatsApp"]["events"] == 5 and ls.stats["WhatsApp"]["curated"] is True
    assert ls.status()[0]["kind"] == "curated"


def test_whatsapp_catches_up_past_one_batch(wa, events):
    ls = stores(events, wa)
    ls.poll("WhatsApp")
    for i in range(60):
        wa.say(f"message number {i} in a long burst", sid=f"B{i}")
    assert ls.poll("WhatsApp") == 50 and ls.poll("WhatsApp") == 10 and ls.poll("WhatsApp") == 0
    texts = [r["text"] for r in stored(events)]
    assert texts[0] == "message number 0 in a long burst" and texts[-1] == "message number 59 in a long burst" and len(texts) == 60


def test_whatsapp_messages_fire_chat_triggers(wa, events, moments):
    assert moments.save_trigger({"name": "Any DM", "text": "someone DMs me", "sigs": ["*:dm"], "action": "draft"}).get("ok")
    ls = stores(events, wa, moments.watching)
    ls.poll("WhatsApp")
    wa.say("are you around this afternoon?", sid="3EB0AAA1")
    wa.say("bins go out tonight", chat=2, member=1, sid="3EB0AAA2")
    ls.poll("WhatsApp")
    ms = moments.list()
    assert [(m["app"], m["kind"], m["who"], m["text"]) for m in ms] == [("WhatsApp", "dm", "Dana Whitfield", "are you around this afternoon?")]
    moments.set_watch("WhatsApp", False)
    wa.say("this one arrives while paused", sid="3EB0AAA3")
    assert ls.poll("WhatsApp") == 0 and len(stored(events)) == 2


def test_whatsapp_database_is_opened_read_only_and_errors_are_contained(wa, events, capsys):
    ls = stores(events, wa)
    ls.poll("WhatsApp")
    with pytest.raises(sqlite3.OperationalError):
        localstore._ro(wa.path).execute("DELETE FROM ZWAMESSAGE")
    wa.path.unlink()
    assert ls.poll("WhatsApp") == 0 and "ambient localstore: WhatsApp" in capsys.readouterr().out
    assert not wa.available()


def test_whatsapp_adapter_is_mac_only(wa, monkeypatch):
    monkeypatch.setattr(osal, "SYSTEM", "linux")
    assert not wa.available()


# ---------- any app's SQLite ----------
@pytest.fixture
def notes_app(tmp_path, monkeypatch):
    root = tmp_path / "Application Support" / "Jotter"
    (root / "Default").mkdir(parents=True)
    monkeypatch.setattr(osal, "app_data_roots", lambda app, exe_path="": [root] if app == "Jotter" else [])
    path = root / "notes.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE note (id INTEGER PRIMARY KEY, uuid TEXT, title TEXT, body TEXT, updated REAL, blob BLOB);
        CREATE TABLE settings (k TEXT, v TEXT);
        CREATE TABLE cache_entries (k TEXT, v TEXT);
        INSERT INTO note (uuid, title, body) VALUES ('550e8400-e29b-41d4-a716-446655440000', 'Old', 'a note from before Ambient looked');
        INSERT INTO settings VALUES ('theme', 'dark mode with a long description');""")
    db.commit()
    tick = [0]

    def write(sql, *args):
        db.execute(sql, args)
        db.commit()
        tick[0] += 5                                               # make sure the mtime moves, whatever the filesystem's resolution
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + tick[0]))
    yield root, path, write
    db.close()


def collect():
    got = []
    return got, lambda app, table, label, text, fields: got.append((app, table, label, text, fields))


def test_generic_sqlite_emits_only_rows_added_after_the_baseline(notes_app):
    root, path, write = notes_app
    g = GenericSQLite("Jotter", "/Applications/Jotter.app/Contents/MacOS/Jotter")
    got, emit = collect()
    assert g.poll(emit) == 0 and got == []                          # first look: remember where each table is
    assert g.files == [path]
    assert g.poll(emit) == 0                                        # unchanged file: not even opened
    write("INSERT INTO note (uuid, title, body) VALUES (?,?,?)", "7c9e6679-7425-40de-944b-e07fc1f90ae7", "Groceries", "Buy oat milk and coffee beans")
    write("INSERT INTO settings VALUES ('last_note', 'a settings row that reads like a sentence')")
    write("INSERT INTO cache_entries VALUES ('k', 'a cache row that reads like a sentence')")
    assert g.poll(emit) == 1
    app, table, label, text, fields = got[0]
    assert (app, table, label, text) == ("Jotter", "notes.note", "note", "Buy oat milk and coffee beans")
    assert fields.get("title") == "Groceries" and "7c9e6679" not in str(fields)
    assert g.poll(emit) == 0 and len(got) == 1
    write("UPDATE note SET body = 'edited in place, no new row' WHERE id = 1")
    assert g.poll(emit) == 0                                        # edits to old rows are not new events


def test_generic_sqlite_through_localstores(notes_app, events, monkeypatch):
    root, path, write = notes_app
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    ls = stores(events)
    ls.enabled = True                                               # allow poke(); still no thread and no curated adapters
    ls.poke("Zen", "/Applications/Zen.app/Contents/MacOS/zen")      # never a browser's own storage
    ls.poke("1Password", "/Applications/1Password.app/Contents/MacOS/1Password")
    ls.poke("mDNSResponder", "/usr/sbin/mDNSResponder")             # not something a person installed
    ls.poke("Jotter", "")
    assert ls.generic == {} and ls.pending == {}
    ls.poke("Jotter", "/Applications/Jotter.app/Contents/MacOS/Jotter")
    assert list(ls.generic) == ["Jotter"] and "Jotter" in ls.pending
    assert ls.poll("Jotter") == 0
    write("INSERT INTO note (uuid, title, body) VALUES (?,?,?)", "u2", "Groceries", "Buy oat milk and coffee beans")
    assert ls.poll("Jotter") == 1
    t = kind(events)
    assert (t["sig"], t["app"], t["label"], t["klass"], t["source"], t["direction"]) == \
        ("Jotter|LOCAL notes.note", "Jotter", "new note", "incoming", "local", "in")
    assert stored(events)[0]["text"] == "Buy oat milk and coffee beans"
    assert events.catalog()[0]["sources"] == ["local"]
    assert ls.status() == [{"app": "Jotter", "kind": "generic", "files": ["notes.sqlite"], **ls.stats["Jotter"]}]
    off = stores(events)
    off.poke("Jotter", "/Applications/Jotter.app/Contents/MacOS/Jotter")     # disabled (test / proxy mode): never looks
    assert off.generic == {}


def test_generic_sqlite_finds_nested_databases_and_survives_bad_files(notes_app):
    root, path, write = notes_app
    nested = root / "Default" / "data.db"
    db = sqlite3.connect(nested)
    db.executescript("CREATE TABLE message (id INTEGER PRIMARY KEY, body TEXT); INSERT INTO message (body) VALUES ('old one here');")
    db.commit()
    (root / "broken.db").write_bytes(b"this is not a sqlite database at all")
    (root / "empty.sqlite").write_bytes(b"")
    g = GenericSQLite("Jotter")
    got, emit = collect()
    assert g.poll(emit) == 0
    assert set(g.files) == {path, nested, root / "broken.db"}       # empty files are skipped at discovery
    db.execute("INSERT INTO message (body) VALUES ('a new message in the nested database')")
    db.commit()
    st = nested.stat()
    os.utime(nested, (st.st_atime, st.st_mtime + 9))
    assert g.poll(emit) == 1 and got[0][1] == "data.message" and got[0][3] == "a new message in the nested database"
    db.close()


def test_generic_sqlite_does_not_lose_rows_in_a_burst(notes_app):
    root, path, write = notes_app
    g = GenericSQLite("Jotter")
    got, emit = collect()
    g.poll(emit)
    for i in range(25):
        write("INSERT INTO note (uuid, title, body) VALUES (?,?,?)", f"u{i}", "Note", f"note number {i} written in a burst")
    g.poll(emit)
    write("UPDATE note SET title = 'touch' WHERE id = 1")
    g.poll(emit)
    assert len(got) == 25


def test_generic_kind_becomes_visible_once_rows_have_text(notes_app, events):
    root, path, write = notes_app
    ls = stores(events)
    ls.generic["Jotter"] = GenericSQLite("Jotter")
    ls.poll("Jotter")
    write("INSERT INTO note (uuid, title) VALUES ('u1', 'x')")
    ls.poll("Jotter")
    write("INSERT INTO note (uuid, title, body) VALUES ('u2', 'Groceries', 'Buy oat milk and coffee beans')")
    ls.poll("Jotter")
    assert kind(events)["klass"] == "incoming" and events.catalog() != []


# ---------- system-synced apps: fixed locations, read with the same generic reader ----------
@pytest.mark.parametrize("table, want", [("ZREMCDREMINDER", "reminder"), ("ZWAMESSAGE", "message"), ("CalendarItem", "calendar item"),
                                         ("messages", "messages"), ("chat_message_join", "chat message join")])
def test_table_label(table, want):
    assert localstore.table_label(table) == want


def test_system_source_is_read_from_its_fixed_location_after_a_push(tmp_path, monkeypatch, events):
    monkeypatch.setattr(osal, "HOME", tmp_path)                     # a pretend home: the pattern resolves inside it
    monkeypatch.setattr(osal, "SYSTEM", "darwin")
    path = tmp_path / "Library" / "Messages" / "chat.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, is_from_me INTEGER);"
                     "INSERT INTO message (guid, text, is_from_me) VALUES ('g0', 'from before Ambient looked', 0);")
    db.commit()
    ls = stores(events)
    ls.enabled = True
    ls.system = {"Messages": GenericSQLite("Messages", patterns=["Library/Messages/chat.db", "Library/Messages/nothing-*.db"])}
    ls.poke("apsd")                                                 # the push daemon talked: look at system-synced apps
    assert list(ls.pending) == ["Messages"] and ls.generic == {}
    assert ls.poll("Messages") == 0 and ls.system["Messages"].files == [path]
    db.execute("INSERT INTO message (guid, text, is_from_me) VALUES ('g1', 'running ten minutes late, sorry', 0)")
    db.commit()
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 7))
    assert ls.poll("Messages") == 1
    t = kind(events)
    assert (t["sig"], t["label"], t["klass"], t["source"]) == ("Messages|LOCAL chat.message", "new message", "incoming", "local")
    assert stored(events)[0]["text"] == "running ten minutes late, sorry"
    assert ls.status()[0]["kind"] == "system"
    db.close()


def test_a_burst_is_finished_on_the_next_looks_without_new_writes(notes_app, events):
    root, path, write = notes_app
    ls = stores(events)
    g = ls.generic["Jotter"] = GenericSQLite("Jotter")
    ls.poll("Jotter")
    for i in range(45):
        write("INSERT INTO note (uuid, title, body) VALUES (?,?,?)", f"u{i}", "Note", f"note number {i} written in a burst")
    assert ls.poll("Jotter") == 20 and g.behind == {str(path)} and "Jotter" in ls.pending      # asked to come back for the rest
    assert ls.poll("Jotter") == 20 and ls.poll("Jotter") == 5 and g.behind == set()
    assert ls.poll("Jotter") == 0
    assert [r["text"] for r in stored(events)] == [f"note number {i} written in a burst" for i in range(45)]


def test_generic_kind_stays_visible_after_a_row_without_text(notes_app, events):
    root, path, write = notes_app
    ls = stores(events)
    ls.generic["Jotter"] = GenericSQLite("Jotter")
    ls.poll("Jotter")
    write("INSERT INTO note (uuid, title, body) VALUES ('u1', 'Groceries', 'Buy oat milk and coffee beans')")
    ls.poll("Jotter")
    write("INSERT INTO note (uuid, title) VALUES ('u2', 'x')")
    ls.poll("Jotter")
    assert kind(events)["klass"] == "incoming" and kind(events)["n"] == 2


def test_a_local_row_is_not_described_as_received(notes_app, events, moments, fake_prob):
    root, path, write = notes_app
    assert moments.save_trigger({"name": "Shopping", "text": "a note about shopping", "sigs": ["Jotter|LOCAL *"], "action": "draft",
                                 "semantic": True}).get("ok")
    ls = stores(events)
    ls.generic["Jotter"] = GenericSQLite("Jotter")
    ls.poll("Jotter")
    write("INSERT INTO note (uuid, title, body) VALUES ('u1', 'Groceries', 'Buy oat milk and coffee beans')")     # the user may have written it
    ls.poll("Jotter")
    assert fake_prob.calls[0][0] == 'new note in Jotter: "Buy oat milk and coffee beans"' and len(moments.list()) == 1
