"""Cheap, local detectors run on every flow.

Two jobs:
  1. findings: secrets / personal data leaving the device (regex, no model).
  2. LLM + MCP recognition: pull model name and token usage out of AI API calls.

The regex layer is deliberately dumb. The interesting product question is what a
local classifier (the "post-tool checker" idea) adds on top of it.
"""
from __future__ import annotations

import json
import re

SECRETS = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"sk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[abcdeprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("password_field", re.compile(r"""(?i)["']?(?:password|passwd|pwd)["']?\s*[:=]\s*["']?[^"'&\s,}]{3,}""")),
]
PII = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("card_number", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
    ("uk_passport", re.compile(r"(?i)passport[^0-9]{0,30}\b\d{9}\b")),
]

# Which hosts a key "belongs" to. A key sent anywhere else is the alarming case.
KEY_HOME = {
    "anthropic_key": ("anthropic.com",),
    "openai_key": ("openai.com",),
    "github_token": ("github.com", "githubusercontent.com"),
    "slack_token": ("slack.com",),
    "google_api_key": ("googleapis.com", "google.com"),
}

LLM_HOSTS = {
    "api.anthropic.com": "anthropic",
    "api.openai.com": "openai",
    "generativelanguage.googleapis.com": "google",
    "openrouter.ai": "openrouter",
    "api.mistral.ai": "mistral",
    "api.groq.com": "groq",
    "api.together.xyz": "together",
    "api.deepseek.com": "deepseek",
    "chatgpt.com": "chatgpt-web",
    "claude.ai": "claude-web",
}


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(d) <= 19:
        return False
    s = 0
    for i, n in enumerate(reversed(d)):
        if i % 2:
            n *= 2
            n -= 9 if n > 9 else 0
        s += n
    return s % 10 == 0


def _card_prefix(s: str) -> bool:
    d = "".join(c for c in s if c.isdigit())
    return len(d) in (15, 16) and bool(re.match(r"4|5[1-5]|2[2-7]|3[47]|6011|65", d))


def _mask(s: str) -> str:
    return s if len(s) <= 10 else f"{s[:6]}…{s[-4:]}"


def scan(text: str, where: str, host: str) -> list[dict]:
    """Find secrets/PII in outgoing text. `where` is e.g. 'request.body'."""
    out: list[dict] = []
    if not text:
        return out
    for kind, rx in SECRETS:
        for m in rx.finditer(text):
            sev = "high"
            if where.startswith(("request.header", "request.url")):
                home = KEY_HOME.get(kind)
                # A credential sent to its own service is normal operation. Session
                # tokens (JWTs) in headers/URLs are how most apps authenticate.
                if kind == "jwt":
                    sev = "info" if where.startswith("request.header") else "low"
                else:
                    sev = "info" if (home is None or host.endswith(home)) else "high"
            elif kind == "password_field":
                sev = "medium"
            out.append({"kind": kind, "severity": sev, "where": where, "sample": _mask(m.group(0))})
            if len(out) > 20:
                return out
    if where.startswith("request.body"):
        for kind, rx in PII:
            for m in rx.finditer(text):
                if kind == "card_number" and not (_luhn(m.group(0)) and _card_prefix(m.group(0))):
                    continue
                out.append({"kind": kind, "severity": "medium" if kind != "email" else "low",
                            "where": where, "sample": _mask(m.group(0))})
                if len(out) > 20:
                    return out
    return out


def llm_info(host: str, path: str, req_text: str, resp_text: str, resp_ctype: str) -> dict | None:
    provider = LLM_HOSTS.get(host)
    if not provider:
        return None
    info: dict = {"provider": provider}
    try:
        req = json.loads(req_text) if req_text and req_text.lstrip().startswith("{") else {}
    except Exception:
        req = {}
    if isinstance(req, dict):
        info["model"] = req.get("model")
        msgs = req.get("messages") or req.get("input") or req.get("contents")
        if isinstance(msgs, list):
            info["messages"] = len(msgs)
        if req.get("tools"):
            info["tools"] = len(req["tools"])
        info["stream"] = bool(req.get("stream"))

    usage: dict = {}
    if "event-stream" in (resp_ctype or ""):
        # SSE: usage arrives in message_start / message_delta (Anthropic) or the final chunk (OpenAI).
        for line in resp_text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            for u in (ev.get("usage"), (ev.get("message") or {}).get("usage"),
                      (ev.get("response") or {}).get("usage")):
                if isinstance(u, dict):
                    usage.update({k: v for k, v in u.items() if isinstance(v, int)})
    else:
        try:
            body = json.loads(resp_text)
            if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                usage = {k: v for k, v in body["usage"].items() if isinstance(v, int)}
            if isinstance(body, dict) and not info.get("model"):
                info["model"] = body.get("model")
        except Exception:
            pass
    info["input_tokens"] = usage.get("input_tokens") or usage.get("prompt_tokens")
    info["output_tokens"] = usage.get("output_tokens") or usage.get("completion_tokens")
    return info


def mcp_info(req_text: str) -> dict | None:
    """Remote MCP servers speak JSON-RPC over HTTP; tag tools/call etc."""
    if not req_text or '"jsonrpc"' not in req_text[:200]:
        return None
    try:
        msg = json.loads(req_text)
    except Exception:
        return None
    if isinstance(msg, dict) and msg.get("jsonrpc") == "2.0" and "method" in msg:
        p = msg.get("params") or {}
        return {"method": msg["method"], "tool": p.get("name") if isinstance(p, dict) else None}
    return None
