"""Curated recognizers (Slack, Discord, Granola) fed with synthetic traffic in each app's wire shape."""
import json

import zstandard

from engine.recognizers import blocks_text, form_fields
from engine.wsdecode import SocketDecoder
from helpers import etf, kind, kinds, multipart, stored

ME, PRIYA = "U0ME00001", "U024BE7LH"
SLACK_WS = "wss-primary.slack.com"
BOOT = {"ok": True, "self": {"id": ME, "name": "sam"},
        "users": [{"id": PRIYA, "real_name": "Priya Shah", "profile": {"display_name": "Priya"}},
                  {"id": ME, "real_name": "Sam Example", "profile": {"display_name": ""}}],
        "channels": [{"id": "C024BE91L", "name": "design"}],
        "ims": [{"id": "D024BE91L", "is_im": True, "user": PRIYA}]}


def boot(moments):
    moments.rec.on_http("Slack", "acme.slack.com", "/api/client.userBoot", "token=x", json.dumps(BOOT))


def slack_push(moments, **msg):
    moments.rec.on_ws("Slack", SLACK_WS, "sock1", json.dumps({"type": "message", "ts": "1731200000.000100", **msg}), None, False)


def bind(moments, *sigs):
    assert moments.save_trigger({"name": "t", "text": "x", "sigs": list(sigs), "action": "draft"}).get("ok")


# ---------- Slack ----------
def test_slack_dm_with_names_learned_from_responses(moments, events):
    boot(moments)
    slack_push(moments, channel="D024BE91L", user=PRIYA, text="can you send me the <https://example.com/deck|deck> &amp; notes?")
    ev = stored(events)[0]
    assert (ev["sig"], ev["who"], ev["place"]) == ("slack:dm", "Priya", "DM with Priya")
    assert ev["text"] == "can you send me the deck & notes?"
    t = kind(events)
    assert t["curated"] == 1 and t["klass"] == "incoming" and t["label"] == "someone DMs me"


def test_slack_mention_in_a_channel(moments, events):
    boot(moments)
    slack_push(moments, channel="C024BE91L", user=PRIYA, text=f"<@{ME}> can you look at <#C024BE91L|design> today?")
    ev = stored(events)[0]
    assert (ev["sig"], ev["who"], ev["place"]) == ("slack:mention", "Priya", "#design")
    assert ev["text"] == "@Sam Example can you look at #design today?"


def test_slack_ignores_what_is_not_for_me(moments, events):
    boot(moments)
    slack_push(moments, channel="C024BE91L", user=PRIYA, text="general chatter in a channel")
    slack_push(moments, channel="D024BE91L", user=ME, text="my own message echoed back")
    slack_push(moments, channel="D024BE91L", user=PRIYA, bot_id="B01", text="a bot wrote this one")
    slack_push(moments, channel="D024BE91L", user=PRIYA, subtype="message_changed", text="an edit, not a new message")
    moments.rec.on_ws("Slack", SLACK_WS, "sock1", json.dumps({"type": "user_typing", "channel": "D024BE91L", "user": PRIYA}), None, False)
    moments.rec.on_ws("Slack", SLACK_WS, "sock1", json.dumps({"type": "message", "channel": "D024BE91L", "user": PRIYA, "text": "upstream"}), None, True)
    moments.rec.on_ws("Slack", SLACK_WS, "sock1", "{not json", None, False)                 # must not raise
    assert kinds(events) == []


def test_slack_same_message_twice_is_one_event(moments, events):
    boot(moments)
    for _ in range(2):
        slack_push(moments, channel="D024BE91L", user=PRIYA, text="can you send me the deck?")
    assert len(stored(events)) == 1


def test_slack_send_from_desktop_blocks_marks_the_dm_replied(moments, events):
    boot(moments)
    bind(moments, "*:dm", "*:sent")
    slack_push(moments, channel="D024BE91L", user=PRIYA, text="can you send me the deck?")
    assert [m["status"] for m in moments.list()] == ["new"]
    blocks = json.dumps([{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "text", "text": "sure, "}, {"type": "user", "user_id": PRIYA}, {"type": "text", "text": " here it is "},
        {"type": "link", "url": "https://example.com/deck"}, {"type": "emoji", "name": "tada"}]}]}])
    body, _ = multipart({"token": "xoxc-" + "1" * 12, "channel": "D024BE91L", "blocks": blocks, "client_msg_id": "c1"})
    moments.rec.on_http("Slack", "acme.slack.com", "/api/chat.postMessage?_x_id=abc", body, '{"ok":true}')
    sent = stored(events, "slack:sent")[0]
    assert sent["text"] == "sure, @Priya here it is https://example.com/deck:tada:" and sent["who"] == "you" and sent["place"] == "DM with Priya"
    by_kind = {m["kind"]: m for m in moments.list()}
    assert by_kind["dm"]["status"] == "replied" and by_kind["sent"]["status"] == "done"


def test_slack_send_plain_form_and_paused(moments, events):
    moments.rec.on_http("Slack", "acme.slack.com", "/api/chat.postMessage", "channel=C024BE91L&text=see+you+at+the+standup", '{"ok":true}')
    assert stored(events, "slack:sent")[0]["text"] == "see you at the standup"
    moments.set_watch("Slack", False)
    moments.rec.on_http("Slack", "acme.slack.com", "/api/chat.postMessage", "channel=C024BE91L&text=this+one+is+not+kept", '{"ok":true}')
    slack_push(moments, channel="D024BE91L", user=PRIYA, text="neither is this one")
    assert len(stored(events)) == 1


