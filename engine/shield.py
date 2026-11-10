"""Shield: keep secrets in tool outputs from reaching model providers.

Outgoing LLM requests are rewritten so that secrets found in tool results
become stable placeholders ([[ambient:aws_access_key:3f2a9c]]). The model can
still reason about "the AWS key"; it never sees the value. Placeholders in the
model's replies are swapped back to the real value before the agent receives
them, so tool calls the model makes with a placeholder still work locally.

Placeholders are deterministic (hash of the value), so the same secret gets
the same placeholder in every turn and prompt caching keeps working.

Detection is pluggable: `classifier` is any callable text -> [(start, end, kind)].
The default is the regex detector in detect.py; Tofunmi's classifier can slot in.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Iterable

from engine import decide, detect

PH_RE = re.compile(r"\[\[ambient:([a-z_]+):([0-9a-f]{6})\]\]")
PH_PREFIX = "[[ambient:"

# What Shield redacts by default. Emails/phone numbers stay visible: agents
# legitimately need them, and redacting them breaks ordinary tasks.
DEFAULT_KINDS = {
    "anthropic_key", "openai_key", "aws_access_key", "github_token", "slack_token",
    "google_api_key", "private_key", "jwt", "password_field", "card_number", "uk_passport",
}

Span = tuple[int, int, str]

# Personal details: redacted unless the local classifier decides the user's task
# needs that kind of detail. Statement = what the judge is asked to confirm.
PII = {
    "email": (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
              "This task needs people's email addresses."),
    "phone": (re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?|0)\d{2,4}[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?!\d)"),
              "This task needs people's phone numbers."),
    "uk_ni": (re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
              "This task needs someone's National Insurance number."),
    "iban": (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b"),
             "This task needs bank account numbers."),
}
NEEDS_THRESHOLD = 0.5
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def task_text(body: dict) -> str:
    """The user's latest request: last user turn that isn't only tool results."""
    for msg in reversed(body.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = ""
        text = REMINDER_RE.sub("", text).strip()
        if text:
            return text[-2000:]
    for item in reversed(body.get("input") or []):  # OpenAI Responses
        if isinstance(item, dict) and item.get("role") == "user":
            c = item.get("content")
            text = c if isinstance(c, str) else " ".join(
                p.get("text", "") for p in (c or []) if isinstance(p, dict))
            if text.strip():
                return text.strip()[-2000:]
    return ""


def regex_classifier(text: str) -> list[Span]:
    spans: list[Span] = []
    for kind, rx in detect.SECRETS:
        for m in rx.finditer(text):
            if kind == "password_field":
                # redact only the value, keep `"password": ` so the structure stays readable
                val = re.search(r"[:=]\s*[\"']?([^\"'&\s,}]{3,})", m.group(0))
                if val:
                    spans.append((m.start() + val.start(1), m.start() + val.end(1), kind))
                continue
            if kind == "private_key":
                end = text.find("-----END", m.end())
                end = text.find("-----", end + 8) + 5 if end != -1 else m.end()
                spans.append((m.start(), max(end, m.end()), kind))
                continue
            spans.append((m.start(), m.end(), kind))
    for kind, rx in detect.PII:
        if kind == "card_number":
            for m in rx.finditer(text):
                if detect._luhn(m.group(0)) and detect._card_prefix(m.group(0)):
                    spans.append((m.start(), m.end(), kind))
        elif kind == "uk_passport":
            for m in rx.finditer(text):
                num = re.search(r"\d{9}", m.group(0))
                if num:
                    spans.append((m.start() + num.start(), m.start() + num.end(), kind))
    # drop overlaps, keep the earliest/longest
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    out: list[Span] = []
    for s in spans:
        if not out or s[0] >= out[-1][1]:
            out.append(s)
    return out


NOTE = ("Values like [[ambient:kind:hash]] are secrets redacted by the user's local privacy tool. "
        "Use them verbatim where the real value is needed (commands, files, tool arguments); "
        "they are restored locally. Never ask for or guess the real value.")


def add_note(provider: str, body: dict) -> None:
    """Tell the model what placeholders mean. Appended last in the system prompt so
    cached prefixes before it stay valid, and added on every request once a
    placeholder is present, so it is stable across turns."""
    if provider == "anthropic":
        sys_ = body.get("system")
        if isinstance(sys_, list):
            if not any(isinstance(b, dict) and b.get("text") == NOTE for b in sys_):
                sys_.append({"type": "text", "text": NOTE})
        elif isinstance(sys_, str) and sys_:
            if NOTE not in sys_:
                body["system"] = sys_ + "\n\n" + NOTE
        else:
            body["system"] = NOTE
    elif provider == "openai":
        if isinstance(body.get("messages"), list):
            msgs = body["messages"]
            if not any(isinstance(m, dict) and m.get("content") == NOTE for m in msgs):
                msgs.insert(0, {"role": "system", "content": NOTE})
        elif isinstance(body.get("input"), list):
            ins = body.get("instructions") or ""
            if NOTE not in ins:
                body["instructions"] = (ins + "\n\n" + NOTE).strip()
    elif provider == "google":
        si = body.setdefault("systemInstruction", {"parts": []})
        parts = si.setdefault("parts", []) if isinstance(si, dict) else None
        if isinstance(parts, list) and not any(isinstance(p, dict) and p.get("text") == NOTE for p in parts):
            parts.append({"text": NOTE})


class Vault:
    """placeholder <-> original, for the life of the engine process (memory only)."""

    def __init__(self):
        self.by_ph: dict[str, str] = {}
        self.by_val: dict[str, str] = {}

    def placeholder(self, value: str, kind: str) -> str:
        ph = self.by_val.get(value)
        if ph:
            return ph
        ph = f"[[ambient:{kind}:{hashlib.sha256(value.encode()).hexdigest()[:6]}]]"
        self.by_ph[ph] = value
        self.by_val[value] = ph
        return ph

    def restore(self, text: str) -> tuple[str, int]:
        n = 0

        def sub(m):
            nonlocal n
            v = self.by_ph.get(m.group(0))
            if v is None:
                return m.group(0)
            n += 1
            return v
        return PH_RE.sub(sub, text), n

    def known(self) -> Iterable[tuple[str, str]]:
        # longest first, so a secret that contains another is replaced whole
        return sorted(self.by_val.items(), key=lambda kv: -len(kv[0]))


class Shield:
    def __init__(self, kinds: set[str] | None = None,
                 classifier: Callable[[str], list[Span]] = regex_classifier):
        self.vault = Vault()
        self.kinds = kinds or set(DEFAULT_KINDS)
        self.classifier = classifier
        self.task = ""
        self.pii_decided: dict[str, tuple[bool, float | None]] = {}  # value -> (redact, p)

    def _pii_spans(self, text: str) -> list[tuple[int, int, str, float | None]]:
        out = []
        for kind, (rx, statement) in PII.items():
            for m in rx.finditer(text):
                value = m.group(0)
                if value not in self.pii_decided:
                    p = decide.prob(self.task, statement) if self.task else None
                    # no task context or no model: leave it visible, as before Shield
                    self.pii_decided[value] = (p is not None and p < NEEDS_THRESHOLD, p)
                redact, p = self.pii_decided[value]
                if redact:
                    out.append((m.start(), m.end(), kind, p))
        return out

    # ---------- redaction ----------
    def redact_text(self, text: str, hits: list[dict], where: str) -> str:
        if not text:
            return text
        spans = [(*s, None) for s in self.classifier(text) if s[2] in self.kinds]
        taken = [(a, b) for a, b, _, _ in spans]
        spans += [s for s in self._pii_spans(text) if not any(s[0] < b and a < s[1] for a, b in taken)]
        spans.sort()
        if spans:
            parts, last = [], 0
            for start, end, kind, p in spans:
                if start < last:
                    continue
                value = text[start:end]
                ph = self.vault.placeholder(value, kind)
                parts += [text[last:start], ph]
                last = end
                hit = {"kind": kind, "where": where, "placeholder": ph, "sample": detect._mask(value)}
                if p is not None:
                    hit["why"] = f"task didn't need it (p={p:.2f})"
                hits.append(hit)
            parts.append(text[last:])
            text = "".join(parts)
        return self.scrub_known(text)

    def scrub_known(self, text: str) -> str:
        # Values redacted earlier must never leak back, e.g. when the agent's
        # history now contains a tool call the model made with the restored value.
        for value, ph in self.vault.known():
            if value in text:
                text = text.replace(value, ph)
        return text

    def _walk(self, obj, fn):
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, list):
            return [self._walk(x, fn) for x in obj]
        if isinstance(obj, dict):
            return {k: self._walk(v, fn) for k, v in obj.items()}
        return obj

    def redact_request(self, provider: str, body: dict) -> tuple[dict, list[dict]]:
        """Redact tool outputs in an LLM API request body. Returns (body, hits)."""
        hits: list[dict] = []
        self.task = task_text(body)
        tool = lambda s, where: self.redact_text(s, hits, where)  # noqa: E731

        def tool_any(x, where):
            return self._walk(x, lambda s: tool(s, where))

        if isinstance(body.get("messages"), list):
            for msg in body["messages"]:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                # Anthropic: tool_result blocks inside user messages
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            block["content"] = tool_any(block.get("content"), "tool_result")
                # OpenAI chat: role=tool messages
                if msg.get("role") == "tool":
                    msg["content"] = tool_any(content, "tool_message")
        if isinstance(body.get("input"), list):  # OpenAI Responses API
            for item in body["input"]:
                if isinstance(item, dict) and item.get("type") in ("function_call_output", "custom_tool_call_output"):
                    item["output"] = tool_any(item.get("output"), "function_call_output")
        if isinstance(body.get("contents"), list):  # Gemini
            for c in body["contents"]:
                for part in (c.get("parts") or []) if isinstance(c, dict) else []:
                    if isinstance(part, dict) and "functionResponse" in part:
                        part["functionResponse"] = tool_any(part["functionResponse"], "function_response")
        # Anything already in the vault, anywhere in the request (history, tool inputs, text)
        body = self._walk(body, self.scrub_known)
        if PH_PREFIX in json.dumps(body, ensure_ascii=False):
            add_note(provider, body)
        return body, hits

    def redact_mcp_result(self, msg: dict) -> tuple[dict, list[dict]]:
        """Redact a remote MCP server's JSON-RPC result before the agent sees it."""
        hits: list[dict] = []
        if isinstance(msg, dict) and "result" in msg:
            msg["result"] = self._walk(msg["result"], lambda s: self.redact_text(s, hits, "mcp_result"))
        return msg, hits

    # ---------- restoration ----------
    def restore_json(self, body) -> tuple[object, int]:
        total = 0

        def fn(s):
            nonlocal total
            s2, n = self.vault.restore(s)
            total += n
            return s2
        return self._walk(body, fn), total


