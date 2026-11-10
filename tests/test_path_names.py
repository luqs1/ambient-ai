"""Learned name positions in paths (PathShapes): one kind for "star a repo" whichever repo it is,
without folding different operations into each other."""
import pytest

from engine.events import Events
from helpers import FORM, kind, kinds, send, stored

REPOS = [("luqs1", "ambient-ai"), ("tauri-apps", "tauri"), ("astral-sh", "uv"), ("pallets", "flask")]


def star(events, owner, repo):
    send(events, "gh", "api.github.com", f"/user/starred/{owner}/{repo}", method="PUT")


def test_names_in_a_path_are_learned_after_a_few_different_ones(events):
    for o, r in REPOS[:2]:
        star(events, o, r)
    assert len(kinds(events)) == 2                                  # two repos could still be two operations
    for o, r in REPOS[2:]:
        star(events, o, r)
    t = kind(events)
    assert t["sig"] == "GitHub CLI|PUT api.github.com/user/starred/{name}/{name}" and t["n"] == 4
    assert t["label"] == "update starred"
    star(events, "encode", "httpx")
    assert kind(events)["n"] == 5
    events.flush()
    assert [r["sig"] for r in events.store.q("SELECT sig FROM event_types")] == [t["sig"]]      # the folded kinds are gone


def test_operation_names_are_never_generalised(events):
    ops = ["chat.postMessage", "reactions.add", "pins.add", "files.upload", "stars.add", "reminders.add", "bookmarks.add"]
    for round_ in range(2):
        for m in ops:
            send(events, "Thing", "acme.example.com", f"/api/{m}", "channel=C024BE91L&text=see+you+at+the+standup", ctype=FORM)
    assert sorted(t["sig"].split("/api/")[1].split("+")[0] for t in kinds(events)) == sorted(ops)
    assert events.shapes.masks.get("Thing|POST acme.example.com|3|", set()) == set()


def test_learned_names_survive_a_restart(events, store):
    for o, r in REPOS:
        star(events, o, r)
    events.flush()
    again = Events(store)
    star(again, "encode", "httpx")                                  # recognised at once, no relearning
    t = kind(again)
    assert t["sig"].endswith("/user/starred/{name}/{name}") and t["n"] == 5


def test_trigger_shown_once_on_one_repo_follows_the_kind_to_every_repo(moments, events):
    """"Show me once" binds to what you did (star luqs1/ambient-ai). What you meant is the kind of thing
    (star a repo): the second, differently named repo already teaches the path and fires the trigger."""
    star(events, *REPOS[0])
    exact = kind(events)["sig"]
    assert exact.endswith("/user/starred/luqs1/ambient-ai")
    assert moments.save_trigger({"name": "Stars", "text": "I star a repo", "sigs": [exact], "action": "draft"}).get("ok")
    star(events, *REPOS[1])
    folded = kind(events)["sig"]
    assert folded.endswith("/user/starred/{name}/{name}")
    assert moments.triggers()[0]["sigs"] == [folded]                 # the trigger moved with the kind
    assert [m["sig"] for m in moments.list()] == [folded]            # and fired for the second repo
    for o, r in REPOS[2:]:
        star(events, o, r)
    assert len(moments.list()) == len(REPOS) - 1


def test_plain_words_at_the_end_of_a_watched_path_are_not_merged_on_two_examples(moments, events):
    """PUT /api/me/status and PUT /api/me/avatar are different things to do, and a second repo with a plain
    name (luqs1/notes) looks just the same: neither is merged until the usual evidence is in."""
    send(events, "gh", "api.example.com", "/api/me/status", {"emoji": "palm_tree"}, method="PUT")
    sig = kind(events)["sig"]
    assert moments.save_trigger({"name": "S", "text": "I set my status", "sigs": [sig], "action": "draft"}).get("ok")
    send(events, "gh", "api.example.com", "/api/me/avatar", {"url": "https://example.com/a.png"}, method="PUT")
    assert len(kinds(events)) == 2 and moments.triggers()[0]["sigs"] == [sig]
    assert moments.list() == []


def test_a_name_spelled_second_example_is_enough_for_a_watched_path(moments, events):
    star(events, "luqs1", "Ambient.AI")
    exact = kind(events)["sig"]
    assert moments.save_trigger({"name": "Stars", "text": "I star a repo", "sigs": [exact], "action": "draft"}).get("ok")
    star(events, "luqs1", "notes_2")                                  # same owner; both spelled like names
    assert kind(events)["sig"].endswith("/user/starred/luqs1/{name}")
    assert len(moments.list()) == 1


def test_the_example_a_trigger_is_bound_to_still_counts_after_a_restart(store):
    from engine.moments import Moments
    from conftest import SyncQueue
    ev = Events(store)
    star(ev, "luqs1", "ambient-ai")
    mo = Moments(store, ev)
    assert mo.save_trigger({"name": "Stars", "text": "I star a repo", "sigs": [kind(ev)["sig"]], "action": "draft"}).get("ok")
    ev.flush()
    ev2 = Events(store)                                               # restart: nothing remembered in memory
    mo2 = Moments(store, ev2)
    mo2.work = SyncQueue(mo2._handle)
    star(ev2, "tauri-apps", "tauri")
    assert [t["sigs"] for t in mo2.triggers() if t["name"] == "Stars"] == [["GitHub CLI|PUT api.github.com/user/starred/{name}/{name}"]]
    assert [m["sig"] for m in mo2.list()] == ["GitHub CLI|PUT api.github.com/user/starred/{name}/{name}"]


