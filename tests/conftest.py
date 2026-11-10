"""Shared fixtures for the event-layer and trigger-engine tests.

Everything here is synthetic: temp databases, hand-written request/frame shapes. Nothing reads
the captured databases in data/ or any app's real storage, and no agent CLI is ever started
(AMBIENT_AGENT_CMD is forced to `cat`).

    HF_HUB_OFFLINE=1 AMBIENT_AGENT_CMD=cat uv run pytest -q
"""
import os
import sys

os.environ["AMBIENT_AGENT_CMD"] = "cat"          # never a real agent, whatever the caller's shell says
os.environ.setdefault("HF_HUB_OFFLINE", "1")     # models come from the local cache or not at all
os.environ.setdefault("AMBIENT_KEEP_BODIES", "1")   # older storage tests read bodies; test_storage_size.py turns it off
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from engine.events import Events  # noqa: E402
from engine.moments import Moments  # noqa: E402
from engine.store import Store  # noqa: E402


class SyncQueue:
    """Stands in for Moments.work so matching events are handled on the calling thread."""

    def __init__(self, handle):
        self.handle = handle
        self.seen = []

    def put_nowait(self, ev):
        self.seen.append(ev)
        self.handle(ev)


@pytest.fixture(scope="session", autouse=True)
def _local_models_loaded_once():
    """Load the judge and the embedder up front (about a second, from the local cache). Moments starts a
    background load of its own whenever a checked trigger exists; doing it here first keeps that from
    racing a test or the interpreter's exit. Without the models both return None and the tests that
    need them are skipped."""
    from engine import decide
    decide.judge()
    decide.embedder()


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "ambient.db"))


@pytest.fixture
def events(store):
    return Events(store)


@pytest.fixture
def moments(store, events):
    """Moments with the starter triggers removed and events handled synchronously."""
    m = Moments(store, events)
    for t in m.triggers():
        m.delete_trigger(t["id"])
    m.work = SyncQueue(m._handle)
    return m


@pytest.fixture
def fake_prob(monkeypatch):
    """Replace the local judge. `fake_prob.value` is what every check returns; `.calls` records them."""
    from engine import decide

    class Fake:
        value = 0.9
        calls: list = []

        def __call__(self, context, statement):
            self.calls.append((context, statement))
            return self.value(context, statement) if callable(self.value) else self.value

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(decide, "prob", fake)
    return fake
