"""Text extraction: the human sentence in a body, whatever shape the body has."""
import json
import random
import zlib

import pytest
import zstandard

from engine.events import decode_body, harvest, looks_like_prose, mask_secrets, printable_runs
from engine.wsdecode import SocketDecoder, decode_payload
from helpers import UUID, UUID2, etf, fake_secrets, kind, send, stored

NOISE = {
    "id": UUID, "client_msg_id": UUID2, "created_at": "2026-11-10T08:15:30.123Z", "ts": "1731200000.000100",
    "avatar_url": "https://cdn.example.com/avatars/u/18273645/large.png?v=3",
    "session_token": "c2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtb25seS1ub3QtcmVhbA==",
    "thumbnail": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0",
    "locale": "en-GB", "etag": "W/\"5e1f-9a8b7c6d5e4f3a2b1c0d\"", "checksum": "9fceb02d0ae598e95dc970b74767f19372d61af8",
    "channel": "C024BE91L", "count": 3, "ratio": 0.25, "ok": True, "tags": ["blue_green", "v2.1.0", "a-b-c"],
}


# ---------- harvest ----------
def test_picks_the_sentence_over_ids_urls_tokens_and_timestamps():
    text, fields, op = harvest({**NOISE, "payload": {"meta": dict(NOISE), "value": "Could you send me the signed contract today?"}})
    assert text == "Could you send me the signed contract today?"
    blob = json.dumps(fields)
    for junk in (UUID, "cdn.example.com", "c2Vzc2lvbi10", "iVBORw0KGgo", "Mozilla"):
        assert junk not in blob


def test_opaque_values_are_not_kept_as_fields():
    _, fields, _ = harvest({"text": "This is a thoughtful reply about databases", "hmac": "9fceb02d0ae598e95dc970b74767f19372d61af8",
                            "state": "c2Vzc2lvbi10b2tlbi1mb3ItdGVzdHM", "checksum": "d41d8cd98f00b204e9800998ecf8427e"})
    assert fields == {}


def test_prefers_a_text_key_over_a_longer_incidental_string():
    text, _, _ = harvest({"text": "ship it today please",
                          "debug_note_for_devs": "the renderer picked layout variant B because the viewport was narrow"})
    assert text == "ship it today please"


@pytest.mark.parametrize("key, value", [("text", "hi"), ("message", "Lunch?"), ("body", "thanks!"), ("content", "ok"),
                                        ("comment", "nice"), ("q", "ramen"), ("msg", "yes"), ("reply", "Sure")])
def test_one_word_message_under_a_strong_key(key, value):
    text, _, _ = harvest({"channel": "C024BE91L", key: value, "ts": "1731200000.000100"})
    assert text == value


@pytest.mark.parametrize("value", ["true", "null", UUID, "https://example.com/x", "sam@example.com", "snake_case_token",
                                   "kebab-case-token", "C024BE91L", "12345", "{ viewer { id } }", "query Me { viewer { id } }",
                                   "<br>"])
def test_one_word_machine_values_are_not_messages(value):
    text, _, _ = harvest({"text": value})
    assert text == ""


@pytest.mark.parametrize("value", ["OK", "LGTM", "YES"])
def test_one_word_all_caps_reply(value):
    assert harvest({"text": value})[0] == value


def test_one_word_under_an_ordinary_key_is_not_text():
    text, fields, _ = harvest({"state": "open", "visibility": "public"})
    assert text == "" and fields == {"state": "open", "visibility": "public"}


def test_graphql_document_is_not_the_text():
    text, _, op = harvest({"operationName": "IssueCreate",
                           "query": "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id title } } }",
                           "variables": {"input": {"title": "Export button does nothing on staging"}}})
    assert op == "IssueCreate" and text == "Export button does nothing on staging"
    text, _, op = harvest({"query": "query Viewer { viewer { id name email organization { name } } }", "variables": {}})
    assert text == "" and op == "get Viewer"


def test_json_inside_a_string_is_walked():
    inner = json.dumps({"post": json.dumps({"message": "Could you review my pull request today?", "user_id": UUID})})
    text, _, _ = harvest({"event": "posted", "data": inner})
    assert text == "Could you review my pull request today?"


def test_bytes_values_are_decoded():
    text, _, _ = harvest({"d": {"content": b"see you at the standup tomorrow"}})
    assert text == "see you at the standup tomorrow"