def test_without_a_trigger_two_examples_are_not_enough_to_fold(events):
    star(events, *REPOS[0])
    star(events, *REPOS[1])
    assert len(kinds(events)) == 2


def test_a_watched_path_does_not_fold_plain_resource_words(moments, events):
    """Eager learning needs both values to be spelled like names: notes/tasks stay two kinds."""
    send(events, "gh", "api.example.com", "/api/v1/notes/archive", {"all": True}, method="PUT")
    sig = kind(events)["sig"]
    assert moments.save_trigger({"name": "A", "text": "I archive notes", "sigs": [sig], "action": "draft"}).get("ok")
    send(events, "gh", "api.example.com", "/api/v1/tasks/archive", {"all": True}, method="PUT")
    assert len(kinds(events)) == 2 and moments.triggers()[0]["sigs"] == [sig]


def test_learned_positions_do_not_leak_into_other_endpoints(events):
    for o, r in REPOS:
        star(events, o, r)
    send(events, "gh", "api.github.com", "/repos/acme/api/subscription", {"subscribed": True}, method="PUT")      # watch a repo
    send(events, "gh", "api.github.com", "/repos/acme/api/topics", {"names": ["python", "testing"]}, method="PUT")  # set its topics
    sigs = [t["sig"] for t in kinds(events) if "/repos/" in t["sig"]]
    assert len(sigs) == 2 and any("subscription" in s for s in sigs) and any("topics" in s for s in sigs)


@pytest.mark.parametrize("method, paths", [
    ("PATCH", [f"/api/v1/{r}/42" for r in ("notes", "tasks", "issues", "projects", "labels", "teams")]),
    ("PUT", [f"/api/me/{r}" for r in ("avatar", "status", "theme", "locale", "timezone", "bio")]),
    ("POST", [f"/api/{r}/42/comments" for r in ("issues", "pulls", "discussions", "commits", "gists", "reviews")]),
])
def test_a_closed_set_of_operations_is_not_mistaken_for_names(events, method, paths):
    for p in paths:
        send(events, "Thing", "api.example.com", p, {"text": "Renamed this one earlier today"}, method=method)
    assert len(kinds(events)) == len(paths)


def test_events_recorded_before_a_fold_stay_with_their_kind(events):
    for o, r in REPOS:
        star(events, o, r)
    t = kind(events)
    assert t["n"] == 4 and len(stored(events)) == 4
    assert len(events.recent(sig=t["sig"])) == 4


def test_plain_word_names_need_a_dozen_before_they_fold(events):
    orgs = ["acme", "globex", "initech", "umbrella", "hooli", "stark", "wayne", "wonka", "tyrell", "cyberdyne", "soylent", "aperture"]
    for o in orgs[:11]:
        send(events, "Thing", "api.example.com", f"/orgs/{o}/settings", {"theme": "dark"}, method="PUT")
    assert len(kinds(events)) == 11                                 # could still be eleven different resources
    send(events, "Thing", "api.example.com", f"/orgs/{orgs[11]}/settings", {"theme": "dark"}, method="PUT")
    t = kind(events)
    assert t["sig"] == "Thing|PUT api.example.com/orgs/{name}/settings" and t["n"] == 12 and len(stored(events)) == 12


def test_names_that_no_api_would_use_fold_after_three(events):
    for w in ("Acme-Corp", "luqs1", "my.team"):
        send(events, "Thing", "api.example.com", f"/workspaces/{w}/invite", {"email": "sam@example.com"}, method="PUT")
    assert kind(events)["sig"] == "Thing|PUT api.example.com/workspaces/{name}/invite"
    send(events, "Thing", "api.example.com", "/workspaces/Globex/archive", {"reason": "no longer used"}, method="PUT")     # another operation
    assert sorted(t["sig"].rsplit("/", 1)[1] for t in kinds(events)) == ["archive", "invite"]


def test_positions_learned_by_an_older_build_become_templates(events, store):
    for o, r in REPOS:
        star(events, o, r)
    events.flush()
    grp = "GitHub CLI|PUT api.github.com|5|"
    with store.lock:                                                # the old format: bare positions for a whole depth
        store.db.execute("UPDATE path_shapes SET mask='[3, 4]' WHERE grp=?", (grp,))
        store.db.execute("INSERT INTO path_shapes VALUES ('Gone|PUT api.gone.example|4|', '[2]')")
        store.db.commit()
    again = Events(store)
    assert again.shapes.masks == {grp: [("", "user", "starred", "{name}", "{name}")]}
    assert store.q("SELECT grp, mask FROM path_shapes") == [{"grp": grp, "mask": '[["", "user", "starred", "{name}", "{name}"]]'}]
    star(again, "encode", "httpx")
    send(again, "gh", "api.github.com", "/repos/acme/api/subscription", {"subscribed": True}, method="PUT")
    assert sorted(t["sig"].split("api.github.com")[1] for t in kinds(again)) == ["/repos/acme/api/subscription", "/user/starred/{name}/{name}"]
