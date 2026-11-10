"""Universal events: turn any app's traffic into a small catalog of "kinds of things that happen".

No app-specific code here. Every decrypted exchange is reduced to
  - a signature: which app, which request shape (method + path template + an operation name
    found in the body or query: GraphQL operationName, JSON-RPC method, a WebSocket `type`…)
  - the human text in it, if any (the longest natural-language string, found by walking the body)
and counted per signature. Deterministic rules then sort signatures into
  action      you did something (a write with human text, or opening a page)
  incoming    something reached you (a pushed message with human text)
  signal      a quiet write without text (starred, toggled, saved…)
  background  telemetry, heartbeats, polling, presence: counted, never stored
  read        plain GETs: counted, never stored
Only action/incoming/signal events are stored (text + a few fields, secrets masked), so the UI
can show "what could I trigger on?" per app without a firehose, and triggers can bind to any
signature. Curated recognizers (Slack, Discord, WhatsApp…) emit richer events through emit().
Entry points: http() per request, ws() per WebSocket frame, sse() per event-stream response.
"""
from __future__ import annotations

import base64
import html
import json
import re
import threading
import time
import unicodedata
from collections import deque
from fnmatch import fnmatchcase
from urllib.parse import parse_qs, urlsplit

from engine import detect
from engine.store import credential_param

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_types (
  sig TEXT PRIMARY KEY, app TEXT, via TEXT, host TEXT, label TEXT,
  direction TEXT,                       -- out (you/app sent) | in (pushed to you)
  klass TEXT,                           -- action | incoming | signal | background | read
  curated INTEGER DEFAULT 0, source TEXT DEFAULT 'network',   -- network | local
  n INTEGER DEFAULT 0, n_text INTEGER DEFAULT 0, first_ts REAL, last_ts REAL,
  example TEXT, fields TEXT, muted INTEGER DEFAULT 0,
  schema TEXT                           -- wide-event field names seen for this kind: {name: [type, n, example]}
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sig TEXT, app TEXT, direction TEXT,
  who TEXT, place TEXT, text TEXT, fields TEXT, key TEXT UNIQUE,
  wide TEXT,                            -- the wide record (flat dotted fields, bounded), see flatten_wide
  op TEXT                               -- operation name, for queries
);
CREATE INDEX IF NOT EXISTS events_sig ON events(sig, id);
"""

BROWSERS = {"Zen", "Firefox", "Google Chrome", "Chromium", "Safari", "Arc", "Brave Browser", "Brave",
            "Microsoft Edge", "Opera", "Vivaldi", "Orion", "Dia", "com.apple.WebKit.Networking",
            "Safari Technology Preview", "LibreWolf", "Floorp", "firefox", "chrome", "msedge",
            "Microsoft Edge WebView2"}
SITE_NAMES = {
    "mail.google.com": "Gmail", "calendar.google.com": "Google Calendar", "docs.google.com": "Google Docs",
    "drive.google.com": "Google Drive", "meet.google.com": "Google Meet", "www.google.com": "Google Search",
    "github.com": "GitHub", "gitlab.com": "GitLab", "linear.app": "Linear", "www.notion.so": "Notion",
    "app.notion.com": "Notion", "notion.so": "Notion", "x.com": "X", "twitter.com": "X", "x.ai": "xAI", "grok.com": "Grok",
    "web.whatsapp.com": "WhatsApp Web", "app.slack.com": "Slack", "discord.com": "Discord",
    "chatgpt.com": "ChatGPT", "claude.ai": "Claude", "www.linkedin.com": "LinkedIn", "www.youtube.com": "YouTube",
    "outlook.office.com": "Outlook", "outlook.live.com": "Outlook", "teams.microsoft.com": "Microsoft Teams",
    "www.figma.com": "Figma", "trello.com": "Trello", "app.asana.com": "Asana", "www.reddit.com": "Reddit",
    "news.ycombinator.com": "Hacker News", "web.telegram.org": "Telegram", "www.instagram.com": "Instagram",
    "www.amazon.co.uk": "Amazon", "www.amazon.com": "Amazon", "app.granola.ai": "Granola", "notes.granola.ai": "Granola",
}
OWN_APPS = {"Ambient", "ambient", "ambient-engine", "ambient-engine.exe"}
# Command-line tools whose process name isn't what a person would call them.
PROC_NAMES = {"gh": "GitHub CLI", "git-remote-https": "git", "git-remote-http": "git", "Code Helper": "Visual Studio Code",
              "Code Helper (Plugin)": "Visual Studio Code", "Electron": "Visual Studio Code"}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
# Sign-in and payment endpoints: never turned into events. Matched on the lowercased path with camelCase
# split ("signInWithPassword" -> "sign in with password"), each keyword as a whole word.
_SENS = (r"log[-_ ]?[io]n|sign[-_ ]?in|sign[-_ ]?up|sign[-_ ]?on|register|registration|o?auth[02]?|token|password|passwd|"
         r"credential|authoriz(?:e|ation)|authenticat(?:e|ion)|sso|saml|2fa|mfa|otp|webauthn|passkey|checkout|payment|"
         r"billing|(?:credit|debit|bank|payment)[-_ ]?card|card[-_ ]?(?:token|number|detail|verification)")
_SENS_WORDS = re.compile(rf"(?<![a-z0-9])(?:{_SENS})s?(?![a-z0-9])|(?:^|/)sessions?/?$")      # POST /sessions is a sign-in
_SENS_HOST = re.compile(rf"(?<![a-z0-9])(?:{_SENS}|idp|identity|accounts|cognito-idp|cognito-identity|identitytoolkit|"
                        r"securetoken|okta)s?(?![a-z0-9])")
# "card" alone is as often a kanban card as a payment card: only the body can tell.
_CARD = re.compile(r"(?<![a-z0-9])cards?(?![a-z0-9])")
_PAY_BODY = re.compile(r"(?:^|[\"'\[&{,_\s])(?:card[-_ ]?)?(?:number|pan|cvc2?|cvv2?|cv2|exp(?:iry|iration)?(?:[-_ ]?(?:month|year|date))?|"
                       r"security[-_ ]?code|pass(?:word|wd|code)?|pwd|secret|iban|sort[-_ ]?code)[\"'\]]?\s*[:=]", re.I)


def _spaced(s: str) -> str:
    """Lowercase, with camelCase humps separated."""
    return s if s.islower() else _CAMEL.sub(r"\1 \2", s).lower()


class _SensitivePath:
    """The path-only test (kept pattern-like for callers that do SENSITIVE_PATH.search(path)). It has no
    body to look at, so any `card` word counts; Events uses sensitive(), which looks closer."""

    @staticmethod
    def search(path: str):
        p = _spaced(path or "")
        return _SENS_WORDS.search(p) or _CARD.search(p)


SENSITIVE_PATH = _SensitivePath()


def sensitive(host: str, path: str, body=None) -> bool:
    """A sign-in or payment exchange, by its path, its host, or (for /cards endpoints) what it carries."""
    p = _spaced(path.split("?", 1)[0])
    if _SENS_WORDS.search(p) or _SENS_HOST.search(host.lower()):
        return True
    if "card" in p and _CARD.search(p) and body:
        head = body[:8192]
        return bool(_PAY_BODY.search(head.decode("latin-1") if isinstance(head, bytes) else head))
    return False


# Machine chatter. Searched on a signature's path and operation (lowercased, camelCase split); every word
# is a whole word, so "hotels" is not otel and "/collections" is not /collect. Vendor names also count in the host.
_B, _E = r"(?<![a-z])", r"(?![a-z])"
_VENDORS = (_B + r"(?:sentry|datadog\w*|segment(?=\.)|amplitude|mixpanel|posthog|statsig\w*|launchdarkly|crashlytics|"
            r"bugsnag|onesignal|doubleclick|googletagmanager|google-analytics|safebrowsing|detectportal|"
            r"portal-detection|remote-settings|connectivity-check)")
TELEMETRY = re.compile(
    _B + r"(?:telemetry|analytics|metrics?|clogs?|beacons?|logging|otel|heart[-_ ]?beats?|keep[-_ ]?alive|"
    r"experiments?|feature[-_ ]?flags?|gabo-receiver|one ?collector|impressions?|page[-_ ]?views?|"
    r"client[-_ ]events?|rate[-_ ]?limits?|content[-_ ]part|output[-_ ]item|record[-_ ]events?|reconnect_url|"
    r"sessions_replace|ready_supplemental|content_inventory|passive_update|voice_state|guild_member|cdn-cgi|"
    r"challenge-platform|turnstile|captcha|upload-pack|receive-pack)" + _E
    + r"|/(?:rum|collect|logs?|traces?|science|flags|perf|stats|vitals|gen_204|jsd|ocsp|csp-?reports?|js[-_ ]?errors?|"
    r"log[-_ ]events?|batch[-_ ]log|events?/batch)" + _E
    + r"|/track(?:/(?:events?|batch))?/?(?=[#+]|$)|\.delta" + _E + "|" + _VENDORS, re.I)
TELEMETRY_HOST = re.compile(_VENDORS + "|" + _B + r"(?:telemetry|analytics|metrics?|beacons?|logging|logs|otel|rum|clogs?)" + _E
                            + r"|push\.services\.mozilla|notifications\.bitwarden|" + _B + "ocsp" + _E, re.I)
# Words that mean chatter only as the operation itself (a socket's `user_typing`, POST …/typing,
# /api/presence.set), not further up a path: /typing-tests/55/results is somebody's typing score.
CHATTER = re.compile(_B + r"(?:ping|pong|presence|typing|ack|hello)" + _E, re.I)
# Analytics SDK calls (Segment and its many clones): POST …/v1/identify, …/page, …/track. Only when the
# whole label is one of these words: "identify speaker" is a real operation.
ANALYTICS_LABELS = frozenset({"identify", "page", "track", "group", "alias"})
# Operation names that only fetch: POSTs in form, reads in meaning.
READ_OP = re.compile(r"^(get|list|fetch|info|counts?|history|check|load|read|poll|sync|refresh|status|lookup|resolve|"
                     r"stats|prefs|suggest\w*|autocomplete|typeahead|validate|preview|unfurl|should|eligib\w*|"
                     r"permissions?|membership|subscribe|view|find|query|batch get|bulk|can|is|has|open|mark|"
                     r"heartbeat|connect|negotiate|configure|init\w*|bootstrap|boot|start session|token)\b|"
                     r"\b(get|list|info|counts|history|stats|prefs|replies|generic info|team info|profile get|"
                     r"targeting|reacji|ticket stats|should reload|issue advanced)\b", re.I)
OP_KEYS = ("operationName", "method", "type", "action", "event", "op", "command", "cmd", "t", "kind", "name", "rpcids")
TEXT_KEYS = re.compile(r"(^|_|\.)(text|content|body|message|msg|title|subject|summary|description|query|prompt|"
                       r"comment|caption|note|snippet|question|answer|reply|input|search|q|name|label|topic)s?$", re.I)
# `ts` and `os` only as whole words: "parts", "comments", "arguments", "videos" are not timestamps or OS names.
# Credential-ish keys are junk too, so a password never lands in an event's fields.
JUNK_KEYS = re.compile(r"(id|uuid|guid|token|hash|url|uri|href|key|sig|signature|cursor|nonce|(?<![a-z])ts|time|date|type|"
                       r"status|version|locale|lang|platform|agent|(?<![a-z])os|device|session|trace|span|etag|csrf|color|"
                       r"icon|avatar|image|src|path|file|mime|format|encoding|origin|referr?er|timezone|tz|slug|"
                       r"pass(word|wd|phrase|code)?|pwd|secret|otp|cvv|cvc|ssn|credential|auth(orization)?)s?$", re.I)
# The part of JUNK_KEYS that is never text however much it reads like a sentence (a passphrase is four words).
# The rest (status, type, path…) is machine data when short, but people do write sentences under those names.
CRED_KEYS = re.compile(r"(token|key|sig|signature|nonce|csrf|hash|session|cursor|etag|pass(word|wd|phrase|code)?|pwd|"
                       r"secret|otp|cvv|cvc|ssn|credential|auth(orization)?)s?$", re.I)
STRONG_READ = re.compile(r"\b(get|list|fetch|info|counts?|history|lookup|suggest\w*|autocomplete|typeahead|find|"
                         r"query|read|replies|stats|prefs|validate|preview|unfurl)\b", re.I)
# The same without the nouns: POST /comments/{id}/replies with a sentence in it writes a reply, it doesn't read them.
READ_VERB = re.compile(r"\b(get|list|fetch|lookup|suggest\w*|autocomplete|typeahead|find|query|read|validate|preview|"
                       r"unfurl)\b", re.I)
WRITE_WORD = re.compile(r"\b(create|add|new|update|edit|delete|remove|post|send|save|set|submit|publish|reply|"
                        r"comment|upload|share|invite|assign|move|rename|archive|star|react|vote)\w*\b", re.I)
STRONG_TEXT = re.compile(r"(^|_|\.)(text|content|body|message|msg|comment|caption|reply|prompt|query|q|search|"
                         r"title|subject|summary|description|note)$", re.I)
GENERIC_PATH = re.compile(r"^(graphql|gql|api|rpc|batch\w*|query|gateway|v\d+|index|execute\w*|invoke|call|data|"
                          r"ajax|jsonrpc|trpc|events?|messages?|send|\{id\})?$", re.I)
ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8}-[0-9a-f-]{20,}|[0-9a-f]{12,}|[A-Z][A-Z0-9]{8,}|[^/]*@[^/]+\.[a-z]+|"
                    r"[A-Z][A-Z0-9]{1,9}-\d+|\d+\.[a-z]{2,5}|"          # ticket keys (PROJ-123), ids with a suffix (98231.json)
                    r"[!$][^/]{6,}|%21[^/]{6,}|[#@][^/]+:[^/]+\.[a-z]+|"  # Matrix-style sigil ids (!room:server)
                    r"(?=[^/]*\d)[A-Za-z0-9_\-=.~:%+]{16,}|[^/]{48,})$")
# Short random ids (YouTube's dQw4w9WgXcQ, nanoid, Trello short links) have no fixed shape, but unlike a
# camelCase name their case flips every letter or two.
_MIXED = re.compile(r"[A-Za-z0-9_-]{8,24}")
_HUMP = re.compile(r"[A-Z]+[a-z]*|[a-z]+")
NOT_PROSE = re.compile(r"^(https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|[A-Za-z0-9+/=_-]{24,}|[0-9a-f-]{16,}|"
                       r"\d{4}-\d\d-\d\d[T ][\d:.+Z-]*|[\d\s.,:+-]+|[A-Z0-9_]{3,}|[a-z]+([A-Z][a-z]+)+|[\w-]+(\.[\w-]+)+|"
                       r"[a-z0-9]+(_[a-z0-9]+)+|[a-z0-9]+(-[a-z0-9]+)+)$")
WORD = re.compile(r"[A-Za-zÀ-ɏ]{2,}")
UWORD = re.compile(r"[^\W\d_]{2,}")                 # a word in any script
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f�]")
BINARY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1d\x1f]")    # CTRL without \x1e, which SignalR uses to end a JSON frame
SPACELESS = {"cjk", 0x0e, 0x0f, 0x10, 0x17}         # scripts written without spaces: Chinese, Japanese, Thai, Lao, Tibetan, Burmese, Khmer
TAGS = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^<>]{0,300}>", re.S | re.I)
OP_NAME = re.compile(r"[\w.:/\-]+")                 # an operation name is an identifier: no spaces
OP_MARKED = re.compile(r"[._:/]|[a-z][A-Z]")
OPAQUE = re.compile(r"[0-9a-f-]{12,}|(?=\S*\d)[A-Za-z0-9+/_=.~-]{16,}|(?=\S*[a-z])(?=\S*[A-Z])[A-Za-z0-9+/_=-]{24,}")
CAPS_WORDS = re.compile(r"[A-Z]{2,5}")              # OK, YES, LGTM: a reply, unless it is one of these
CAPS_MACHINE = frozenset("TRUE FALSE NULL NONE GET POST PUT HEAD TEXT HTML JSON XML PING PONG ACK ASC DESC AND NOT ALL ANY "
                         "INFO WARN ERROR DEBUG AUTO USER".split())
SOCKETIO = re.compile(r"4[2356](?:\d+-)?(?:/[^,\[]*,)?\d*(\[.*)\Z", re.S)
K_TEXT, K_STRONG, K_JUNK, K_CRED = 1, 2, 4, 8
_KEYCLASS: dict[str, int] = {}


def site_name(host: str) -> str:
    host = host.lower()
    if re.fullmatch(r"\d+(\.\d+){3}", host) or ":" in host:      # an IP address has no site name in it
        return host
    parts = [p for p in host.split(".") if p]
    for i in range(len(parts) - 1):                 # alive.github.com -> github.com -> "GitHub"
        name = SITE_NAMES.get(".".join(parts[i:])) or SITE_NAMES.get("www." + ".".join(parts[i:]))
        if name and (i == 0 or name not in ("Google Search",)):
            return name
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac", "gov") and len(parts[-1]) == 2:
        core = parts[-3]
    else:
        core = parts[-2] if len(parts) >= 2 else host
    return core[:1].upper() + core[1:]


def host_template(host: str) -> str:
    return re.sub(r"\d+", "N", host.lower())


def path_template(path: str) -> str:
    segs = []
    for seg in path.split("?")[0].split("/"):
        segs.append("{id}" if seg and (ID_SEG.match(seg) or _mixed_id(seg)) else seg)
    out = "/".join(segs)
    return out[:120] or "/"


def _mixed_id(seg: str) -> bool:
    if seg.islower() or seg.isupper() or not _MIXED.fullmatch(seg):
        return False
    humps = _HUMP.findall(seg)
    return len(humps) >= 4 and sum(map(len, humps)) < len(humps) * 2.2 and any(len(h) == 1 for h in humps)


def words(s: str) -> str:
    """'chat.postMessage' / 'MESSAGE_CREATE' / 'CreateIssueMutation' -> 'chat post message' …"""
    s = re.sub(r"\{id\}|\{name\}|\$rpc", " ", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"[_\-./:{}#|]+", " ", s)
    s = re.sub(r"\b(api|v\d+|rest|rpc|graphql|mutation|query|ajax|json|www|web|internal|i|u|\d+)\b", " ", s, flags=re.I)
    out = []
    for w in re.sub(r"\s+", " ", s).strip().lower().split(" "):
        w = {"msg": "message", "msgs": "messages"}.get(w, w)
        if w and (not out or out[-1] != w):         # "sync.sync" -> "sync"
            out.append(w)
    return " ".join(out)


SCAFFOLD = frozenset({"user", "users", "me", "self", "current"})
VAGUE = frozenset({"sync", "batch", "batchexecute", "batch execute", "execute", "mutate", "data", "request", "ops",
                   "operations", "apply", "transactions", "save transactions", "submit transactions", "write"})


def nice(label: str, sig: str = "") -> str:
    """The label as shown to a person. API scaffolding words go ("update user starred" -> "update starred"),
    and an endpoint whose name says nothing ("sync", "batch") but which carries your words is "save a change"."""
    ws = label.split()
    kept = [w for w in ws if w not in SCAFFOLD]
    if len(ws) >= 3 and len(kept) >= 2:
        ws = kept
    out = " ".join(ws)
    if sig.endswith("+text") and out in VAGUE:
        return "save a change"
    return out


def _script(head: str):
    """The one non-Latin script a string is written in, if it is: 'cjk' or a Unicode block number.
    None for Latin text and for a mix of scripts (bytes that happened to decode)."""
    counts: dict = {}
    for c in head:
        o = ord(c)
        if o >= 0x250 and c.isalpha():
            b = ("cjk" if 0x2e80 <= o <= 0x9fff or 0xf900 <= o <= 0xfaff or 0xff00 <= o <= 0xffef
                 else "hangul" if 0xac00 <= o <= 0xd7af else o >> 8)
            counts[b] = counts.get(b, 0) + 1
    if not counts:
        return None
    b, top = max(counts.items(), key=lambda kv: kv[1])
    return b if top >= 3 and top >= 0.8 * sum(counts.values()) else None


def looks_like_prose(s: str) -> bool:
    """A human sentence or phrase, in any script: at least two words, mostly letters, not made of opaque tokens."""
    s = s.strip()
    if not (3 <= len(s) <= 20000) or s[0] in "<{[" or NOT_PROSE.match(s):
        return False
    head = s[:600]
    if CTRL.search(head):                           # binary that happened to decode
        return False
    toks = head.split()
    ascii_ = head.isascii()
    script = None if ascii_ else _script(head)
    spaceless = script in SPACELESS
    if len(toks) < 2 and not spaceless:
        return False
    if not spaceless and max(map(len, toks)) > 34:
        # One long token (a commit sha, a pasted key) inside a sentence is still a sentence; judge the rest.
        short = [t for t in toks if len(t) <= 34 or t.startswith("http")]
        if len(short) < len(toks):
            if len(toks) - len(short) > 1 + len(toks) // 8 or len(short) < 4:
                return False
            head = " ".join(short)
    if re.match(r"^(query|mutation|fragment|subscription)\b[^.]*[({]", head):      # a GraphQL document
        return False
    if ascii_:
        letters = sum(c.isalpha() or c.isspace() for c in head)
    else:                                           # combining marks belong to their letters (Devanagari, Thai, Arabic)
        letters = sum(c.isalpha() or c.isspace() or (c > "\u02ff" and unicodedata.category(c)[0] == "M") for c in head)
    if letters < len(head) * 0.72:
        return False
    if spaceless:
        return letters >= 4
    return len((UWORD if script else WORD).findall(head)) >= 2


def _keyclass(key: str) -> int:
    """What a key name says about its value (text / strong text / machine data / credential), whatever its
    spelling: messageText, message_text and message[text] are the same key. Cached: key names repeat."""
    c = _KEYCLASS.get(key)
    if c is None:
        k = _CAMEL.sub(r"\1_\2", key).lower().replace("[", ".").replace("]", "").rstrip(".")
        c = ((K_TEXT if TEXT_KEYS.search(k) else 0) | (K_STRONG if STRONG_TEXT.search(k) else 0)
             | (K_JUNK if JUNK_KEYS.search(k) else 0) | (K_CRED if CRED_KEYS.search(k) else 0))
        if len(_KEYCLASS) > 20000:
            _KEYCLASS.clear()
        _KEYCLASS[key] = c
    return c


def _op_ok(key: str, v) -> bool:
    """Could this value be an operation name? An identifier, never free text: `name` is as often the thing's
    title ("Groceries") as an event name ("page_view"), so there it must also be spelled like an identifier."""
    return (isinstance(v, str) and 2 <= len(v) <= 80 and OP_NAME.fullmatch(v) is not None and not v.isdigit()
            and (key != "name" or OP_MARKED.search(v) is not None or (v.isupper() and len(v) > 5)))


def _plain(s: str) -> str:
    """Text of a small piece of HTML (Teams, Outlook and Intercom send messages as <p>\u2026</p>)."""
    return " ".join(html.unescape(TAGS.sub(" ", s[:20000])).split())


def _maybe_json(s: str):
    s2 = s.lstrip()
    if s2.startswith(")]}'"):                       # Google's anti-XSSI prefix, with or without a newline after it
        s2 = s2[4:].lstrip()
    if len(s2) > 12 and s2[0] in "[{":
        try:
            return json.loads(s2)
        except Exception:
            return None
    return None


def harvest(obj, limit: int = 30000) -> tuple[str, dict, str | None]:
    """Walk a decoded body. Returns (best human text, a few salient fields, operation name)."""
    cands: list[tuple] = []     # (score, key, text, the positional array it sits in or None, its place there)
    small: dict[str, str] = {}
    op: list[str] = []
    budget = [limit]

    def walk(x, key: str, depth: int, row=None, pos: int = 0):
        if budget[0] <= 0 or depth > 40:        # Google-style payloads nest arrays 15+ levels deep
            return
        budget[0] -= 1
        if isinstance(x, dict):
            if depth <= 2 and not op and isinstance(x.get("query"), str):
                m = re.match(r"\s*(query|mutation|subscription)\s+(\w+)", x["query"])
                if m:
                    op.append(("" if m.group(1) == "mutation" else "get ") + m.group(2))
                elif x["query"].lstrip().startswith(("{", "query", "fragment")):
                    op.append("get query")
            if depth <= 2 and not op:
                for k in OP_KEYS:
                    if _op_ok(k, x.get(k)):
                        op.append(x[k])
                        break
                else:                           # SignalR: {"type": 1, "target": "ReceiveMessage", "arguments": […]}
                    if isinstance(x.get("arguments"), list) and _op_ok("target", x.get("target")):
                        op.append(x["target"])
            for k, v in list(x.items())[:200]:
                walk(v, str(k), depth + 1)
        elif isinstance(x, (list, tuple)):
            # Array-framed sockets (Phoenix: [join_ref, ref, topic, event, payload]) name the event in a short
            # string before the payload; the one nearest the payload is the event, the ones before it say where.
            if depth == 0 and not op and 2 <= len(x) <= 6 and isinstance(x[-1], (dict, list)):
                head = x[:-1]
                if all(v is None or isinstance(v, (str, int)) for v in head):
                    for v in reversed(head):
                        if _op_ok("", v) and len(v) <= 60 and not re.search(r"\d", v):
                            op.append(v)
                            break
            part = x[:200]
            mixed = x if any(not isinstance(v, str) for v in part) else None      # a record, not a list of strings
            for i, v in enumerate(part):
                walk(v, key, depth + 1, mixed, i)
        elif isinstance(x, bytes):
            try:
                walk(x.decode("utf-8"), key, depth, row, pos)
            except Exception:
                pass
        elif isinstance(x, str):
            inner = _maybe_json(x) if depth < 30 else None
            if inner is not None:
                walk(inner, key, depth + 1)
                return
            s = x.strip()
            if s[:1] == "<" and ">" in s:
                s = _plain(s)
            if not s:
                return
            kc = _keyclass(key)
            if looks_like_prose(s):
                score = 2.0 + min(len(s), 240) / 120
                if kc & K_TEXT:
                    score += 3
                if kc & K_CRED or (kc & K_JUNK and s.count(" ") < 4):
                    score -= 4
                cands.append((score, key, s, row, pos))
            elif (kc & K_STRONG and 2 <= len(s) <= 40 and re.search(r"[A-Za-z]{2}", s)
                  and not re.match(r"^(https?://|true$|false$|null$|none$|[0-9a-f-]{16,}$|[\w.+-]+@|[<{\[]|"
                                   r"(query|mutation|fragment|subscription)\b.*[({]|"      # a short GraphQL document
                                   r"[a-z0-9]+([-_.][a-z0-9]+)+$|[A-Z0-9_]+$)", s)
                  or kc & K_STRONG and CAPS_WORDS.fullmatch(s) and s not in CAPS_MACHINE):
                cands.append((3.0, key, s, row, pos))  # a one-word message is still a message
            elif (len(s) <= 60 and key and not kc & K_JUNK and len(small) < 8 and re.search(r"[A-Za-z]", s)
                  and not OPAQUE.fullmatch(s)):         # a field is something a person could read
                small.setdefault(key, s)
        elif isinstance(x, bool):
            pass
        elif isinstance(x, (int, float)) and key and len(small) < 8 and _keyclass(key) & K_TEXT:
            small.setdefault(key, str(x))

    walk(obj, "", 0)
    cands.sort(key=lambda c: -c[0])
    best = cands[0] if cands and cands[0][0] >= 2.2 else None
    text = best[2][:4000] if best else ""
    # A title right before its body in one positional record (Gmail and Google's f.req arrays have no key
    # names): keep both, title first. Two strings that merely share a key name are two separate things.
    if best and best[3] is not None:
        before = [c for c in cands[1:8] if c[3] is best[3] and c[4] < best[4] and c[0] >= 2]
        if before:
            other = max(before, key=lambda c: c[4])[2]
            if len(other) < len(text) and len(other) <= 200 and other not in text:
                text = f"{other}: {text}"[:4000]
    fields = dict(small)
    for score, key, s, _row, _pos in cands[1:6]:
        if score >= 2 and key and key not in fields:
            fields[key] = s[:200]
    return text, fields, (op[0] if op else None)


def printable_runs(data: bytes) -> list[str]:
    """Human strings inside a binary body (protobuf, msgpack…): UTF-8 runs with spaces in them."""
    out = []
    for m in re.finditer(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xf4]{5,}", data[:200_000]):
        try:
            s = m.group().decode("utf-8")
        except Exception:
            continue
        if looks_like_prose(s) and " " in s:
            out.append(s)
            if len(out) >= 20:
                break
    return out


def decode_body(raw: bytes | str | None, ctype: str = ""):
    """Bytes/str body -> a Python object harvest() can walk."""
    if not raw:
        return None
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except Exception:
            return {"strings": printable_runs(raw)}
    else:
        text = raw
    t = text.lstrip()
    if t[:1] in "[{" or t.startswith(")]}'"):
        j = _maybe_json(t)
        if j is not None:
            return j
        lines = [ln for ln in t.splitlines() if ln.strip()[:1] in "[{"]      # NDJSON / batch
        objs = [o for o in (_maybe_json(ln) for ln in lines[:50]) if o is not None]
        if objs:
            return objs
    if t[:1] == "4" and t[1:2] in ("2", "3", "5", "6"):      # socket.io: 42["event", payload…], 43[ack…]
        m = SOCKETIO.match(t)
        try:
            arr = json.loads(m.group(1)) if m else None
        except Exception:
            arr = None
        if isinstance(arr, list) and arr:
            if t[1] in "25" and isinstance(arr[0], str):
                return {"event": re.sub(r"\s+", "_", arr[0].strip())[:80], "data": arr[1:]}
            return {"event": "ack", "data": arr}
    if "x-www-form-urlencoded" in ctype or (re.match(r"^[\w.%\[\]-]+=", t) and "\n" not in t[:200] and len(t) < 200_000):
        try:
            q = parse_qs(t, keep_blank_values=False)
            if q:
                return {k: (v[0] if len(v) == 1 else v) for k, v in q.items()}
        except Exception:
            pass
    if "multipart/form-data" in ctype:
        parts = {}
        for m in re.finditer(r'name="([^"]+)"\r?\n\r?\n(.*?)\r?\n--', text, re.S):
            if len(m.group(2)) < 20000:
                parts[m.group(1)] = m.group(2)
        return parts or None
    if "text/event-stream" in ctype:
        objs = [o for o in (_maybe_json(ln[5:]) for ln in t.splitlines() if ln.startswith("data:")) if o is not None]
        return objs[:50] or None
    if isinstance(raw, bytes) and BINARY.search(text[:2000]):    # small protobuf frames are often valid UTF-8
        return {"strings": printable_runs(raw)}
    if looks_like_prose(t[:2000]) and len(t) < 20000 and "<html" not in t[:500].lower():
        return {"text": t}
    return None


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a credential with its kind, e.g. [aws_access_key]."""
    if not text:
        return text
    for kind, rx in detect.SECRETS:
        try:
            text = rx.sub(f"[{kind}]", text)
        except Exception:
            pass
    return text