class AnthropicSSERestorer:
    """Rewrites an Anthropic Messages SSE stream, restoring placeholders.

    text_delta: forwarded as it arrives, except a possible partial placeholder
    at the end, which is held back until it completes (or the block ends).
    input_json_delta (tool arguments): held until content_block_stop and sent
    as one delta, so a placeholder split across chunks is still restored.
    """

    def __init__(self, shield: Shield, on_restore: Callable[[int], None]):
        self.shield = shield
        self.on_restore = on_restore
        self.buf = b""
        self.text_carry: dict[int, str] = {}
        self.json_buf: dict[int, list[str]] = {}

    def _event(self, name: str | None, data: dict) -> bytes:
        head = f"event: {name}\n" if name else ""
        return f"{head}data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    def _restore(self, s: str) -> str:
        s2, n = self.shield.vault.restore(s)
        if n:
            self.on_restore(n)
        return s2

    def _split_carry(self, s: str) -> tuple[str, str]:
        i = s.rfind("[[")
        if i == -1:
            # a lone trailing "[" could start a placeholder
            return (s[:-1], "[") if s.endswith("[") else (s, "")
        tail = s[i:]
        if "]]" in tail or not PH_PREFIX.startswith(tail[:len(PH_PREFIX)]) or len(tail) > 64:
            return s, ""
        return s[:i], tail

    def _handle(self, raw: bytes) -> bytes:
        text = raw.decode("utf-8", "replace")
        name, data_lines = None, []
        for line in text.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return raw
        try:
            ev = json.loads("\n".join(data_lines))
        except Exception:
            return raw
        t = ev.get("type")
        idx = ev.get("index")
        if t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                s = self.text_carry.pop(idx, "") + d.get("text", "")
                emit, carry = self._split_carry(s)
                if carry:
                    self.text_carry[idx] = carry
                d["text"] = self._restore(emit)
                return self._event(name, ev) if d["text"] else b""
            if d.get("type") == "input_json_delta":
                self.json_buf.setdefault(idx, []).append(d.get("partial_json", ""))
                return b""
        if t == "content_block_stop":
            out = b""
            if idx in self.text_carry:
                out += self._event("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": self._restore(self.text_carry.pop(idx))}})
            if idx in self.json_buf:
                joined = self._restore("".join(self.json_buf.pop(idx)))
                out += self._event("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": joined}})
            return out + raw
        return raw

    def __call__(self, chunk: bytes) -> bytes:
        if chunk == b"":  # end of stream
            rest, self.buf = self.buf, b""
            return self._handle(rest) if rest.strip() else rest
        self.buf += chunk.replace(b"\r\n", b"\n")
        out = b""
        while b"\n\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n\n", 1)
            out += self._handle(raw + b"\n\n")
        return out