def test_operation_name_keys():
    assert harvest({"type": "message.new", "text": "hello there everyone"})[2] == "message.new"
    assert harvest({"op": 0, "t": "MESSAGE_CREATE", "d": {"content": "hello there everyone"}})[2] == "MESSAGE_CREATE"
    assert harvest({"jsonrpc": "2.0", "method": "tools/call", "params": {}})[2] == "tools/call"
    assert harvest({"type": "12345", "name": UUID + UUID + UUID})[2] is None          # digits / too long: not a name


def test_long_text_is_capped():
    text, _, _ = harvest({"body": "word " * 3000})
    assert 0 < len(text) <= 4000


def test_walk_survives_huge_and_deep_bodies():
    deep = cur = []
    for _ in range(200):
        nxt = []
        cur.append(nxt)
        cur = nxt
    cur.append("this sentence is buried far too deep to matter")
    assert harvest(deep)[0] == ""
    wide = {"rows": [{"id": i, "v": "x" * 5} for i in range(50000)], "text": "a sentence at the very end of the body"}
    harvest(wide)                                                                    # must return, whatever it finds


@pytest.mark.parametrize("key", ["parts", "comments", "arguments", "contents", "posts"])
def test_plural_keys_ending_in_ts_or_os_are_not_junk(key):
    text, _, _ = harvest({"id": UUID, key: ["What is a good name for a bakery in Leeds?"]})
    assert text == "What is a good name for a bakery in Leeds?"


def test_sentence_under_a_junk_looking_key_is_still_text():
    assert harvest({"status": "Shipping the new importer today, feedback welcome", "visibility": "public"})[0]


@pytest.mark.parametrize("key", ["messageText", "commentBody", "comment[body]", "message[content]"])
def test_short_message_under_camelcase_or_bracket_text_key(key):
    assert harvest({key: "ok sounds good", "channelId": "C024BE91L"})[0] == "ok sounds good"


def test_html_wrapped_message():
    text, _, _ = harvest({"content": "<p>Can you join the standup five minutes early?</p>", "messagetype": "RichText/Html"})
    assert "join the standup" in text


def test_two_strings_under_the_same_key_are_not_glued_together():
    ddp = {"msg": "method", "method": "sendMessage", "id": "17", "params": [{"rid": "GENERAL", "msg": "Deploy is done, please check staging"}]}
    assert harvest(ddp)[0] == "Deploy is done, please check staging"
    chat = {"messages": [{"role": "user", "content": "thanks, that worked"},
                         {"role": "user", "content": "Can you also draft the release notes for Friday?"}]}
    assert harvest(chat)[0] == "Can you also draft the release notes for Friday?"


# ---------- looks_like_prose ----------
@pytest.mark.parametrize("s", [
    "hello there", "can you send me the deck?", "I'll do it", "Meeting at 3pm tomorrow?", "ok then",
    "¿Puedes enviarme el archivo?", "Über die Brücke gehen wir morgen", "see https://example.com/doc/123 for details",
    "query the database for all users please",
])
def test_prose(s):
    assert looks_like_prose(s)


@pytest.mark.parametrize("s", [
    "hi", "OK", UUID, "https://example.com/a/b/c?d=e", "aGVsbG8gd29ybGQgdGhpcyBpcyBiYXNlNjQ=", "2026-11-10T08:00:00Z",
    "2026-11-10 08:00:00", "1731200000.000100", "12, 14, 18", "MESSAGE_CREATE", "camelCaseIdentifier", "snake_case_name",
    "kebab-case-name", "com.example.app", "sam@example.com", "<p>Hello world from html</p>", '{"a": "b c d"}', "[1, 2, 3]",
    "query Foo($id: ID!) { issue(id: $id) { title } }", "mutation Bar { doThing(input: {a: 1}) { ok } }",
    "\x00\x01 bin ary data here", "weird �� bytes here", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "en-US,en;q=0.9", "a1b2 c3d4 e5f6 0718 2930", "", "   ",
])
def test_not_prose(s):
    assert not looks_like_prose(s)


@pytest.mark.parametrize("s", ["Привет, как дела сегодня?", "مرحبا كيف حالك اليوم", "你好，今天下午可以开会吗", "Γεια σου, τι κάνεις σήμερα;"])
def test_prose_in_other_scripts(s):
    assert looks_like_prose(s)
    assert harvest({"text": s})[0] == s


def test_sentence_containing_one_long_token():
    msg = "the bad commit is 9fceb02d0ae598e95dc970b74767f19372d61af8, can you revert it today?"
    assert looks_like_prose(msg) and harvest({"text": msg})[0] == msg