def mask_fields(fields: dict | None) -> dict | None:
    """mask_secrets for an event's fields (values short enough to hold no credential are left alone)."""
    if not fields:
        return fields
    return {k: (mask_secrets(v) if isinstance(v, str) and len(v) >= 8 else v) for k, v in fields.items()}


# ---------- wide events ----------
# Every stored event also keeps a "wide" record: flat, typed, dotted fields in the style of a canonical
# log line (app, host, method, path and its learned parameters, operation, direction, who/where, title,
# recipients, urls, status, and the useful scalars of the request/frame body as `fields.a.b` and of the
# reply as `resp.a.b`). Bounded: WIDE_MAX_FIELDS keys, WIDE_STR chars per string, WIDE_MAX_BYTES per row.
# Credential-named keys are dropped, every string goes through mask_secrets, and sign-in/payment
# exchanges never become events at all (sensitive()).
WIDE_MAX_FIELDS = 60
WIDE_STR = 160
WIDE_MAX_BYTES = 2000
WIDE_BODY_FIELDS = 36                 # of which at most this many come from bodies
SCHEMA_MAX_FIELDS = 80                # per kind, in event_types.schema
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_RECIPIENT_KEYS = re.compile(r"(^|\.)(to|cc|bcc|recipients?|attendees?|participants?|invitees?|members?|guests?|"
                             r"assignees?|reviewers?)(\[\]|\.|$)", re.I)
