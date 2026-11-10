"""Small builders for synthetic traffic. Imported by the test modules (tests/ is on sys.path)."""
from __future__ import annotations

import json
import re
import struct
import time

JSON = "application/json"
FORM = "application/x-www-form-urlencoded"
NAV = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
UUID = "550e8400-e29b-41d4-a716-446655440000"
UUID2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def send(events, app, host, path, body=None, *, method="POST", ctype=JSON, headers=None, resp="", status=200):
    """One HTTP exchange, the way the addon hands it to Events.http (raw request bytes)."""
    if body is not None and not isinstance(body, (bytes, str)):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode()
    events.http(app, host, method, path, ctype if body is not None else "", body, headers or {}, resp, status)


def visit(events, browser, host, path, title, status=200):
    send(events, browser, host, path, method="GET", headers=NAV, status=status,
         resp=f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head><body></body></html>")


def multipart(fields: dict, boundary="----WebKitFormBoundary7MA4YWxkTrZu0gW") -> tuple[str, str]:
    body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                   for k, v in fields.items()) + f"--{boundary}--\r\n"
    return body, f"multipart/form-data; boundary={boundary}"


def kinds(events) -> list[dict]:
    return list(events.types.values())


def kind(events, contains: str = "") -> dict:
    """The one kind whose signature contains `contains` (fails loudly when there are zero or several)."""
    hits = [t for t in events.types.values() if contains in t["sig"]]
    assert len(hits) == 1, f"expected one kind matching {contains!r}, got {[t['sig'] for t in hits]}"
    return hits[0]


def stored(events, sig: str | None = None) -> list[dict]:
    rows = events.store.q("SELECT * FROM events ORDER BY id")
    for r in rows:
        r["fields"] = json.loads(r["fields"]) if r["fields"] else {}
    return [r for r in rows if sig is None or r["sig"] == sig]


def readable(label: str) -> bool:
    """A label a person could read in a list: a few plain lowercase words, at least one real word,
    no leftover placeholders or identifier fragments."""
    ws = label.split()
    return (bool(re.fullmatch(r"[a-z][a-z ]*", label)) and 1 <= len(ws) <= 6
            and any(len(w) >= 4 for w in ws) and "id" not in ws
            and sum(len(w) <= 2 for w in ws) <= 1
            and label not in ("sync", "batch", "execute", "request", "data", "ops"))   # says nothing to a person


def wait_for(cond, timeout=5.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return cond()


# Credentials are assembled at run time so no literal secret-shaped string sits in the repo.
def fake_secrets() -> dict[str, str]:
    return {
        "aws_access_key": "AKIA" + "IOSFODNN7EXAMPLE",
        "github_token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0" * 2,
        "slack_token": "xoxb-" + "123456789012-abcdefghijkl",
        "anthropic_key": "sk-ant-" + "api03-" + "Zx9" * 12,
        "jwt": "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    }


# ---------- a minimal ETF encoder (the inverse of engine.discord.etf_decode) ----------
def etf(obj) -> bytes:
    return b"\x83" + _etf(obj)


def _etf(x) -> bytes:
    if x is None or isinstance(x, bool):
        a = {None: b"nil", True: b"true", False: b"false"}[x]
        return bytes([119, len(a)]) + a
    if isinstance(x, int):
        if 0 <= x < 256:
            return bytes([97, x])
        if -2 ** 31 <= x < 2 ** 31:
            return bytes([98]) + struct.pack(">i", x)
        raw = abs(x).to_bytes((abs(x).bit_length() + 7) // 8, "little")
        return bytes([110, len(raw), 1 if x < 0 else 0]) + raw
    if isinstance(x, float):
        return bytes([70]) + struct.pack(">d", x)
    if isinstance(x, str):
        b = x.encode()
        return bytes([109]) + struct.pack(">I", len(b)) + b
    if isinstance(x, (list, tuple)):
        if not x:
            return bytes([106])
        return bytes([108]) + struct.pack(">I", len(x)) + b"".join(_etf(v) for v in x) + bytes([106])
    if isinstance(x, dict):
        out = bytes([116]) + struct.pack(">I", len(x))
        for k, v in x.items():
            kb = k.encode()
            out += bytes([119, len(kb)]) + kb + _etf(v)      # keys as atoms, like Discord sends them
        return out
    raise TypeError(type(x))