def test_form_fields_and_blocks_text():
    assert form_fields("a=1&text=hello+there") == {"a": "1", "text": "hello there"}
    assert form_fields('{"channel": "C1", "text": "hi"}') == {"channel": "C1", "text": "hi"}
    assert form_fields("") == {} and form_fields("{broken") == {}
    body, _ = multipart({"channel": "C1", "text": "two\r\nlines"})
    assert form_fields(body) == {"channel": "C1", "text": "two\r\nlines"}
    assert blocks_text("not json") == "" and blocks_text(None) == ""


# ---------- Discord ----------
DME, DPRIYA = 1437000000000000001, 1437000000000000002
READY = {"op": 0, "s": 1, "t": "READY", "d": {
    "user": {"id": DME, "username": "sam"}, "users": [{"id": DPRIYA, "username": "priya", "global_name": "Priya"}],
    "private_channels": [{"id": 1437000000000000010, "recipient_ids": [DPRIYA]}],
    "guilds": [{"id": 1437000000000000020, "properties": {"name": "Acme"}, "channels": [{"id": 1437000000000000021, "name": "general"}]}]}}


def message(mid, channel, author, content, guild=None, mentions=()):
    d = {"id": mid, "channel_id": channel, "content": content, "mentions": [{"id": m} for m in mentions],
         "author": {"id": author, "username": "priya" if author == DPRIYA else "sam", "global_name": "Priya" if author == DPRIYA else "Sam"}}
    if guild:
        d["guild_id"] = guild
    return {"op": 0, "s": mid % 1000, "t": "MESSAGE_CREATE", "d": d}


def test_discord_gateway_etf_over_zstd(moments, events):
    c, dec = zstandard.ZstdCompressor().compressobj(), SocketDecoder()
    frames = [READY,
              message(1437000000000000101, 1437000000000000010, DPRIYA, "are you free for a call at three?"),
              message(1437000000000000102, 1437000000000000010, DME, "yes, give me five minutes"),
              message(1437000000000000103, 1437000000000000021, DPRIYA, "hey <@me> can you review this?", guild=1437000000000000020, mentions=[DME]),
              message(1437000000000000104, 1437000000000000021, DPRIYA, "unrelated server chatter", guild=1437000000000000020),
              message(1437000000000000105, 1437000000000000010, DPRIYA, "")]
    for f in frames:
        raw = c.compress(etf(f)) + c.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
        moments.rec.on_ws("Discord", "gateway-us-east1-b.discord.gg", "s1", None, dec.feed(raw), False)
    got = [(r["sig"], r["who"], r["place"], r["text"]) for r in stored(events)]
    assert got == [("discord:dm", "Priya", "DM with Priya", "are you free for a call at three?"),
                   ("discord:sent", "you", "DM with Priya", "yes, give me five minutes"),
                   ("discord:mention", "Priya", "#general · Acme", "hey <@me> can you review this?")]
    assert moments.setting("discord_me") == str(DME)


def test_discord_json_text_frames_and_bots(moments, events):
    host = "gateway.discord.gg"
    moments.rec.on_ws("Discord", host, "s1", json.dumps(READY), None, False)
    bot = message(1437000000000000201, 1437000000000000010, DPRIYA, "automated message from a bot")
    bot["d"]["author"]["bot"] = True
    moments.rec.on_ws("Discord", host, "s1", json.dumps(bot), None, False)
    moments.rec.on_ws("Discord", host, "s1", json.dumps(message(1437000000000000202, 1437000000000000010, DPRIYA, "a real one")), None, False)
    moments.rec.on_ws("Discord", host, "s1", json.dumps({"op": 11}), None, False)
    assert [r["text"] for r in stored(events)] == ["a real one"]


# ---------- Granola ----------
def turn(text, final=True):
    return json.dumps({"type": "Turn", "turn_order": 1, "end_of_turn": final, "turn_is_formatted": final, "transcript": text})


def test_granola_meeting_ends_with_a_transcript(moments, events):
    host = "streaming.us.assemblyai.com"
    for t in ("Thanks for joining everyone.", "Sam will send the proposal by Friday.", "Great, talk next week."):
        moments.rec.on_ws("Granola", host, "sockA", turn(t[:10], final=False), None, False)       # partials are ignored
        moments.rec.on_ws("Granola", host, "sockA", turn(t), None, False)
    moments.rec.on_ws("Granola", host, "sockB", turn("Too short to be a meeting."), None, False)
    assert kinds(events) == []                                      # nothing until the socket closes
    moments.rec.on_ws_end(host, "sockA")
    moments.rec.on_ws_end(host, "sockB")
    moments.rec.on_ws_end(host, "sockA")                            # closing twice is harmless
    assert len(stored(events)) == 1
    ev = events.recent(sig="granola:meeting")[0]
    assert ev["text"] == "Great, talk next week." and ev["place"] == "Granola"
    assert kind(events)["klass"] == "incoming" and kind(events)["label"] == "a meeting ends"


def test_granola_meeting_reaches_the_agent_with_its_transcript(moments, events):
    bind(moments, "granola:meeting")
    host = "streaming.us.assemblyai.com"
    lines = ["Thanks for joining everyone.", "Sam will send the proposal by Friday.", "Great, talk next week."]
    for t in lines:
        moments.rec.on_ws("Granola", host, "sockA", turn(t), None, False)
    moments.rec.on_ws_end(host, "sockA")
    m = moments.list()[0]
    assert m["kind"] == "meeting" and m["context"]["transcript"] == "\n".join(lines) and m["context"]["turns"] == 3
    prompt = moments.prompt_for(m, "draft")
    assert "Sam will send the proposal by Friday." in prompt and "follow-up" in prompt
    assert moments.notes_after(0)["notes"][0]["title"] == "Meeting ended · Granola"