_TITLE_KEYS = re.compile(r"(^|\.)(title|subject|summary|name|heading|topic)$", re.I)
_PLACE_KEYS = re.compile(r"(^|\.)(channel|channel_?name|room|chat|thread|conversation|folder|board|project|"
                         r"repo|repository|team|workspace|calendar)(_?name)?$", re.I)
_GQL_DOC = re.compile(r"\s*(query|mutation|subscription|fragment)\b[^{]*\{", re.S)


def _wide_key(k) -> str | None:
    """A body key as a field-name part. Keys that are themselves data (an id, an email, a long blob used as a
    dict key) become `*` so field names stay a schema, not a list of values."""
    k = str(k)
    if not k or len(k) > 40 or ID_SEG.match(k) or _EMAIL.fullmatch(k) or _mixed_id(k) or not re.search(r"[A-Za-z]", k):
        return "*"
    return re.sub(r"[^\w\-]", "_", k)


def _wide_str(key: str, s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    if s[:1] == "<" and ">" in s:
        s = _plain(s)
    if _GQL_DOC.match(s):
        return None                                   # a GraphQL document is the request's shape, not its data
    last = key.rsplit(".", 1)[-1]
    idish = bool(re.search(r"(id|ids|key|uuid|guid|number)$", last, re.I))
    if OPAQUE.fullmatch(s) and not (idish and len(s) <= 64):
        return None                                   # an opaque blob under a non-id name: could be anything
    s = mask_secrets(s)
    return s if len(s) <= WIDE_STR else s[:WIDE_STR - 1] + "…"


def flatten_wide(obj, prefix: str = "fields", limit: int = WIDE_BODY_FIELDS) -> dict:
    """The useful scalars of a decoded body as dotted keys: `fields.event.title`, `fields.event.attendees.count`,
    `fields.event.attendees[].email` ("a@x, b@y"). Lists of records keep a count and the values of their first
    few records; positional arrays (Google's f.req) are walked by index. Credential-named keys never appear."""
    out: dict = {}
    budget = [4000]

    def put(k, v):
        if len(out) >= limit or k in out and not isinstance(out[k], str):
            return
        if k in out:                                  # a field of several records: keep a short joined list
            if isinstance(v, str) and len(out[k]) < WIDE_STR and v not in out[k].split(", "):
                out[k] = (out[k] + ", " + v)[:WIDE_STR]
            return
        out[k] = v

    def walk(x, key: str, depth: int):
        if budget[0] <= 0 or depth > 8 or len(out) >= limit:
            return
        budget[0] -= 1
        if isinstance(x, dict):
            for k, v in list(x.items())[:80]:
                if _keyclass(str(k)) & K_CRED:
                    continue
                walk(v, f"{key}.{_wide_key(k)}", depth + 1)
        elif isinstance(x, (list, tuple)):
            if not x:
                put(f"{key}.count", 0)
                return
            if all(isinstance(v, dict) for v in x[:20]):
                put(f"{key}.count", len(x))
                for v in x[:3]:
                    walk(v, key + "[]", depth + 1)
            elif all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in x[:20]):
                put(f"{key}.count", len(x))
                vals = [w for w in (_wide_str(key, str(v)) for v in x[:5]) if w]
                if vals:
                    put(key, ", ".join(vals)[:WIDE_STR])
            else:                                     # a positional record: walk it by index
                for i, v in enumerate(x[:12]):
                    walk(v, f"{key}.{i}", depth + 1)
        elif isinstance(x, bool):
            put(key, x)
        elif isinstance(x, (int, float)):
            if x == x and abs(x) < 1e15:              # finite, and not an id dressed as a number
                put(key, x)
        elif isinstance(x, bytes):
            try:
                walk(x.decode("utf-8"), key, depth)
            except Exception:
                pass
        elif isinstance(x, str):
            inner = _maybe_json(x) if len(x) < 100_000 else None
            if inner is not None:
                walk(inner, key, depth + 1)
                return
            v = _wide_str(key, x)
            if v is not None:
                put(key, v)

    walk(obj, prefix, 0)
    return out


