"""Curated recognizers: apps where we know more than the generic event layer can infer.

They read the same traffic and emit richer events (who wrote, where, DM vs mention vs sent)
under stable signatures like `slack:dm`, `discord:sent`, `granola:meeting`. Everything else an
app does still shows up through the generic layer in events.py, so these are an upgrade for
popular apps, not a requirement for an app to work.
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs

WATCHABLE = {"Slack": "DMs and mentions, and what you send",
             "Discord": "DMs and mentions, and what you send",
             "Granola": "meetings when they end",
             "WhatsApp": "messages, read from its local database"}
CHAT_LABELS = {"dm": "someone DMs me", "mention": "someone @mentions me", "sent": "I send a message",
               "group": "someone writes in a group chat"}
SLACK_ID = re.compile(r"^[UWBCGD][A-Z0-9]{6,}$")
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
CHANNEL_REF = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
LINK = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")


def form_fields(body: str) -> dict:
    if not body:
        return {}
    if body.lstrip().startswith("{"):
        try:
            return json.loads(body)
        except Exception:
            return {}
    out = {}
    if "Content-Disposition" in body:
        for part in body.split('Content-Disposition: form-data; name="')[1:]:
            name, _, rest = part.partition('"')
            out[name] = rest.strip().split("\r\n--")[0].split("\n--")[0].strip()
        return out
    return {k: v[0] for k, v in parse_qs(body).items()}


def blocks_text(blocks) -> str:
    """Plain text of Slack rich_text blocks (what the desktop client sends)."""
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except Exception:
            return ""
    out: list[str] = []

    def walk(x):
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            t = x.get("type")
            if t == "text":
                out.append(x.get("text", ""))
            elif t == "user":
                out.append(f"<@{x.get('user_id')}>")
            elif t == "link":
                out.append(x.get("text") or x.get("url", ""))
            elif t == "emoji":
                out.append(f":{x.get('name')}:")
            walk(x.get("elements"))
    walk(blocks)
    return "".join(out)



class Recognizers:
    def __init__(self, store, events, setting, set_setting, mark_replied):
        self.store, self.events = store, events
        self.setting, self.set_setting, self.mark_replied = setting, set_setting, mark_replied
        self.names: dict[tuple[str, str], str] = {}
        self.im_user: dict[str, str] = {}      # DM channel -> other user
        for r in store.q("SELECT app, id, name, extra FROM names"):
            self.names[(r["app"], r["id"])] = r["name"]
            if r["extra"]:
                self.im_user[r["id"]] = r["extra"]
        self.slack_self: set[str] = set(json.loads(setting("slack_self") or "[]"))
        self.discord_me = setting("discord_me") or ""
        self.meetings: dict[str, list[str]] = {}   # socket id -> transcript turns

    def watching(self, app: str) -> bool:
        return self.setting(f"watch:{app}", "1") == "1"

    def chat(self, app: str, kind: str, *, who, place, text, key, context):
        """One chat message, in the shared shape every chat app uses."""
        return self.events.emit(app=app, sig=f"{app.lower()}:{kind}", label=CHAT_LABELS[kind],
                                direction="out" if kind == "sent" else "in",
                                klass="action" if kind == "sent" else "incoming",
                                who=who, place=place, text=text, key=key, context=context)

    # ---------- names ----------
    def _learn(self, app: str, id_: str, name: str, extra: str | None = None):
        if not name or self.names.get((app, id_)) == name and (extra is None or self.im_user.get(id_) == extra):
            return
        self.names[(app, id_)] = name
        if extra:
            self.im_user[id_] = extra
        with self.store.lock:
            self.store.db.execute("INSERT OR REPLACE INTO names VALUES (?,?,?,?)", (app, id_, name, extra))
            self.store.db.commit()

    def _slack_names(self, obj, depth=0):
        if depth > 8:
            return
        if isinstance(obj, list):
            for x in obj[:2000]:
                self._slack_names(x, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        id_ = obj.get("id")
        if isinstance(id_, str) and SLACK_ID.match(id_):
            prof = obj.get("profile") if isinstance(obj.get("profile"), dict) else {}
            if id_[0] in "UW" and (obj.get("real_name") or prof):
                name = prof.get("display_name") or obj.get("real_name") or prof.get("real_name") or obj.get("name")
                self._learn("Slack", id_, name)
            elif id_[0] in "CGD":
                if obj.get("is_im") and obj.get("user"):
                    self._learn("Slack", id_, "DM", obj["user"])
                elif obj.get("name"):
                    self._learn("Slack", id_, "#" + obj["name"])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                self._slack_names(v, depth + 1)

    def slack_name(self, uid: str | None) -> str:
        return self.names.get(("Slack", uid or ""), uid or "someone")

    def slack_place(self, ch: str) -> str:
        if ch.startswith("D"):
            other = self.im_user.get(ch)
            return f"DM with {self.slack_name(other)}" if other else "DM"
        return self.names.get(("Slack", ch), ch)

    def slack_text(self, text: str) -> str:
        text = MENTION.sub(lambda m: "@" + self.slack_name(m.group(1)), text or "")
        text = CHANNEL_REF.sub(lambda m: "#" + (m.group(2) or self.names.get(("Slack", m.group(1)), m.group(1)).lstrip("#")), text)
        text = LINK.sub(lambda m: m.group(2) or m.group(1), text)
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

    # ---------- feed from the addon ----------
    def on_http(self, app: str, host: str, path: str, req_text: str, resp_text: str):
        try:
            if app == "Slack" or host.endswith("slack.com"):
                self._slack_http(path or "", req_text or "", resp_text or "")
        except Exception as e:  # recognizers must never break recording
            print(f"ambient recognizers: {e!r}", flush=True)

    def on_ws(self, app: str, host: str, sock_id: str, text: str | None, obj, from_client: bool):
        """text for text frames; obj when the frame was binary and wsdecode could read it."""
        try:
            if from_client:
                return
            if host == "wss-primary.slack.com" and text:
                self._slack_event(text)
            elif host == "streaming.us.assemblyai.com" and app == "Granola" and text:
                self._granola_turn(sock_id, text)
            elif host.endswith("discord.gg") and host.startswith("gateway"):
                msg = obj
                if msg is None and text and text.startswith("{"):
                    msg = json.loads(text)
                if isinstance(msg, dict) and msg.get("op") == 0:
                    self._discord_dispatch(msg.get("t"), msg.get("d") or {})
        except Exception as e:
            print(f"ambient recognizers: {e!r}", flush=True)

    def on_ws_end(self, host: str, sock_id: str):
        if host == "streaming.us.assemblyai.com":
            turns = self.meetings.pop(sock_id, [])
            if len(turns) >= 3 and self.watching("Granola"):
                transcript = "\n".join(turns)
                self.events.emit(app="Granola", sig="granola:meeting", label="a meeting ends", direction="in",
                                 klass="incoming", place="Granola", text=turns[-1][:280],
                                 key=f"granola:{sock_id}",
                                 context={"transcript": transcript[-20000:], "turns": len(turns)})

    # ---------- Slack ----------
    def _slack_http(self, path: str, req: str, resp: str):
        if resp.startswith("{"):
            try:
                data = json.loads(resp)
            except Exception:
                data = None
            if isinstance(data, dict):
                me = data.get("self")
                if isinstance(me, dict) and isinstance(me.get("id"), str) and me["id"] not in self.slack_self:
                    self.slack_self.add(me["id"])
                    self.set_setting("slack_self", json.dumps(sorted(self.slack_self)))
                self._slack_names(data)
        if "chat.postMessage" in path and self.watching("Slack"):
            f = form_fields(req)
            ch, text = f.get("channel") or "", f.get("text") or blocks_text(f.get("blocks"))
            if ch and text:
                self.mark_replied("Slack", ch)
                self.chat("Slack", "sent", who="you", place=self.slack_place(ch), text=self.slack_text(text),
                          key=f"slack-sent:{ch}:{f.get('client_msg_id') or time.time()}", context={"channel": ch})

    def _slack_event(self, text: str):
        if not text.startswith("{"):
            return
        ev = json.loads(text)
        if ev.get("type") != "message" or ev.get("subtype") not in (None, "thread_broadcast"):
            return
        user, ch = ev.get("user"), ev.get("channel") or ""
        if not user or user in self.slack_self or ev.get("bot_id") or not self.watching("Slack"):
            return
        mentioned = any(m in self.slack_self for m in MENTION.findall(ev.get("text") or ""))
        if not (ch.startswith("D") or mentioned):
            return
        if ch.startswith("D") and ch not in self.im_user:
            self._learn("Slack", ch, "DM", user)
        who = self.slack_name(user)
        kind = "dm" if ch.startswith("D") else "mention"
        self.chat("Slack", kind, who=who, place=self.slack_place(ch), text=self.slack_text(ev.get("text") or ""),
                  key=f"slack:{ch}:{ev.get('ts')}",
                  context={"channel": ch, "user": user, "ts": ev.get("ts"), "thread_ts": ev.get("thread_ts")})

    # ---------- Discord ----------
    @staticmethod
    def _discord_user(u: dict) -> str:
        return (u or {}).get("global_name") or (u or {}).get("username") or "someone"

    def _discord_dispatch(self, t: str, d: dict):
        if t == "READY":
            me = str((d.get("user") or {}).get("id") or "")
            if me and me != self.discord_me:
                self.discord_me = me
                self.set_setting("discord_me", me)
            users = {str(u.get("id")): u for u in d.get("users") or [] if isinstance(u, dict)}
            for ch in d.get("private_channels") or []:
                recips = ch.get("recipients") or [users.get(str(r), {"id": r}) for r in ch.get("recipient_ids") or []]
                names = ", ".join(self._discord_user(u) for u in recips if isinstance(u, dict)) or "someone"
                self._learn("Discord", str(ch.get("id")), f"DM with {names}" if len(recips) < 2 else names)
            for g in d.get("guilds") or []:
                gname = (g.get("properties") or {}).get("name") or g.get("name") or "a server"
                for ch in g.get("channels") or []:
                    if ch.get("name"):
                        self._learn("Discord", str(ch.get("id")), f"#{ch['name']} · {gname}")
            return
        if t == "CHANNEL_CREATE" and not d.get("guild_id"):
            recips = d.get("recipients") or []
            if recips:
                self._learn("Discord", str(d.get("id")), "DM with " + ", ".join(self._discord_user(u) for u in recips))
            return
        if t != "MESSAGE_CREATE" or not self.watching("Discord"):
            return
        author, ch = d.get("author") or {}, str(d.get("channel_id") or "")
        text = d.get("content") or ""
        if not text or author.get("bot"):
            return
        mine = bool(self.discord_me) and str(author.get("id")) == self.discord_me
        is_dm = not d.get("guild_id")
        who = self._discord_user(author)
        place = self.names.get(("Discord", ch)) or (f"DM with {who}" if is_dm and not mine else
                                                    "a DM" if is_dm else "a server channel")
        mentioned = any(str(u.get("id")) == self.discord_me for u in d.get("mentions") or [] if isinstance(u, dict))
        if mine:
            kind = "sent"
            self.mark_replied("Discord", ch)
        elif is_dm:
            kind = "dm"
        elif mentioned:
            kind = "mention"
        else:
            return
        self.chat("Discord", kind, who="you" if mine else who, place=place, text=text[:4000],
                  key=f"discord:{ch}:{d.get('id')}",
                  context={"channel": ch, "message_id": str(d.get("id")), "guild": str(d.get("guild_id") or "")})

    # ---------- Granola ----------
    def _granola_turn(self, sock_id: str, text: str):
        if '"end_of_turn":true' not in text.replace(" ", ""):
            return
        ev = json.loads(text)
        if ev.get("end_of_turn") and ev.get("turn_is_formatted") and ev.get("transcript"):
            self.meetings.setdefault(sock_id, []).append(ev["transcript"])