# ---------- decode_body ----------
def test_decode_json_form_multipart_ndjson_sse():
    assert decode_body(b'{"a": {"b": "c d e"}}', "application/json") == {"a": {"b": "c d e"}}
    assert decode_body(b"a=1&text=hello+there&x=%F0%9F%91%8D", "application/x-www-form-urlencoded") == {"a": "1", "text": "hello there", "x": "👍"}
    assert decode_body(b"tag=a&tag=b", "application/x-www-form-urlencoded") == {"tag": ["a", "b"]}
    mp = ('--B\r\nContent-Disposition: form-data; name="channel"\r\n\r\nC024BE91L\r\n'
          '--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nline one\r\nline two\r\n--B--\r\n')
    assert decode_body(mp.encode(), "multipart/form-data; boundary=B") == {"channel": "C024BE91L", "text": "line one\r\nline two"}
    assert decode_body(b'{"a":1,"b":"x"}\n{"c":"two words"}\n', "application/x-ndjson") == [{"a": 1, "b": "x"}, {"c": "two words"}]
    sse = b'event: message\ndata: {"delta": "Hello there"}\n\ndata: [DONE]\n\n'
    assert decode_body(sse, "text/event-stream") == [{"delta": "Hello there"}]


def test_decode_xssi_prefix_and_plain_text():
    assert decode_body(b")]}'\n[[\"wrb.fr\", \"two words\"]]", "") == [["wrb.fr", "two words"]]
    assert decode_body(b")]}'[[\"wrb.fr\", \"two words\"]]", "") == [["wrb.fr", "two words"]]
    assert decode_body(b"Remember to water the plants", "text/plain") == {"text": "Remember to water the plants"}
    assert decode_body(b"<html><body>Remember to water the plants</body></html>", "text/html") is None
    assert decode_body(b"", "application/json") is None and decode_body(None) is None


def test_binary_garbage_gives_no_text():
    rnd = random.Random(1234)
    for n in (64, 900, 20000):
        obj = decode_body(rnd.randbytes(n), "application/octet-stream")
        assert harvest(obj)[0] == "" if obj is not None else True
    gz = zlib.compress(("Could you send me the signed contract today? " * 30).encode())
    obj = decode_body(gz, "application/octet-stream")
    assert obj is None or harvest(obj)[0] == ""


def test_binary_garbage_request_stores_no_text(events):
    send(events, "Thing", "api.example.com", "/v1/upload", random.Random(5).randbytes(4000), ctype="application/octet-stream")
    t = kind(events)
    assert t["n_text"] == 0 and t["klass"] in ("signal", "background")
    assert all(r["text"] == "" for r in stored(events))


def test_strings_inside_a_binary_body():
    # protobuf-style: length-prefixed strings between non-UTF-8 bytes
    raw = b"\x0a\x2c\xff\xfe\x12\x29Please send me the signed contract today\x18\x01\x22\x10" + bytes(range(0x80, 0x90))
    assert printable_runs(raw) == [")Please send me the signed contract today"]
    text, _, _ = harvest(decode_body(raw, "application/grpc-web+proto"))
    assert "Please send me the signed contract today" in text


def test_strings_inside_a_binary_body_that_is_valid_utf8():
    raw = b"\x0a\x2c\x08\x01\x12\x28Please send me the signed contract today\x18\x01"
    obj = decode_body(raw, "application/grpc-web+proto")
    assert obj is not None and "signed contract" in harvest(obj)[0]


# ---------- secrets ----------
def test_mask_secrets():
    s = fake_secrets()
    masked = mask_secrets("deploy with " + " and ".join(s.values()) + " then tell me")
    for kind_, value in s.items():
        assert value not in masked and f"[{kind_}]" in masked
    assert masked.startswith("deploy with [") and masked.endswith("then tell me")
    assert mask_secrets("nothing secret here") == "nothing secret here" and mask_secrets("") == ""
    assert "hunter2" not in mask_secrets('{"password": "hunter2xyz"}')


# ---------- WebSocket frames ----------
def dispatch(t, d, s=1):
    return {"op": 0, "s": s, "t": t, "d": d}


MSG = {"id": 1437000000000000001, "channel_id": 1437000000000000002, "content": "are you free for a call at three?",
       "author": {"id": 1437000000000000003, "username": "priya", "global_name": "Priya", "bot": None},
       "mentions": [], "timestamp": "2026-11-10T08:15:30.123000+00:00", "tts": False}