def _json_obj(text) -> object | None:
    if not text:
        return None
    if isinstance(text, bytes):
        text = text[:200_000].decode("utf-8", "replace")
    return _maybe_json(text[:200_000])


def wide_canon(wide: dict, text: str = "") -> dict:
    """Canonical fields found in the flattened body: title, recipients, place, urls."""
    out = {}
    for k, v in wide.items():
        if not isinstance(v, str) or not k.startswith(("fields.", "resp.")):
            continue
        tail = k.split(".", 1)[1]
        if "title" not in out and _TITLE_KEYS.search(tail) and not _EMAIL.fullmatch(v) and len(v) >= 2:
            out["title"] = v
        if _RECIPIENT_KEYS.search(tail):
            found = _EMAIL.findall(v) or ([v] if len(v) <= 60 else [])
            if found:
                have = out.get("recipients", "")
                out["recipients"] = ", ".join(dict.fromkeys(([have] if have else []) + found))[:WIDE_STR]
        if "channel" not in out and _PLACE_KEYS.search(tail) and len(v) <= 80:
            out["channel"] = v
    urls = list(dict.fromkeys(_URL.findall(text or "") + [u for v in wide.values() if isinstance(v, str)
                                                           for u in _URL.findall(v)]))[:3]
    if urls:
        out["urls"] = ", ".join(u[:120] for u in urls)
    if out.get("recipients"):
        out["recipients.count"] = out["recipients"].count(",") + 1
    return out


def path_params(raw_path: str, template: str) -> dict:
    """Name the variable parts of a path after the segment before them: /repos/{name}/{name}/issues/{id} with
    /repos/acme/api/issues/12 -> {path.repo: acme, path.repo_2: api, path.issue: 12}."""
    raw = raw_path.split("?", 1)[0].split("/")
    tpl = template.split("/")
    if len(raw) != len(tpl):
        return {}
    out, last = {}, "path"
    for r, t in zip(raw, tpl):
        if t in ("{id}", "{name}") and r:
            base = re.sub(r"(ies)$", "y", last) if last.endswith("ies") else last.rstrip("s") or last
            k, i = f"path.{base}", 2
            while k in out:
                k, i = f"path.{base}_{i}", i + 1
            v = _wide_str(k + "id", r)
            if v:
                out[k] = v
        elif t:
            last = re.sub(r"[^\w]", "_", t.lower())[:30] or last
    return out


def bound_wide(wide: dict) -> dict:
    """Keep a wide record inside WIDE_MAX_FIELDS keys and WIDE_MAX_BYTES of JSON, core fields first."""
    out = dict(list(wide.items())[:WIDE_MAX_FIELDS])
    while out and len(json.dumps(out, ensure_ascii=False)) > WIDE_MAX_BYTES:
        out.popitem()
    return out


def wide_view(row: dict) -> dict:
    """The whole wide record of a stored event: its columns plus the stored flat fields. This is what trigger
    programs match on and what a decision model may be shown (only the fields a trigger asks for)."""
    w = row.get("wide")
    if isinstance(w, str):
        try:
            w = json.loads(w)
        except Exception:
            w = {}
    out = {"id": row.get("id"), "ts": row.get("ts"), "app": row.get("app"), "kind": row.get("sig"),
           "label": row.get("label"), "klass": row.get("klass"), "direction": row.get("direction")}
    for k in ("who", "place", "text"):
        if row.get(k):
            out[k] = row[k]
    out.update(w or {})
    if not out.get("op") and row.get("sig"):
        m = HTTP_SIG.match(row["sig"])
        op = (m["op"] if m else None) or (row["sig"].split(":", 1)[1] if ":" in row["sig"] and "|" not in row["sig"] else None)
        if op:
            out["op"] = op
    return {k: v for k, v in out.items() if v not in (None, "")}


def field_type(v) -> str:
    return "bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) else "text"


def sse_events(text: str, cap: int = 600):
    """(event name, data) for each complete event in a text/event-stream body."""
    name, data, n = "", [], 0
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if not line:
            if data:
                yield name, "\n".join(data)
                n += 1
                if n >= cap:
                    return
            name, data = "", []
        elif line.startswith("data:"):
            data.append(line[6:] if line[5:6] == " " else line[5:])
        elif line.startswith("event:"):
            name = line[6:].strip()
    if data:
        yield name, "\n".join(data)


