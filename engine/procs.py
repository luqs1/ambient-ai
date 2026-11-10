"""Map a connection to the process that owns it (regular-proxy mode), and decide whether an
agent CLI process trusts our CA. OS specifics live in osal.py."""
from __future__ import annotations

import os
import time

from engine import osal

_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_names: dict[int, dict] = {}
OWN_PID = os.getpid()

_app_name = osal.app_name
_bare_name = osal.bare_name


def _describe(pid: int) -> dict:
    if pid not in _names:
        _names[pid] = osal.describe_pid(pid)
    return _names[pid]


def lookup(port: int, proto: str = "TCP") -> dict | None:
    key = (proto, port)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    pid = osal.pid_for_port(port, proto)
    if not pid:
        return None
    info = _describe(pid)
    _cache[key] = (time.time(), info)
    return info


_trust: dict[int, bool] = {}
CA_MARKERS = ("mitmproxy-ca", "ambient-ca", "Ambient/ca")
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def env_trusts_ca(pid: int) -> bool:
    """True if the process was started with our CA in its trust env vars.

    Agent CLIs (Node/Bun/Python) ignore the system trust store: we only decrypt them when they
    were launched with NODE_EXTRA_CA_CERTS (or SSL_CERT_FILE…) pointing at our CA, so we never
    break a session that wasn't set up for it."""
    if pid not in _trust:
        env = osal.pid_environ(pid)
        _trust[pid] = any(any(m in env.get(k, "") for m in CA_MARKERS) for k in CA_VARS)
    return _trust[pid]
