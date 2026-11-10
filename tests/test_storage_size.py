"""Ambient must not fill the disk: raw bodies aren't kept by default, raw rows are capped, and freed
space goes back to the disk."""
import os
import time

import pytest

from engine import store as S
from engine.store import Store


@pytest.fixture
def lean(tmp_path, monkeypatch):
    monkeypatch.delenv("AMBIENT_KEEP_BODIES", raising=False)
    monkeypatch.delenv("AMBIENT_FLOW_DAYS", raising=False)
    return Store(str(tmp_path / "a.db"))


def row(ts=None, body="x" * 5000):
    return {"ts": ts or time.time(), "kind": "http", "decrypted": 1, "host": "api.example.com", "method": "POST",
            "path": "/v1/notes", "url": "https://api.example.com/v1/notes", "req_body": body, "resp_body": body}


def test_bodies_are_not_kept_by_default(lean):
    fid = lean.add(row())
    r = lean.q("SELECT req_body, resp_body, host FROM flows WHERE id=?", (fid,))[0]
    assert r["req_body"] == r["resp_body"] == S.BODIES_REMOVED and r["host"] == "api.example.com"


def test_bodies_can_be_kept_on_request(lean, monkeypatch):
    monkeypatch.setenv("AMBIENT_KEEP_BODIES", "1")
    fid = lean.add(row(body="hello"))
    assert lean.q("SELECT req_body FROM flows WHERE id=?", (fid,))[0]["req_body"] == "hello"


def test_default_retention_is_one_day(lean):
    assert S.flow_days() == 1


def test_raw_rows_are_capped_however_recent(lean):
    ids = [lean.add(row(body="")) for _ in range(30)]
    assert lean.trim_flows(keep=10, batch=7) == 20
    assert [r["id"] for r in lean.q("SELECT id FROM flows ORDER BY id")] == ids[-10:]


def test_bodies_stored_by_older_versions_are_dropped_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIENT_KEEP_BODIES", "1")
    st = Store(str(tmp_path / "old.db"))
    for _ in range(5):
        st.add(row())
    monkeypatch.delenv("AMBIENT_KEEP_BODIES")
    assert st.drop_old_bodies(batch=2) == 5
    assert {r["b"] for r in st.q("SELECT req_body b FROM flows")} == {S.BODIES_REMOVED}
    assert st.drop_old_bodies() == 0


def test_freed_space_goes_back_to_the_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIENT_KEEP_BODIES", "1")
    path = str(tmp_path / "big.db")
    st = Store(path)
    for _ in range(400):
        st.add(row(body="y" * 20000))
    st.compact()
    big = os.path.getsize(path)
    monkeypatch.delenv("AMBIENT_KEEP_BODIES")
    st.drop_old_bodies()
    st.compact()
    assert os.path.getsize(path) < big / 5
    assert st.db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