def _fragment(obj, depth: int = 0) -> str | None:
    """The piece of text one streamed delta carries, with its spaces (harvest() strips them)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if v and _keyclass(k) & K_STRONG:
                    return v
            elif isinstance(v, (dict, list)) and depth < 6:
                f = _fragment(v, depth + 1)
                if f:
                    return f
    elif isinstance(obj, list):
        for v in obj[:20]:
            if isinstance(v, (dict, list)) and depth < 6:
                f = _fragment(v, depth + 1)
                if f:
                    return f
    return None


WORDY = re.compile(r"[a-z]+(?:[-_][a-z]+)*")        # how an API names a resource or an operation: notes, time-entries


class PathShapes:
    """Learns which path segments are names (a repo, a workspace, a project) rather than operations.

    `PUT /user/starred/luqs1/ambient-ai` and `PUT /user/starred/tauri-apps/tauri` are the same kind of
    thing. Ids are recognised by shape (ID_SEG); names can only be recognised by seeing several. What is
    learned is a template, `/user/starred/{name}/{name}`: it folds the paths that are equal to it
    everywhere else, and no others. A position (or two neighbouring positions) becomes `{name}` once the
    values seen there, with the rest of the path equal, look like an open vocabulary (see _names).
    Operation names (`/api/chat.postMessage`, `/api/users.list`) are a closed vocabulary seen over and
    over, and the last segment of a POST is never generalised, so those stay distinct kinds."""

    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.execute("CREATE TABLE IF NOT EXISTS path_shapes (grp TEXT PRIMARY KEY, mask TEXT)")
            store.db.commit()
        self.masks: dict[str, list[tuple]] = {}       # group -> templates
        old = {}
        for r in store.q("SELECT * FROM path_shapes"):
            try:
                m = json.loads(r["mask"])
            except Exception:
                continue
            if m and all(isinstance(x, list) for x in m):
                self.masks[r["grp"]] = [tuple(x) for x in m]
            elif m:
                old[r["grp"]] = m
        if old:
            self._migrate(old)
        self.seen: dict[str, dict[tuple, int]] = {}

    def _migrate(self, old: dict):
        """Before templates a group held bare positions, applied to every path of that depth. Rebuild the
        templates from the kinds that were folded with them; a group with none starts learning again."""
        for r in self.store.q("SELECT sig, app FROM event_types WHERE sig LIKE '%{name}%'"):
            m = HTTP_SIG.match(r["sig"])
            if not m:
                continue
            segs = tuple(m["path"].split("/"))
            grp = f"{r['app']}|{m['method']} {m['host']}|{len(segs)}|{m['op'] or ''}"
            if grp in old and segs not in self.masks.setdefault(grp, []):
                self.masks[grp].append(segs)
        with self.store.lock:
            for grp in old:
                if self.masks.get(grp):
                    self.store.db.execute("INSERT OR REPLACE INTO path_shapes VALUES (?,?)", (grp, json.dumps(self.masks[grp])))
                else:
                    self.masks.pop(grp, None)
                    self.store.db.execute("DELETE FROM path_shapes WHERE grp=?", (grp,))
            self.store.db.commit()

    @staticmethod
    def _fits(tpl: tuple, segs) -> bool:
        return len(tpl) == len(segs) and all(a == b or (a == "{name}" and b) for a, b in zip(tpl, segs))

    def apply(self, grp: str, segs: list[str]) -> list[str]:
        for tpl in self.masks.get(grp, ()):
            if self._fits(tpl, segs):
                return list(tpl)
        return segs

    @staticmethod
    def _names(same: dict[tuple, int], width: int, eager: bool = False, trailing: bool = False) -> bool:
        """Are these values names (an open vocabulary) rather than a closed set of resources or settings?
        Both look alike early on: a few values, each used once or twice. What tells them apart:
          - spelling: no API calls a resource `luqs1`, `Acme` or `my.repo`; mostly such values is enough;
          - pairs that never repeat a value on either side (owner/repo), with one such spelling among them:
            closed sets repeat (notes/create, notes/delete);
          - sheer number: a dozen plain words in one place is more than any closed set we should fold."""
        k = len(same)
        counts = sorted(same.values())
        cols = [{v[j] for v in same} for j in range(width)]
        namey = sum(1 for v in same if any(not WORDY.fullmatch(x) for x in v))
        # A place a trigger watches (you showed us one example and bound to it): the second example is
        # enough when the values are spelled like names (luqs1, my.repo, Acme), or are owner/repo-style
        # pairs that share nothing with one such spelling among them. Plain words are not enough, even
        # at the end of a path: /api/me/status and /api/me/avatar are two different things to do.
        if eager and k == 2 and min(map(len, cols)) == 2 and namey >= (1 if width == 2 else 2):
            return True
        if k < 3 or counts[k // 2] > 2:               # operations get repeated; a list of names just grows
            return False
        if width == 2 and min(map(len, cols)) < 2:    # a pair must vary in both places
            return False
        if namey * 3 >= k * 2:
            return True
        if width == 2 and k >= 4 and namey and all(len(c) == k for c in cols):
            return True
        return k >= 12 and all(len(c) * 2 >= k for c in cols)

    def observe(self, grp: str, method: str, segs: list[str], eager: bool = False, seeds=()) -> bool:
        """Record one concrete path. True if this taught us a new template. `eager`: a trigger is bound
        to a path in this group, so learn from two examples when their spelling allows it."""
        seen = self.seen.setdefault(grp, {})
        for sd in seeds:                              # paths triggers are bound to: examples already shown
            seen.setdefault(tuple(self.apply(grp, list(sd))) if grp in self.masks else tuple(sd), 1)
        key = tuple(self.apply(grp, segs)) if grp in self.masks else tuple(segs)
        c = seen.get(key)
        if c:
            seen[key] = c + 1
            return False
        if len(seen) >= 300:                          # plenty looked at already: stop learning, stop growing
            return False
        seen[key] = 1
        if len(seen) < (2 if eager else 3):
            return False
        n = len(key)
        allowed = [i for i in range(n) if key[i] and key[i] not in ("{id}", "{name}")
                   and not (method == "POST" and i == n - 1)]
        for width in (1, 2):
            for i in allowed:
                pos = tuple(range(i, i + width))
                if any(q not in allowed for q in pos):
                    continue
                same: dict[tuple, int] = {}
                rest = tuple(x for j, x in enumerate(key) if j not in pos)
                for path, c in seen.items():
                    if len(path) == n and tuple(x for j, x in enumerate(path) if j not in pos) == rest:
                        v = tuple(path[j] for j in pos)
                        if "{id}" not in v and "{name}" not in v and all(v):
                            same[v] = c
                if not self._names(same, width, eager, trailing=pos[-1] == n - 1):
                    continue
                tpl = tuple("{name}" if j in pos else x for j, x in enumerate(key))
                self.masks[grp] = [t for t in self.masks.get(grp, []) if not self._fits(tpl, t)] + [tpl]
                with self.store.lock:
                    self.store.db.execute("INSERT OR REPLACE INTO path_shapes VALUES (?,?)",
                                          (grp, json.dumps(self.masks[grp])))
                    self.store.db.commit()
                merged: dict[tuple, int] = {}
                for path, c in seen.items():
                    g = tuple(self.apply(grp, list(path)))
                    merged[g] = merged.get(g, 0) + c
                self.seen[grp] = merged
                return True
        return False


HTTP_SIG = re.compile(r"^(?P<app>.*?)\|(?P<method>[A-Z]+) (?P<host>[^/ ]+)(?P<path>/[^#+]*)(?:#(?P<op>[^+]*))?(?P<text>\+text)?$")


class Events:
    def __init__(self, store, on_event=None):
        self.store = store
        self.on_event = on_event            # called with the stored event dict (action/incoming/signal)
        with store.lock:
            store.db.executescript(SCHEMA)
            for table, col in (("events", "wide"), ("events", "op"), ("event_types", "schema")):
                if col not in {r[1] for r in store.db.execute(f"PRAGMA table_info({table})")}:
                    store.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            store.db.execute("CREATE INDEX IF NOT EXISTS events_app ON events(app, id)")
            own = tuple(OWN_APPS)             # kinds recorded for ourselves before OWN_APPS existed
            marks = ",".join("?" * len(own))
            store.db.execute(f"DELETE FROM event_types WHERE app IN ({marks})", own)
            store.db.execute(f"DELETE FROM events WHERE app IN ({marks})", own)
            store.db.commit()
        self.lock = threading.Lock()
        self.types: dict[str, dict] = {r["sig"]: r for r in store.q("SELECT * FROM event_types")}
        self.shapes = PathShapes(store)
        self.dirty: set[str] = set()
        # Kinds sorted under older rules (an analytics call filed as an action) are sorted again, so a
        # rule fix reaches what is already in the catalog and not only what happens next.
        for t in self.types.values():
            if t["curated"] or "|LOCAL " in t["sig"] or t["sig"].endswith(("|VISIT",)) or "|PUSH " in t["sig"]:
                continue
            k = self._classify(t, deque())
            if k in ("background", "read") and t["klass"] != k:
                t["klass"] = k
                self.dirty.add(t["sig"])
            lab = nice(t["label"], t["sig"])
            if lab != t["label"]:
                t["label"] = lab
                self.dirty.add(t["sig"])
        # Called with nothing after kinds were merged under a learned path template, so whoever holds
        # signatures (triggers) can move to the merged kind. Moments sets this.
        self.on_refold = None
        # A callable (host, path) -> bool for traffic that must never become an event (model providers'
        # APIs). The addon sets it; it also covers rebuilding the catalog from stored traffic.
        self.skip = None
        # Path groups a trigger is bound to: there, two differently spelled names are enough to learn
        # that a position is a name (you showed us once; the second time must already count).
        # Maps each such group to the exact paths bound there, so the example you showed still counts
        # after a restart.
        self.eager_groups: dict[str, set[tuple]] = {}
        self.rate: dict[str, deque] = {}
        self.recordings: dict[str, dict] = {}
        self.muted_apps: set[str] = set()     # apps the user paused: counted, never stored
        # Kinds that pruning must leave alone (the ones triggers are bound to): a callable sig -> bool, or
        # an iterable of signatures / fnmatch patterns. Moments sets this.
        self.keep = None
        self._clock: float | None = None      # set while replaying recorded flows
        self.last_flush = time.time()
        self.last_prune = time.time()         # first prune ten minutes in, not during start-up or a backfill

    def _now(self) -> float:
        return self._clock or time.time()

    def backfill(self, limit: int = 30000) -> int:
        """Build the catalog from traffic already recorded (first run after an upgrade).
        Triggers don't fire for replayed events."""
        cb, self.on_event = self.on_event, None
        rows = self.store.q(
            "SELECT ts, kind, app, host, method, path, req_ctype, req_body, req_headers, resp_body, status "
            "FROM flows WHERE decrypted=1 AND kind IN ('http','ws') ORDER BY id DESC LIMIT ?", (limit,))
        try:
            for r in reversed(rows):
                self._clock = r["ts"] or time.time()
                if r["kind"] == "http":
                    try:
                        hdr = {k.lower(): v for k, v in json.loads(r["req_headers"] or "[]")}
                    except Exception:
                        hdr = {}
                    self.http(r["app"], r["host"] or "", r["method"] or "GET", r["path"] or "/", r["req_ctype"] or "",
                              r["req_body"], hdr, r["resp_body"] or "", r["status"])
                else:
                    up = (r["method"] or "").endswith("↑")
                    self.ws(r["app"], r["host"] or "", r["path"] or "", r["req_body"] if up else r["resp_body"], up)
        finally:
            self._clock = None
            self.on_event = cb
        self.flush()
        return len(rows)

    # ---------- catalog bookkeeping ----------
    def _touch(self, sig: str, *, app, via, host, label, direction, has_text, example, fields,
               curated=False, klass=None, source="network") -> dict:
        now = self._now()
        t = self.types.get(sig)
        if t is None:
            t = {"sig": sig, "app": app, "via": via, "host": host, "label": label, "direction": direction,
                 "klass": klass or "signal", "curated": 1 if curated else 0, "source": source, "n": 0, "n_text": 0,
                 "first_ts": now, "last_ts": now, "example": None, "fields": None, "muted": 0, "schema": None}
            self.types[sig] = t
        t["n"] += 1
        t["last_ts"] = now
        if not t["curated"]:
            t["label"] = label                # follows the path as name positions are learned; classified below
        if has_text:
            t["n_text"] += 1
            if not (t.get("muted") or t["app"] in self.muted_apps):     # paused: counted, but no content is kept
                t["example"] = example[:300]
                if fields:
                    t["fields"] = json.dumps(fields, ensure_ascii=False)[:1500]
        dq = self.rate.setdefault(sig, deque(maxlen=40))
        dq.append(now)
        if not t["curated"]:
            t["klass"] = klass or self._classify(t, dq)
            t["label"] = nice(label, sig)     # classified by the API's own words, shown in plainer ones
        elif klass:
            t["klass"] = klass                # its source decides, and may change its mind (a local table
                                              # whose first row had no text is not a signal forever)
        self.dirty.add(sig)
        for rec in self.recordings.values():
            if self._clock is None and now <= rec["until"]:
                rec["seen"][sig] = rec["seen"].get(sig, 0) + 1
        if time.time() - self.last_flush > 3:
            self.flush()
        return t

    @staticmethod
    def _classify(t: dict, dq: deque) -> str:
        sig = t["sig"]
        tail = sig.split("|", 1)[-1]
        if tail.startswith(("GET ", "HEAD ", "OPTIONS ")):
            return "read"
        tel = t.get("_tel")
        if tel is None:                       # a signature never changes: work this out once per kind
            hp = tail.partition(" ")[2]
            cut = min((i for i in (hp.find("/"), hp.find("#")) if i >= 0), default=len(hp))
            rest = _spaced(hp[cut:])
            tel = t["_tel"] = bool(TELEMETRY_HOST.search(hp[:cut]) or TELEMETRY.search(rest)
                                   or CHATTER.search(rest[rest.rfind("/") + 1:]))
        if tel or (t["direction"] == "out" and t["label"] in ANALYTICS_LABELS):
            return "background"
        # A request that carries a human sentence is only a read if its name clearly says so ("get…",
        # "list…", "autocomplete"): vague names like "sync" or "batch" with text are edits, and a noun
        # (POST /comments/{id}/replies) with text creates one.
        strong = READ_VERB if sig.endswith("+text") else STRONG_READ
        if t["direction"] == "out" and READ_OP.search(t["label"]):
            if not (sig.endswith("+text") and not strong.search(t["label"])):
                return "read"
        if t["direction"] == "out" and strong.search(t["label"]) and not WRITE_WORD.search(t["label"]):
            return "read"
        n, share = t["n"], (t["n_text"] / t["n"] if t["n"] else 0)
        fast = len(dq) >= 12 and (dq[-1] - dq[0]) / (len(dq) - 1) < 12     # steady machine chatter
        if fast and share < 0.5:
            return "background"
        if t["direction"] == "in":
            if share >= 0.3:
                return "incoming"
            return "background" if n >= 15 else "signal"
        return "action" if share >= 0.3 else "signal"

    def flush(self):
        with self.lock:
            dirty, self.dirty = self.dirty, set()
            self.last_flush = time.time()
        if not dirty:
            return
        rows = [self.types[s] for s in dirty if s in self.types]
        with self.store.lock:
            self.store.db.executemany(
                "INSERT INTO event_types (sig, app, via, host, label, direction, klass, curated, source, n, n_text, "
                "first_ts, last_ts, example, fields, muted, schema) VALUES (:sig,:app,:via,:host,:label,:direction,:klass,"
                ":curated,:source,:n,:n_text,:first_ts,:last_ts,:example,:fields,:muted,:schema) ON CONFLICT(sig) DO UPDATE SET "
                "klass=excluded.klass, n=excluded.n, n_text=excluded.n_text, last_ts=excluded.last_ts, "
                "example=excluded.example, fields=excluded.fields, label=excluded.label, app=excluded.app, "
                "schema=COALESCE(excluded.schema, schema), first_ts=MIN(first_ts, excluded.first_ts)",
                [{**r, "schema": r.get("schema")} for r in rows])
            self.store.db.commit()
        if time.time() - self.last_prune > 600:
            self.last_prune = time.time()
            self.prune()

    STALE_AFTER = 14 * 86400
    MAX_TYPES = 5000

    def prune(self) -> int:
        """Keep the catalog bounded: forget kinds not seen for two weeks (old signatures left behind by a
        rule change, one-off endpoints), and beyond MAX_TYPES the quietest and oldest first. Never the
        curated kinds, the ones the user muted (the mute must outlive them) or the ones `keep` names;
        a forgotten kind simply comes back the next time it happens. Also trims the events table."""
        keep = self.keep
        if keep is not None and not callable(keep):
            pats = list(keep)
            keep = lambda sig: any(fnmatchcase(sig, p) for p in pats)      # noqa: E731
        now = self._now()
        free = [t for t in list(self.types.values()) if not (t["curated"] or t.get("muted") or (keep and keep(t["sig"])))]
        # Things you do rarely (a monthly report, a yearly renewal) should still be there to trigger on:
        # actions and incoming kinds are kept for 90 days, quiet kinds for two weeks.
        drop = [t["sig"] for t in free if (t["last_ts"] or 0) < now - self.STALE_AFTER * (
            6.5 if t["klass"] in ("action", "incoming") else 1)]
        over = len(self.types) - len(drop) - self.MAX_TYPES
        if over > 0:
            gone = set(drop)
            rest = sorted((t for t in free if t["sig"] not in gone),
                          key=lambda t: (t["klass"] in ("action", "incoming"), t["last_ts"] or 0))
            drop += [t["sig"] for t in rest[:over]]
        for sig in drop:
            self.types.pop(sig, None)
            self.rate.pop(sig, None)
            self.dirty.discard(sig)
        with self.store.lock:
            self.store.db.executemany("DELETE FROM event_types WHERE sig=?", [(s,) for s in drop])
            self.store.db.execute("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 20000")
            self.store.db.commit()
        return len(drop)

    def _store(self, t: dict, *, text, fields, who=None, place=None, key=None, context=None, sig0=None,
               wide=None) -> dict | None:
        """Record one event. `text` and `fields` arrive with secrets already masked; `wide` is the flat record
        built by the source (masked as it was built)."""
        if t["klass"] not in ("action", "incoming", "signal") or t.get("muted") or t["app"] in self.muted_apps:
            return None
        text = (text or "")[:4000]
        fj = json.dumps(fields, ensure_ascii=False)[:3000] if fields else None
        now = self._now()
        w = self._wide(t, text, fields, wide or {})
        wj = json.dumps(w, ensure_ascii=False) if w else None
        with self.store.lock:
            cur = self.store.db.execute(
                "INSERT OR IGNORE INTO events (ts, sig, app, direction, who, place, text, fields, key, wide, op) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (now, t["sig"], t["app"], t["direction"], who, place, text, fj, key,
                                                  wj, w.get("op")))
            self.store.db.commit()
            eid = cur.lastrowid if cur.rowcount else None
        if not eid:
            return None
        self._learn_schema(t, w, text, who, place)
        ev = {"id": eid, "ts": now, "sig": t["sig"], "app": t["app"], "via": t.get("via"), "label": t["label"],
              "direction": t["direction"], "klass": t["klass"], "who": who, "place": place, "text": text,
              "fields": fields or {}, "context": context or {}}
        ev["wide"] = wide_view({**ev, "wide": w})
        if sig0:
            ev["sig0"] = sig0          # the exact path before name positions were generalised
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception as e:
                print(f"ambient events: on_event {e!r}", flush=True)
        return ev

    @staticmethod
    def _wide(t: dict, text: str, fields: dict | None, wide: dict) -> dict:
        """The stored part of the wide record (the row's own columns are added back by wide_view)."""
        w = {"via": t.get("via"), "source": t.get("source") or "network"}
        w.update({k: v for k, v in wide.items() if not k.startswith(("fields.", "resp.")) and v not in (None, "")})
        body = {k: v for k, v in wide.items() if k.startswith(("fields.", "resp."))}
        if not body and fields:                       # a source that only had harvest()'s few fields
            body = flatten_wide(fields)
        w.update(wide_canon(body, text))
        if text:
            w["text_len"] = len(text)
        w.update(body)
        return bound_wide({k: v for k, v in w.items() if v not in (None, "")})

    def _learn_schema(self, t: dict, w: dict, text: str, who, place):
        """Which wide fields this kind carries, with a type, a count and the latest short example."""
        sch = t.get("_schema")
        if sch is None:
            try:
                sch = json.loads(t.get("schema") or "{}")
            except Exception:
                sch = {}
            t["_schema"] = sch
        seen = dict(w)
        if text:
            seen["text"] = text
        for k, v in (("who", who), ("place", place)):
            if v:
                seen[k] = v
        for k, v in seen.items():
            if k not in sch and len(sch) >= SCHEMA_MAX_FIELDS:
                continue
            e = sch.get(k) or [field_type(v), 0, None]
            e[1] += 1
            e[2] = v if isinstance(v, (int, float, bool)) else str(v)[:40]
            sch[k] = e
        t["schema"] = json.dumps(sch, ensure_ascii=False)
        self.dirty.add(t["sig"])

    def schema(self, sig: str) -> dict:
        t = self.types.get(sig) or {}
        try:
            return t.get("_schema") or json.loads(t.get("schema") or "{}")
        except Exception:
            return {}

    # ---------- generic sources ----------
    def http(self, proc_app: str, host: str, method: str, path: str, req_ctype: str, req_raw, headers=None,
             resp_text: str = "", status: int | None = None):
        try:
            self._http(proc_app, host, method, path, req_ctype, req_raw, headers or {}, resp_text, status)
        except Exception as e:
            print(f"ambient events: http {e!r}", flush=True)

    def _http(self, proc_app, host, method, path, req_ctype, req_raw, headers, resp_text, status):
        if proc_app in OWN_APPS or host in LOOPBACK or (self.skip and self.skip(host, path)):
            return                          # never report on ourselves or on loopback traffic
        if sensitive(host, path, req_raw):
            return
        if resp_text and "text/event-stream" in headers.get("accept", "") and re.match(r"\s*(data|event|id|retry)?:", resp_text):
            self._sse(proc_app, host, path, resp_text)      # what the server pushed; the request itself goes on below
        app, via = self._who(proc_app, host)
        method = method.upper()
        ptempl = path_template(path)
        query = parse_qs(urlsplit(path).query) if "?" in path else {}
        op = next((query[k][0] for k in OP_KEYS if k in query and _op_ok(k, query[k][0].replace(",", "."))), None)
        text, fields, body_wide = "", {}, {}
        navigate = (method == "GET" and headers.get("sec-fetch-mode") == "navigate"
                    and headers.get("sec-fetch-dest", "document") == "document" and (status or 200) < 400)
        if method not in ("GET", "HEAD", "OPTIONS"):
            obj = decode_body(req_raw, req_ctype)
            if obj is not None:
                text, fields, body_op = harvest(obj)
                op = op or body_op
                body_wide = flatten_wide(obj)
            resp_obj = _json_obj(resp_text)
            if resp_obj is not None:
                body_wide.update(flatten_wide(resp_obj, "resp", limit=max(6, WIDE_BODY_FIELDS - len(body_wide))))
        elif navigate:
            m = re.search(r"<title[^>]*>(.*?)</title>", resp_text[:200000] or "", re.S | re.I)
            title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
            text = html.unescape(title)[:200]
            fields = {"url": f"https://{host}{path.split('?')[0]}"[:300]}
        if method == "POST" and path.split("?")[0].endswith("/git-receive-pack"):
            repo = path.split("?")[0][:-len("/git-receive-pack")].strip("/").removesuffix(".git")
            self._emit_generic(f"{app}|PUSH {host_template(host)}", app, via, host, "git push", "out",
                               f"pushed commits to {repo} on {host}", {"repo": repo}, klass="action",
                               wide={"transport": "http", "host": host, "method": method, "op": "push",
                                     "path.repo": repo, "status": status})
            return
        if navigate:
            if not text:
                return
            self._emit_generic(f"{app}|VISIT", app, via, host, "open a page", "out", text, fields, klass="action",
                               wide={"transport": "http", "host": host, "method": method, "op": "visit",
                                     "path": path.split("?")[0][:WIDE_STR], "title": text, "status": status})
            return
        last = ptempl.rstrip("/").split("/")[-1]
        if not GENERIC_PATH.match(last) and len(words(last)) > 2:
            op = None                         # the path already names the operation; keep the signature stable
        # One endpoint often carries both background sync and real edits (Google's sync.sync, Notion's
        # saveTransactions). Requests with human text in them are their own kind, so they can be told apart.
        segs = ptempl.split("/")
        grp = f"{app}|{method} {host_template(host)}|{len(segs)}|{op or ''}"
        seeds = self.eager_groups.get(grp)
        if self.shapes.observe(grp, method, segs, eager=seeds is not None, seeds=seeds or ()):
            self._refold(app)                 # kinds that turned out to be one kind become one
        sig0 = (f"{app}|{method} {host_template(host)}{ptempl}" + (f"#{op}" if op else "")
                + ("+text" if text else ""))
        ptempl = "/".join(self.shapes.apply(grp, segs))
        sig = (f"{app}|{method} {host_template(host)}{ptempl}" + (f"#{op}" if op else "")
               + ("+text" if text else ""))
        label = words(op) if op and len(words(op)) > 2 else words(" ".join(ptempl.split("/")[-2:]))
        if not op and method != "POST":       # REST: the path is the meaning ("update user starred …")
            label = words(" ".join(ptempl.split("/")[-4:])) or label
        label = label or words(" ".join(ptempl.split("/")[-4:])) or "request"
        if not any(len(w) > 2 for w in label.split()):      # a terse path (Gmail's /sync/u/0/i/s -> "s"):
            real = [w for w in words(ptempl.replace("/", " ")).split() if len(w) > 2]      # use what words it has,
            label = " ".join(real[-2:]) or ("send" if method == "POST" and text else "")    # or just the gesture
        verb = {"DELETE": "delete ", "PUT": "update ", "PATCH": "update "}.get(method, "")
        wide = {"transport": "http", "host": host, "method": method, "path": path.split("?")[0][:WIDE_STR],
                "path_template": ptempl[:WIDE_STR], "op": op, "status": status,
                **path_params(path, ptempl)}
        for k, v in list(query.items())[:8]:
            if v and not _keyclass(k) & K_CRED and not credential_param(k) and k not in OP_KEYS:
                qv = _wide_str(f"query.{k}", v[0])
                if qv:
                    wide[f"query.{_wide_key(k)}"] = qv
        wide.update(body_wide)
        self._emit_generic(sig, app, via, host, (verb + label).strip() or method.lower(), "out", text, fields,
                           sig0=sig0 if sig0 != sig else None, wide=wide)

    def _refold(self, app: str):
        """After learning a name position, merge the kinds it unifies (counts add up, newest example wins)."""
        moved = False
        for sig, t in list(self.types.items()):
            m = HTTP_SIG.match(sig)
            if t["app"] != app or t.get("curated") or not m:
                continue
            segs = m["path"].split("/")
            grp = f"{app}|{m['method']} {m['host']}|{len(segs)}|{m['op'] or ''}"
            new = (f"{app}|{m['method']} {m['host']}" + "/".join(self.shapes.apply(grp, segs))
                   + (f"#{m['op']}" if m["op"] else "") + (m["text"] or ""))
            if new == sig:
                continue
            into = self.types.get(new)
            if into is None:
                into = self.types[new] = {**t, "sig": new, "label": nice(words(t["label"]), new)}
                into.pop("_tel", None)
            else:
                into["n"] += t["n"]; into["n_text"] += t["n_text"]
                into["first_ts"] = min(into["first_ts"] or t["first_ts"], t["first_ts"] or into["first_ts"])
                if (t["last_ts"] or 0) >= (into["last_ts"] or 0):
                    into["last_ts"], into["example"], into["fields"] = t["last_ts"], t["example"], t["fields"]
            del self.types[sig]
            self.rate.pop(sig, None)
            self.dirty.discard(sig)
            self.dirty.add(new)
            with self.store.lock:
                self.store.db.execute("DELETE FROM event_types WHERE sig=?", (sig,))
                self.store.db.execute("UPDATE events SET sig=? WHERE sig=?", (new, sig))     # its events move with it
                self.store.db.commit()
            moved = True
        if moved and self.on_refold:
            try:
                self.on_refold()
            except Exception as e:
                print(f"ambient events: on_refold {e!r}", flush=True)

    @staticmethod
    def group_of(sig: str) -> str | None:
        """The path group (app, method, host, depth, operation) a network signature belongs to."""
        m = HTTP_SIG.match(sig)
        if not m:
            return None
        return f"{m['app']}|{m['method']} {m['host']}|{len(m['path'].split('/'))}|{m['op'] or ''}"

    def folded(self, sig: str) -> str:
        """This signature under the path templates learned so far (unchanged if none applies)."""
        m = HTTP_SIG.match(sig)
        if not m:
            return sig
        segs = m["path"].split("/")
        return (f"{m['app']}|{m['method']} {m['host']}" + "/".join(self.shapes.apply(self.group_of(sig), segs))
                + (f"#{m['op']}" if m["op"] else "") + (m["text"] or ""))

    def forget(self, match) -> int:
        """Drop every kind (and its stored events) for which match(kind) is true. Returns how many."""
        gone = [s for s, t in self.types.items() if match(t)]
        for s in gone:
            del self.types[s]
            self.rate.pop(s, None)
            self.dirty.discard(s)
        if gone:
            with self.store.lock:
                self.store.db.executemany("DELETE FROM event_types WHERE sig=?", [(s,) for s in gone])
                self.store.db.executemany("DELETE FROM events WHERE sig=?", [(s,) for s in gone])
                self.store.db.commit()
        return len(gone)

    @staticmethod
    def _who(proc_app: str, host: str) -> tuple[str, str | None]:
        """(app, via): a browser's traffic belongs to the site, anything else to the process."""
        if proc_app in BROWSERS:
            return site_name(host), proc_app
        return PROC_NAMES.get(proc_app, proc_app), None

    def ws(self, proc_app: str, host: str, path: str, data, from_client: bool, obj=None):
        """One WebSocket frame. `obj` is the decoded payload when the caller already decoded it."""
        try:
            if proc_app in OWN_APPS or host in LOOPBACK or (self.skip and self.skip(host, path)):
                return                      # never report on ourselves or on loopback traffic
            app, via = self._who(proc_app, host)
            if isinstance(data, str) and data.startswith("[binary "):
                data = b""
            if obj is None:
                obj = decode_body(data, "")
            if obj is None:
                op, text, fields = ("binary" if isinstance(data, bytes) else "text"), "", {}
            else:
                text, fields, op = harvest(obj)
                op = op or "message"
            self.structured(app, host, op, text, fields, incoming=not from_client, via=via, raw=obj)
        except Exception as e:
            print(f"ambient events: ws {e!r}", flush=True)

    def structured(self, app: str, host: str, op: str, text: str, fields: dict, incoming: bool, via=None, obj=None,
                   raw=None):
        """A decoded message with a known operation name (WebSocket frame, Discord dispatch…)."""
        if obj is not None:
            text, fields, _ = harvest(obj)
            raw = obj
        arrow = "WS↓" if incoming else "WS↑"
        sig = f"{app}|{arrow} {host_template(host)}#{op}"
        wide = {"transport": "ws", "host": host, "op": op}
        if raw is not None:
            wide.update(flatten_wide(raw))
        self._emit_generic(sig, app, via, host, words(op) or "message", "in" if incoming else "out", text, fields,
                           wide=wide)

    def sse(self, proc_app: str, host: str, path: str, data):
        """Server-sent events: the body of a `text/event-stream` response (all of it, or any run of whole
        events). Each event becomes an incoming kind `app|SSE↓ host/path#event-name`. A run of deltas (an
        LLM answer arriving token by token) is put back together and counted as one message."""
        try:
            self._sse(proc_app, host, path, data)
        except Exception as e:
            print(f"ambient events: sse {e!r}", flush=True)

    def _sse(self, proc_app, host, path, data):
        if not data or proc_app in OWN_APPS or host in LOOPBACK or sensitive(host, path) or (
                self.skip and self.skip(host, path)):
            return
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        app, via = self._who(proc_app, host)
        groups: dict[str, list] = {}
        for name, payload in sse_events(data[:512_000]):
            obj = _maybe_json(payload)
            if obj is None:
                if not looks_like_prose(payload):
                    continue                        # "[DONE]", keep-alives, counters
                obj = {"text": payload}
            op = name if _op_ok("", name) and len(name) <= 60 else None
            if op is None and isinstance(obj, dict):
                op = next((obj[k] for k in OP_KEYS if _op_ok(k, obj.get(k))), None)
            groups.setdefault(op or "message", []).append(obj)
        base = f"{app}|SSE↓ {host_template(host)}{path_template(path)}#"
        for op, objs in groups.items():
            label = words(op) or "message"
            frags = [_fragment(o) for o in objs] if len(objs) >= 4 else []
            if len(objs) >= 20 or sum(1 for f in frags if f and (f[0].isspace() or f[-1].isspace())) * 4 >= max(len(frags), 1):
                # Pieces of one message: many events of one name, or pieces that begin or end mid-sentence.
                text = "".join(f for f in (frags or map(_fragment, objs)) if f).strip()[:4000]
                self._emit_generic(base + op, app, via, host, label, "in", text if looks_like_prose(text) else "", {},
                                   wide={"transport": "sse", "host": host, "op": op, "fields.events.count": len(objs)})
                continue
            for o in objs:
                text, fields, _ = harvest(o, limit=2000)
                self._emit_generic(base + op, app, via, host, label, "in", text, fields,
                                   wide={"transport": "sse", "host": host, "op": op, **flatten_wide(o)})

    def _emit_generic(self, sig, app, via, host, label, direction, text, fields, klass=None, sig0=None, wide=None):
        has_text = bool(text)
        if has_text:
            text = mask_secrets(text)
        if fields:
            fields = mask_fields(fields)
        t = self._touch(sig, app=app, via=via, host=host, label=label, direction=direction, has_text=has_text,
                        example=text, fields=fields, klass=klass)
        if t["klass"] == "incoming" and not has_text:
            return
        self._store(t, text=text, fields=fields, sig0=sig0, wide=wide)

    # ---------- curated sources (Slack, Discord, WhatsApp, local stores…) ----------
    def emit(self, *, app: str, sig: str, label: str, direction: str, klass: str, text: str = "", who=None,
             place=None, key=None, fields=None, context=None, source="network") -> dict | None:
        text, fields = mask_secrets(text or ""), mask_fields(fields)
        t = self._touch(sig, app=app, via=None, host="", label=label, direction=direction, has_text=bool(text),
                        example=text, fields=fields, curated=True, klass=klass, source=source)
        wide = {"transport": "local" if source == "local" else "curated"}
        if fields:
            wide.update(flatten_wide(fields))
        for k in ("channel", "thread", "chat", "title", "subject", "to", "attendees"):
            v = (context or {}).get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                vv = _wide_str(f"context.{k}", str(v)) if isinstance(v, str) else v
                if vv is not None:
                    wide[f"fields.context.{k}"] = vv
        return self._store(t, text=text, fields=fields, who=who, place=place, key=key, context=context, wide=wide)

    # ---------- reading ----------
    def catalog(self, include_quiet: bool = False) -> list[dict]:
        self.flush()
        apps: dict[str, dict] = {}
        for t in self.types.values():
            a = apps.setdefault(t["app"], {"app": t["app"], "via": set(), "kinds": [], "quiet": 0, "last_ts": 0,
                                           "n": 0, "sources": set()})
            a["last_ts"] = max(a["last_ts"], t["last_ts"] or 0)
            if t.get("via"):
                a["via"].add(t["via"])
            a["sources"].add(t.get("source") or "network")
            if t["klass"] in ("action", "incoming") or (include_quiet and t["klass"] == "signal"):
                a["kinds"].append({k: t[k] for k in ("sig", "label", "direction", "klass", "n", "n_text", "last_ts",
                                                    "example", "curated", "muted", "source")})
                a["n"] += t["n"]
            else:
                a["quiet"] += 1
        out = []
        for a in apps.values():
            if not a["kinds"] and not include_quiet:
                continue
            a["via"], a["sources"] = sorted(a["via"]), sorted(a["sources"])
            a["kinds"].sort(key=lambda k: (-(k["curated"] or 0), -(k["last_ts"] or 0)))
            out.append(a)
        out.sort(key=lambda a: -a["last_ts"])
        return out

    def recent(self, sig: str | None = None, sigs: list[str] | None = None, limit: int = 50, after: int = 0) -> list[dict]:
        where, args = ["id > ?"], [after]
        if sig:
            where.append("sig = ?"); args.append(sig)
        rows = self.store.q(f"SELECT * FROM events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                            (*args, limit if not sigs else 3000))
        if sigs:
            from fnmatch import fnmatchcase
            rows = [r for r in rows if any(fnmatchcase(r["sig"], p) for p in sigs)][:limit]
        for r in rows:
            r["fields"] = json.loads(r["fields"]) if r["fields"] else {}
            t = self.types.get(r["sig"]) or {}
            r["label"], r["klass"] = t.get("label", ""), t.get("klass", "")
            r["wide"] = wide_view(r)
            r.pop("op", None)
        return rows

    def scan(self, match, limit: int = 60, window: int = 3000) -> list[dict]:
        """The newest stored events (of the last `window`) whose wide record satisfies match(wide)."""
        out = []
        for r in self.recent(limit=window):
            if match(r["wide"]):
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def schemas(self, limit: int = 300) -> list[dict]:
        """The catalog as wide-event schemas: per kind, which fields it carries (type, how often, an example)."""
        self.flush()
        out = []
        ts = [t for t in self.types.values() if t["klass"] in ("action", "incoming", "signal")]
        ts.sort(key=lambda t: -(t["last_ts"] or 0))
        for t in ts[:limit]:
            out.append({"sig": t["sig"], "app": t["app"], "via": t.get("via"), "label": t["label"],
                        "direction": t["direction"], "klass": t["klass"], "n": t["n"], "last_ts": t["last_ts"],
                        "source": t.get("source") or "network",
                        "fields": {k: {"type": e[0], "n": e[1], "example": e[2]} for k, e in self.schema(t["sig"]).items()}})
        return out

    def type(self, sig: str) -> dict | None:
        return self.types.get(sig)

    def mute(self, sig: str, muted: bool):
        t = self.types.get(sig)
        if t:
            t["muted"] = 1 if muted else 0
            with self.store.lock:
                self.store.db.execute("UPDATE event_types SET muted=? WHERE sig=?", (t["muted"], sig))
                self.store.db.commit()

    # ---------- "show me once" ----------
    def record_start(self, seconds: float = 90) -> str:
        import secrets
        token = secrets.token_urlsafe(8)
        self.recordings[token] = {"start": time.time(), "until": time.time() + min(seconds, 300), "seen": {}}
        for k in [k for k, r in self.recordings.items() if time.time() - r["start"] > 900]:
            self.recordings.pop(k, None)
        return token

    def record_read(self, token: str, stop: bool = False) -> dict:
        rec = self.recordings.get(token)
        if not rec:
            return {"error": "no such recording"}
        if stop:
            rec["until"] = time.time()
        out = []
        for sig, n in rec["seen"].items():
            t = self.types.get(sig)
            if not t or t["klass"] in ("background", "read"):
                continue
            # Things that were already firing steadily before you pressed record are probably not it.
            older = t["n"] - n
            span_h = max((rec["start"] - (t["first_ts"] or rec["start"])) / 3600, 1 / 60)
            usual = older / span_h * ((time.time() - rec["start"]) / 3600)
            out.append({"sig": sig, "app": t["app"], "label": t["label"], "direction": t["direction"],
                        "klass": t["klass"], "n": n, "example": t["example"], "new": older == 0,
                        "score": n / (usual + 0.5) + (3 if older == 0 else 0) + (2 if t["klass"] == "action" else 0)})
        out.sort(key=lambda r: -r["score"])
        return {"token": token, "active": time.time() < rec["until"], "left": max(0, int(rec["until"] - time.time())),
                "kinds": out[:12]}