def test_etf_roundtrip_through_decode_payload():
    assert decode_payload(etf(dispatch("MESSAGE_CREATE", MSG))) == dispatch("MESSAGE_CREATE", MSG)


def test_zstd_stream_of_etf_frames():
    c = zstandard.ZstdCompressor().compressobj()
    frames = []
    for i, text in enumerate(["are you free for a call at three?", "never mind, sorted it"]):
        payload = etf(dispatch("MESSAGE_CREATE", {**MSG, "content": text}, s=i + 1))
        frames.append(c.compress(payload) + c.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK))
    assert frames[0].startswith(b"\x28\xb5\x2f\xfd") and not frames[1].startswith(b"\x28\xb5\x2f\xfd")
    dec = SocketDecoder()
    out = [dec.feed(f) for f in frames]
    assert dec.mode == "zstd"
    assert [o["d"]["content"] for o in out] == ["are you free for a call at three?", "never mind, sorted it"]
    assert out[1]["s"] == 2 and out[0]["d"]["id"] == MSG["id"]            # snowflakes survive as big integers


def test_zstd_etf_frame_becomes_an_incoming_kind(events):
    c = zstandard.ZstdCompressor().compressobj()
    frame = c.compress(etf(dispatch("MESSAGE_CREATE", MSG))) + c.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
    obj = SocketDecoder().feed(frame)
    events.ws("Chatty", "gateway.chatty.example", "/?encoding=etf&v=9&compress=zstd-stream", frame, False, obj=obj)
    t = kind(events)
    assert t["sig"] == "Chatty|WS↓ gateway.chatty.example#MESSAGE_CREATE"
    assert t["klass"] == "incoming" and t["label"] == "message create"
    assert stored(events)[0]["text"] == "are you free for a call at three?"


def test_zlib_stream_json_frames_including_a_split_message():
    c = zlib.compressobj()
    one = c.compress(json.dumps({"op": 0, "t": "MESSAGE_CREATE", "d": {"content": "first message here"}}).encode()) + c.flush(zlib.Z_SYNC_FLUSH)
    two = c.compress(json.dumps({"op": 0, "t": "MESSAGE_CREATE", "d": {"content": "second message, " + "long " * 400}}).encode()) + c.flush(zlib.Z_SYNC_FLUSH)
    assert one.endswith(b"\x00\x00\xff\xff") and two.endswith(b"\x00\x00\xff\xff")
    dec = SocketDecoder()
    assert dec.feed(one)["d"]["content"] == "first message here"
    assert dec.mode == "zlib"
    half = len(two) // 2
    assert dec.feed(two[:half]) is None                                    # message not finished yet
    assert dec.feed(two[half:])["d"]["content"].startswith("second message, long")


def test_uncompressed_binary_frames():
    dec = SocketDecoder()
    assert dec.feed(b'{"type": "note", "text": "plain json in a binary frame"}') == {"type": "note", "text": "plain json in a binary frame"}
    assert dec.mode == "raw"
    assert dec.feed(etf({"t": "X"})) == {"t": "X"}
    assert dec.feed(b"\x00\x01\x02 not anything") is None and dec.feed(b"") is None
    assert dec.feed(b'{"still": "works after an unreadable frame"}') == {"still": "works after an unreadable frame"}


def test_joining_a_compressed_stream_midway_gives_up_quietly():
    c = zstandard.ZstdCompressor().compressobj()
    first = c.compress(etf(dispatch("READY", {"v": 9}))) + c.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
    second = c.compress(etf(dispatch("MESSAGE_CREATE", MSG))) + c.flush(zstandard.COMPRESSOBJ_FLUSH_BLOCK)
    dec = SocketDecoder()
    assert dec.feed(first[:4] + bytes(reversed(first[4:]))) is None        # looks like zstd, isn't a valid stream
    assert dec.broken and dec.feed(second) is None
    late = SocketDecoder()                                                 # never saw the stream header
    assert late.feed(second) is None


def test_truncated_or_malformed_etf_is_none_not_an_exception():
    good = etf(dispatch("MESSAGE_CREATE", MSG))
    for bad in (good[:20], b"\x83\xff\x00", b"\x83", b"\x83\x6c\x00\x00\x00\x05\x61\x01"):
        assert decode_payload(bad) is None


def test_ws_text_frames_json_in_and_out(events):
    events.ws("Thing", "ws.example.com", "/ws", json.dumps({"type": "message.new", "data": {
        "content": "Could you review my pull request today?", "author": {"username": "priya"}}}), False)
    events.ws("Thing", "ws.example.com", "/ws", json.dumps({"type": "message.send", "content": "hi"}), True)
    assert kind(events, "WS↓")["sig"] == "Thing|WS↓ ws.example.com#message.new"
    assert kind(events, "WS↓")["klass"] == "incoming" and kind(events, "WS↑")["klass"] == "action"
    assert [r["text"] for r in stored(events)] == ["Could you review my pull request today?", "hi"]


def test_ws_placeholder_for_unstored_binary_is_not_text(events):
    events.ws("Thing", "ws.example.com", "/ws", "[binary 512 bytes]", False)
    assert kind(events)["n_text"] == 0


# ---------- more shapes of text ----------
@pytest.mark.parametrize("value", ["TRUE", "NULL", "POST", "JSON", "ERROR", "MESSAGE_CREATE", "ABCDEFGH", "A1"])
def test_one_word_all_caps_constants_are_still_not_messages(value):
    assert harvest({"text": value})[0] == ""


def test_html_message_keeps_its_words_and_drops_markup():
    text, _, _ = harvest({"body": '<div dir="ltr">Hi <b>Sam</b>, see <a href="https://example.com/x?a=1&amp;b=2">the doc</a> &amp; reply'
                                  '<script>var x = "ignore this code please";</script><style>p { color: red }</style></div>'})
    assert text == "Hi Sam , see the doc & reply"
    assert harvest({"body": "<svg><path d='M0 0L10 10'/></svg>"})[0] == "" and not looks_like_prose("<p>Hello world from html</p>")


def test_a_passphrase_is_never_text_however_long():
    text, fields, _ = harvest({"passphrase": "correct horse battery staple and a few more words", "client_secret": "one two three four five six"})
    assert text == "" and fields == {}


def test_title_and_body_in_one_positional_record_are_joined_title_first():
    record = ["msg-a:r-8123456789012345678", None, [["Alice Example", "alice@example.com"]], "Quarterly budget review",
              "Hi Alice, could you send over the updated budget before Thursday? Thanks", 1731200000000]
    assert harvest([None, [[record]], "ui-2", 3])[0] == \
        "Quarterly budget review: Hi Alice, could you send over the updated budget before Thursday? Thanks"
    # a plain list of strings is several things, not a title and a body
    assert harvest({"parts": ["a first short paragraph", "and a second paragraph, which is the longer one"]})[0] == \
        "and a second paragraph, which is the longer one"


@pytest.mark.parametrize("s", ["नमस्ते आप कैसे हैं", "こんにちは、今日は会議できますか", "안녕하세요 오늘 회의 가능한가요", "שלום מה שלומך היום"])
def test_prose_in_more_scripts(s):
    assert looks_like_prose(s)


@pytest.mark.parametrize("s", ["ab Ωc Жd ؤe", "你好", "ЖЖ", "Ω", "9fceb02d0ae598e95dc970b74767f19372d61af8 d41d8cd98f00b204e9800998ecf8427e0011 a b c"])
def test_mixed_scripts_and_token_soup_are_not_prose(s):
    assert not looks_like_prose(s)


def test_random_bytes_do_not_read_as_text_in_another_script():
    # Short ASCII runs can pass for two words by chance (they always could); letters from other alphabets must not
    # make that any likelier: random bytes decode to a mix of scripts, a sentence is written in one.
    rnd = random.Random(99)
    for _ in range(300):
        raw = rnd.randbytes(rnd.choice((40, 300, 3000)))
        assert not any(ord(c) >= 0x250 and c.isalpha() for run in printable_runs(raw) for c in run)


def test_decode_socketio_frames():
    assert decode_body('42["chat message",{"text":"is the deploy finished yet?"}]') == \
        {"event": "chat_message", "data": [{"text": "is the deploy finished yet?"}]}
    assert decode_body('42/admin,17["user.joined",{"name":"Priya"},3]') == {"event": "user.joined", "data": [{"name": "Priya"}, 3]}
    assert decode_body('431[{"ok":true}]') == {"event": "ack", "data": [{"ok": True}]}
    assert harvest(decode_body('42["chat message",{"text":"is the deploy finished yet?"}]'))[::2] == ("is the deploy finished yet?", "chat_message")
    for frame in ("2", "3", "40", "42", '42["unterminated', "42 is the answer to it all, as they say"):
        obj = decode_body(frame)
        assert obj is None or "event" not in obj
