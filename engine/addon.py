"""ambient: a mitmproxy addon that records what every app on the device sends
and receives, decrypting TLS where the app trusts our CA, and recording
metadata (process, host, bytes, timing) where it doesn't.

Run via ./ambient.sh (see README). Loaded by mitmdump with -s.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import mitmproxy_rs
from mitmproxy import ctx, http, tls, tcp, udp, dns, connection
from mitmproxy.addons.next_layer import NextLayer as BuiltinNextLayer, _starts_like_quic
from mitmproxy.proxy import commands, events, layer
from mitmproxy.proxy import layers, server as proxy_server
from mitmproxy.proxy.layer import NextLayer
from mitmproxy.proxy.layers.http import HTTPMode  # noqa: F401  (keeps layer imports warm)
from mitmproxy.utils import strutils

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import detect, procs  # noqa: E402
from engine.store import Store, SEV_RANK, NOT_STORED  # noqa: E402
from engine.shield import Shield, AnthropicSSERestorer  # noqa: E402
from engine.moments import Moments  # noqa: E402
from engine.events import Events, SENSITIVE_PATH, sensitive  # noqa: E402,F401
from engine.localstore import LocalStores  # noqa: E402
from engine.wsdecode import SocketDecoder  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Hosts known to pin certificates. Passing them through up front avoids one
# failed connection each (iCloud sync, push, messaging) before we learn it.
KNOWN_PINNED = [
    "apple.com", "icloud.com", "icloud-content.com", "apple-cloudkit.com", "mzstatic.com",
    "push.apple.com", "itunes.apple.com", "apple-dns.net",
    "whatsapp.net", "whatsapp.com", "signal.org", "telegram.org",
] + procs.osal.pinned_hosts()
# Never decrypted, whatever the app: password managers, banks, payment and identity providers.
PRIVATE_HOSTS = [
    "bitwarden.com", "bitwarden.eu", "1password.com", "1password.eu", "1passwordusercontent.com", "lastpass.com",
    "dashlane.com", "keepersecurity.com", "proton.me", "protonmail.com",
    "monzo.com", "starlingbank.com", "revolut.com", "wise.com", "hsbc.co.uk", "barclays.co.uk", "lloydsbank.com",
    "natwest.com", "santander.co.uk", "nationwide.co.uk", "chase.com", "bankofamerica.com", "wellsfargo.com",
    "americanexpress.com", "paypal.com", "stripe.com", "checkout.com", "adyen.com", "plaid.com", "truelayer.com",
    "trading212.com", "coinbase.com", "binance.com", "kraken.com",
    "accounts.google.com", "login.microsoftonline.com", "login.live.com", "appleid.apple.com", "okta.com",
    "auth0.com", "duosecurity.com", "gov.uk", "nhs.uk", "hmrc.gov.uk", "irs.gov",
]
# An app that rejects our cert for this many different hosts doesn't trust the
# system store at all (Node, Python certifi, Java, Firefox). Pass all of it through.
APP_REJECT_THRESHOLD = 2
# Agent CLIs are learned per process, not per name: one `claude` started with
# NODE_EXTRA_CA_CERTS trusts us, another started without it doesn't.
AGENT_APPS = {"claude", "node", "bun", "codex", "pi", "opencode", "cursor-agent", "gemini"}
# LLM APIs Shield rewrites (web chat UIs use other formats).
SHIELD_HOSTS = {"api.anthropic.com": "anthropic", "api.openai.com": "openai", "openrouter.ai": "openai",
                "generativelanguage.googleapis.com": "google", "api.mistral.ai": "openai",
                "api.groq.com": "openai", "api.together.xyz": "openai", "api.deepseek.com": "openai"}
# Model providers' own APIs. What goes there is a conversation with a model (prompts, tool output), not
# something you did in an app: it never becomes an event, whether it travels as a request, a stream or a socket.
PROVIDER_HOSTS = set(SHIELD_HOSTS) | {
    "api.x.ai", "api.cohere.com", "api.cohere.ai", "api.perplexity.ai", "api.fireworks.ai", "api.cerebras.ai",
    "cloudcode-pa.googleapis.com",                       # Gemini CLI signed in with a Google account
}
PROVIDER_SUFFIXES = (".openai.azure.com", "aiplatform.googleapis.com")
# Hosts that are an app in the browser and a model API for an agent CLI: only these paths are the API.
PROVIDER_PATHS = {"chatgpt.com": ("/backend-api/codex/",)}     # Codex signed in with a ChatGPT account


def provider_api(host: str, path: str = "") -> bool:
    host = (host or "").lower().rstrip(".")
    if host in PROVIDER_HOSTS or host.endswith(PROVIDER_SUFFIXES) or host.startswith("bedrock-runtime."):
        return True
    return (path or "").startswith(PROVIDER_PATHS.get(host, ()))


# The app looked at our certificate and said no.
TRUST_ERRORS = ("does not trust", "unknown ca", "bad certificate", "certificate unknown")
# The connection dropped mid-handshake. Usually a cancelled request or a restart, sometimes a silent
# rejection: only believe it after it repeats for a host the app has never completed a handshake with.
SOFT_ERRORS = ("disconnected during the handshake",)
SOFT_LIMIT = 3


class DropLayer(layer.Layer):
    """Closes the client side immediately (used to refuse QUIC)."""
    def _handle_event(self, event):
        if isinstance(event, events.Start):
            yield commands.CloseConnection(self.context.client)


# --- process attribution in local mode -------------------------------------
# The macOS redirector knows which process opened each flow (pid, process_name)
# but mitmproxy drops it when building the Client. Keep it.
REDIRECTOR_PROC: dict[str, tuple[int | None, str | None]] = {}
if not getattr(proxy_server.LiveConnectionHandler, "_ambient_patched", False):
    _orig_init = proxy_server.LiveConnectionHandler.__init__

    def _init(self, reader, writer, options, mode):
        _orig_init(self, reader, writer, options, mode)
        if isinstance(writer, mitmproxy_rs.Stream):
            pid = writer.get_extra_info("pid", None)
            name = writer.get_extra_info("process_name", None)
            if pid or name:
                REDIRECTOR_PROC[self.client.id] = (pid, name)

    proxy_server.LiveConnectionHandler.__init__ = _init
    proxy_server.LiveConnectionHandler._ambient_patched = True


def body(msg, limit: int) -> tuple[str, int]:
    raw_len = len(msg.raw_content or b"")
    try:
        data = msg.get_content(strict=False) or b""
    except Exception:
        data = msg.raw_content or b""
    if not data:
        return "", raw_len
    if strutils.is_mostly_bin(data[:4096]):
        return f"[binary {len(data)} bytes, {msg.headers.get('content-type', '?')}]", raw_len
    text = data[:limit].decode("utf-8", "replace")
    if len(data) > limit:
        text += f"\n…[truncated, {len(data)} bytes total]"
    return text, raw_len


def worst(findings: list[dict]) -> str | None:
    return max((f["severity"] for f in findings), key=lambda s: SEV_RANK[s], default=None)


def csv(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def suffix_match(host: str, patterns) -> bool:
    return any(host == p or host.endswith("." + p) for p in patterns)


class AmbientAddon:
    def __init__(self):
        self.clients: dict[str, dict] = {}   # client conn id -> process info
        self.pass_pairs: set[tuple[str, str]] = set()   # (app, host) learned
        self.pass_apps: set[str] = set()                # whole apps learned
        self.rejects: dict[str, set[str]] = {}          # app -> hosts that rejected
        self.quic_blocked: set[tuple[str, str]] = set()
        self.pass_pids: set[int] = set()                # agent processes that don't trust us
        self.pid_rejects: dict[int, set[str]] = {}
        self.ok_apps: set[str] = set()                  # apps that have completed TLS with our certificate
        self.ok_pairs: set[tuple[str, str]] = set()
        self.soft: dict[tuple[str, str], int] = {}
        self.shield = Shield()
        self.store: Store | None = None
        self.ui = None
        self.sockdec: dict[tuple[str, bool], SocketDecoder] = {}
        self.crl_always = procs.osal.certs_need_crl()

    # ---------- setup ----------
    def load(self, loader):
        loader.add_option("ambient_db", str, str(ROOT / "data" / "ambient.db"), "SQLite path")
        loader.add_option("ambient_ui", int, 8787, "UI port on 127.0.0.1 (0 = off)")
        loader.add_option("ambient_body_limit", int, 256 * 1024, "Max body bytes stored")
        loader.add_option("ambient_passthrough", str, "",
                          "Comma-separated hosts never decrypted (suffix match), e.g. your bank")
        loader.add_option("ambient_passthrough_apps", str, "",
                          "Comma-separated apps never decrypted (metadata only), e.g. claude")
        loader.add_option("ambient_passthrough_pids", str, "",
                          "Comma-separated process ids never decrypted (e.g. an agent session to leave alone)")
        loader.add_option("ambient_shield", bool, True, "Redact secrets in tool outputs sent to LLM APIs")
        loader.add_option("ambient_local", bool, True,
                          "Read apps' local databases when their traffic can't be decrypted")

    def running(self):
        os.makedirs(os.path.dirname(ctx.options.ambient_db), exist_ok=True)
        self.store = Store(ctx.options.ambient_db)
        self.events = Events(self.store)
        # Kinds learned from model providers' APIs before those were excluded (your prompts and the model's
        # replies are not app activity to trigger on): drop them and their stored events.
        self.events.skip = provider_api
        gone = self.events.forget(lambda t: not t.get("curated") and provider_api(t.get("host") or ""))
        if gone:
            print(f"ambient: forgot {gone} kinds learned from model providers' APIs", flush=True)
        self.moments = Moments(self.store, self.events)
        self.rec = self.moments.rec
        # Reading other apps' local data only makes sense (and is only allowed to prompt) under real capture.
        capturing = any(str(m).startswith("local") for m in ctx.options.mode)
        self.local = LocalStores(self.events, self.rec, self.moments.watching,
                                 enabled=capturing and ctx.options.ambient_local)
        if not self.moments.setting("events_backfilled"):
            n = self.events.backfill()
            self.moments.set_setting("events_backfilled", "1")
            ctx.log.info(f"ambient: built the activity catalog from {n} recorded exchanges")
        # Forget "dropped mid-handshake" verdicts from older versions, and re-test whole-app verdicts daily.
        with self.store.lock:
            self.store.db.execute("DELETE FROM passthrough WHERE reason LIKE '%disconnected during the handshake%' "
                                  "OR (host = '*' AND ts < ?)", (time.time() - 86400,))
            self.store.db.execute("DELETE FROM passthrough WHERE host = '*' AND app NOT IN "
                                  "(SELECT app FROM passthrough WHERE host != '*' GROUP BY app HAVING COUNT(*) >= 2)")
            self.store.db.commit()
        for app, host in self.store.passthrough_pairs():
            if host == "*":
                self.pass_apps.add(app)
            else:
                self.pass_pairs.add((app, host))
        # Old raw exchanges are deleted (now and hourly); rows stored before credentials were masked are rewritten once.
        self.store.start_maintenance(log=ctx.log.info, sensitive=sensitive)
        if ctx.options.ambient_ui:
            from engine.ui import serve
            self.ui = serve(self.store, ctx.options.ambient_ui, self.moments,
                           lambda: ",".join(ctx.options.mode), events=self.events, local=self.local,
                           on_trusted=self.forget_rejections)
            ctx.log.info(f"Ambient UI: http://127.0.0.1:{ctx.options.ambient_ui}")

    def done(self):
        if self.store:
            self.store.stop_maintenance()
        if self.ui:
            self.ui.shutdown()

    def forget_rejections(self):
        """The certificate was just trusted. Apps that refused it before were right to; ask them again
        instead of passing them through for another day (pinned hosts are simply learned again)."""
        try:
            for seen in (self.pass_pairs, self.pass_apps, self.rejects, self.soft, self.pass_pids, self.pid_rejects):
                seen.clear()
            with self.store.lock:
                self.store.db.execute("DELETE FROM passthrough")
                self.store.db.commit()
        except Exception as e:
            ctx.log.warn(f"ambient: could not reset learned pass-throughs: {e!r}")

    # ---------- who is talking ----------
    async def client_connected(self, client: connection.Client):
        if client.proxy_mode.type_name == "wireguard":
            info = {"app": f"device {client.peername[0]}", "pid": None, "path": "wireguard peer"}
        elif client.id in REDIRECTOR_PROC:
            pid, name = REDIRECTOR_PROC.pop(client.id)
            ps = await asyncio.to_thread(procs._describe, pid) if pid else {}
            path = name or ps.get("path", "")
            if ".app/" in path + "/":
                app = procs._app_name(path)          # bundle name: "Google Chrome"
            else:                                    # bare binary: what ps calls it ("claude", not "2.1.319")
                app = procs._bare_name(ps.get("path") or path) or "unknown"
            info = {"pid": pid, "path": path, "app": app}
        else:
            proto = "UDP" if client.transport_protocol == "udp" else "TCP"
            info = await asyncio.to_thread(procs.lookup, client.peername[1], proto)
            info = dict(info) if info else {"app": "unknown", "pid": None, "path": ""}
        self.clients[client.id] = info

    def client_disconnected(self, client: connection.Client):
        self.clients.pop(client.id, None)
        REDIRECTOR_PROC.pop(client.id, None)

    def _who(self, client: connection.Client) -> dict:
        info = self.clients.get(client.id) or {}
        return {"app": info.get("app", "unknown"), "pid": info.get("pid"),
                "process_path": info.get("path"), "client": f"{client.peername[0]}:{client.peername[1]}"}

    # ---------- decide: decrypt, or pass through and count bytes ----------
    def _passthrough_reason(self, app: str, host: str, pid: int | None = None) -> str | None:
        if pid and (pid in self.pass_pids or str(pid) in csv(ctx.options.ambient_passthrough_pids)):
            return f"{app} (pid {pid}) doesn't trust the ambient CA, so its traffic passes through"
        if app in AGENT_APPS and not (pid and procs.env_trusts_ca(pid)):
            return f"{app} wasn't started with Ambient's certificate, so it passes through untouched (Shield off for this session)"
        if app in self.pass_apps or app in csv(ctx.options.ambient_passthrough_apps):
            return f"{app} doesn't trust the ambient CA, so all its traffic passes through"
        if (app, host) in self.pass_pairs:
            return f"{app} rejected our certificate for {host} before (pinning)"
        if suffix_match(host, PRIVATE_HOSTS):
            return "sign-in, banking and password-manager traffic is never read"
        if suffix_match(host, KNOWN_PINNED) or suffix_match(host, csv(ctx.options.ambient_passthrough)):
            return "host is on the never-decrypt list (pins its certificates, or you chose so)"
        return None

    def next_layer(self, nl: NextLayer):
        # Script addons run before mitmproxy's own next_layer addon (which then
        # leaves our choice alone). If this connection starts with a TLS/QUIC
        # ClientHello we know we shouldn't intercept, choose a raw TCP/UDP layer
        # that still emits flow events, so we get bytes + timing.
        c = nl.context
        if c.client.tls or (nl.layer and not isinstance(
                nl.layer, (layers.ClientTLSLayer, layers.ServerTLSLayer, layers.ClientQuicLayer,
                           layers.ServerQuicLayer))):
            return
        info = self.clients.get(c.client.id) or {}
        app, pid = info.get("app", "unknown"), info.get("pid")
        if c.client.transport_protocol == "udp" and _starts_like_quic(nl.data_client(), c.server.address):
            # mitmproxy's QUIC interception is fragile (parse errors, crashes). Never
            # intercept QUIC: relay it raw for apps we can't decrypt anyway, and drop
            # it for the rest so they fall back to TCP+TLS, which we can read.
            host = c.server.address[0] if c.server.address else "?"
            if self._passthrough_reason(app, host, pid):
                nl.layer = layers.UDPLayer(c, ignore=False)
                self.clients.setdefault(c.client.id, {})["opaque"] = (host, "QUIC, app not decryptable")
            else:
                nl.layer = DropLayer(c)
                if (app, host) not in self.quic_blocked:
                    self.quic_blocked.add((app, host))
                    self.store.add({"ts": time.time(), "kind": "quic-blocked", "decrypted": 0,
                                    **self._who(c.client), "host": host,
                                    "note": "QUIC (HTTP/3) dropped so the app falls back to TCP+TLS, which ambient can read"})
            return
        try:
            ch = BuiltinNextLayer._get_client_hello(c, nl.data_client())
        except Exception:
            return
        if ch is None:
            return
        host = ch.sni or (c.server.address[0] if c.server.address else "?")
        reason = self._passthrough_reason(app, host, pid)
        if reason:
            raw = layers.TCPLayer if c.client.transport_protocol == "tcp" else layers.UDPLayer
            nl.layer = raw(c, ignore=False)
            self.clients.setdefault(c.client.id, {})["opaque"] = (host, reason)

    def tls_clienthello(self, data: tls.ClientHelloData):
        if self.crl_always:
            self._crl_on_every_cert()

    def _crl_on_every_cert(self):
        """Give every certificate we mint a CRL address (see osal.certs_need_crl). mitmproxy only does so when
        the real certificate has one; it answers any request for its CRL path itself, on whatever host."""
        tlsconfig = ctx.master.addons.get("tlsconfig")
        store = getattr(tlsconfig, "certstore", None)
        if store is None or getattr(store, "_ambient_crl", False):
            return
        mint = store.get_cert

        def get_cert(commonname, sans, organization=None, crl_url=None):
            if not crl_url:
                sans = list(sans)
                names = [getattr(s, "value", s) for s in sans]
                host = next((n for n in names if isinstance(n, str) and n and "*" not in n), None)
                if host:
                    crl_url = f"http://{host}{tlsconfig.crl_path()}"
            return mint(commonname, sans, organization, crl_url)

        store.get_cert = get_cert
        store._ambient_crl = True

    def tls_failed_client(self, data: tls.TlsData):
        # The app refused the certificate we minted. Remember (app, host) so the
        # next connection passes through; if the app refuses for several hosts,
        # it ignores the system trust store, so pass the whole app.
        host = data.conn.sni or (data.context.server.address[0] if data.context.server.address else "?")
        who = self._who(data.context.client)
        app = who["app"]
        err = str(data.conn.error or "")
        self.store.add({
            "ts": time.time(), "kind": "tls-rejected", "decrypted": 0, **who,
            "server": f"{data.context.server.address}", "host": host,
            "note": f"app rejected our certificate (one connection failed; it will pass through from now on): {err[:200]}",
        })
        low = err.lower()
        if any(k in low for k in SOFT_ERRORS):
            if (app, host) in self.ok_pairs:
                return          # this app has read this host through us before: just a dropped connection
            n = self.soft[(app, host)] = self.soft.get((app, host), 0) + 1
            if n < SOFT_LIMIT:
                return
        elif not any(k in low for k in TRUST_ERRORS):
            self.pass_pairs.add((app, host))
            return  # a handshake/parse problem, not the app distrusting our CA
        if app in AGENT_APPS and who["pid"]:
            seen = self.pid_rejects.setdefault(who["pid"], set())
            seen.add(host)
            if len(seen) >= APP_REJECT_THRESHOLD:
                self.pass_pids.add(who["pid"])
            return
        self.pass_pairs.add((app, host))
        self.store.add_passthrough(app, host, err[:300], time.time())
        seen = self.rejects.setdefault(app, set())
        seen.add(host)
        if (len(seen) >= APP_REJECT_THRESHOLD and app not in self.pass_apps and app != "unknown"
                and app not in self.ok_apps):       # an app that trusts us elsewhere is pinning these hosts only
            self.pass_apps.add(app)
            self.store.add_passthrough(app, "*", f"rejected {len(seen)} hosts", time.time())
            ctx.log.info(f"ambient: {app} doesn't trust the CA; passing all its traffic through")

    def tls_established_client(self, data: tls.TlsData):
        app = self._who(data.context.client)["app"]
        host = data.conn.sni or ""
        self.ok_apps.add(app)
        self.ok_pairs.add((app, host))
        self.soft.pop((app, host), None)

    # ---------- Shield: redact tool outputs going to model providers ----------
    def requestheaders(self, flow: http.HTTPFlow):
        if flow.request.pretty_host in SHIELD_HOSTS and ctx.options.ambient_shield:
            # uncompressed responses, so streamed replies can be rewritten chunk by chunk
            flow.request.headers["accept-encoding"] = "identity"

    def request(self, flow: http.HTTPFlow):
        if not ctx.options.ambient_shield:
            return
        host = flow.request.pretty_host
        ctype = flow.request.headers.get("content-type", "")
        if "json" not in ctype or not flow.request.content:
            return
        try:
            data = json.loads(flow.request.get_text(strict=False))
        except Exception:
            return
        if host in SHIELD_HOSTS and isinstance(data, dict):
            data, hits = self.shield.redact_request(SHIELD_HOSTS[host], data)
            flow.request.text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            flow.metadata["shield"] = {"provider": SHIELD_HOSTS[host], "redacted": hits, "restored": 0}
        elif isinstance(data, dict) and data.get("jsonrpc") == "2.0" and data.get("method") == "tools/call":
            flow.metadata["mcp_call"] = True

    # ---------- HTTP (the decrypted case) ----------
    def responseheaders(self, flow: http.HTTPFlow):
        sh = flow.metadata.get("shield")
        if sh and sh["provider"] == "anthropic" and "event-stream" in flow.response.headers.get("content-type", ""):
            chunks: list[bytes] = []
            flow.metadata["sse"] = chunks
            restorer = AnthropicSSERestorer(self.shield, lambda n: sh.__setitem__("restored", sh["restored"] + n))

            def restore_and_tee(chunk: bytes) -> bytes:
                try:
                    out = restorer(chunk)
                except Exception as e:  # never break the agent's stream because of Shield
                    ctx.log.error(f"ambient shield: SSE rewrite failed, passing through: {e!r}")
                    out = chunk
                chunks.append(out)
                # An empty bytes object would be written as an empty chunk, which
                # HTTP/1.1 chunked encoding reads as end-of-response. Send nothing instead.
                return out if out else []
            flow.response.stream = restore_and_tee
            return
        if flow.metadata.get("mcp_call"):
            return  # buffer MCP replies so Shield can redact them in response()
        if "event-stream" in flow.response.headers.get("content-type", ""):
            # Keep SSE (LLM token streams) streaming to the app while we tee a copy.
            chunks: list[bytes] = []
            flow.metadata["sse"] = chunks

            def tee(chunk: bytes) -> bytes:
                chunks.append(chunk)
                return chunk
            flow.response.stream = tee
        elif int(flow.response.headers.get("content-length", "0") or 0) > 20 * 1024 * 1024:
            # Big downloads/video: stream them, keep only the size.
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow):
        sh = flow.metadata.get("shield")
        if sh and "sse" not in flow.metadata and flow.response.content:
            try:
                data = json.loads(flow.response.get_text(strict=False))
                data, n = self.shield.restore_json(data)
                if n:
                    flow.response.text = json.dumps(data, ensure_ascii=False)
                    sh["restored"] += n
            except Exception:
                pass
        if flow.metadata.get("mcp_call") and flow.response.content and ctx.options.ambient_shield:
            self._shield_mcp(flow)
        self._record_http(flow)

    def _shield_mcp(self, flow: http.HTTPFlow):
        text = flow.response.get_text(strict=False) or ""
        hits: list[dict] = []
        if "event-stream" in flow.response.headers.get("content-type", ""):
            out = []
            for line in text.split("\n"):
                if line.startswith("data:"):
                    try:
                        msg, h = self.shield.redact_mcp_result(json.loads(line[5:]))
                        hits += h
                        line = "data: " + json.dumps(msg, ensure_ascii=False)
                    except Exception:
                        pass
                out.append(line)
            new = "\n".join(out)
        else:
            try:
                msg, hits = self.shield.redact_mcp_result(json.loads(text))
                new = json.dumps(msg, ensure_ascii=False)
            except Exception:
                return
        if hits:
            flow.response.text = new
            flow.metadata["shield"] = {"provider": "mcp", "redacted": hits, "restored": 0}

    def error(self, flow: http.HTTPFlow):
        if not flow.response:
            self._record_http(flow, note=str(flow.error))

    def _record_http(self, flow: http.HTTPFlow, note: str | None = None):
        lim = ctx.options.ambient_body_limit
        req, resp = flow.request, flow.response
        host = req.pretty_host
        req_text, req_len = body(req, lim)
        if resp is not None and "sse" in flow.metadata:
            raw = b"".join(flow.metadata["sse"])
            resp_text, resp_len = raw[:lim].decode("utf-8", "replace"), len(raw)
        elif resp is not None and resp.stream is True:
            resp_text, resp_len = "[streamed, not stored]", int(resp.headers.get("content-length", "0") or 0)
        elif resp is not None:
            resp_text, resp_len = body(resp, lim)
        else:
            resp_text, resp_len = "", 0

        who = self._who(flow.client_conn)
        self.rec.on_http(who["app"], host, req.path, req_text, resp_text)
        # Model providers' own APIs are left out of the activity catalog: agent conversations aren't app activity.
        if not provider_api(host, req.path):
            try:
                raw = req.get_content(strict=False) or b""
            except Exception:
                raw = req.raw_content or b""
            self.events.http(who["app"], host, req.method, req.path, req.headers.get("content-type", ""),
                             raw[:512 * 1024], {k.lower(): v for k, v in req.headers.items()},
                             resp_text if "html" in (resp.headers.get("content-type", "") if resp else "") else "",
                             resp.status_code if resp else None)
            # Content pushed over a server-sent event stream (a chat reply arriving) is incoming activity.
            if resp is not None and "event-stream" in resp.headers.get("content-type", ""):
                self.events.sse(who["app"], host, req.path, resp_text)
        if sensitive(host, req.path):
            req_text = NOT_STORED                 # never keep credentials on disk: not what was sent,
            if resp_text:
                resp_text = NOT_STORED            # nor the session or card details that came back
        findings = detect.scan(req_text, "request.body", host)
        for k, v in req.headers.items():
            findings += detect.scan(v, f"request.header.{k}", host)
        findings += detect.scan(req.path, "request.url", host)
        resp_ctype = resp.headers.get("content-type", "") if resp else ""

        # Only this stored copy is masked (Store.add replaces credential header values and credential
        # parameters in the URL); the flow itself goes out as the app sent it.
        self.store.add({
            "ts": req.timestamp_start, "kind": "http", "decrypted": 1,
            "duration": (resp.timestamp_end - req.timestamp_start) if resp and resp.timestamp_end else None,
            **self._who(flow.client_conn),
            "server": f"{flow.server_conn.address}", "host": host,
            "method": req.method, "url": req.pretty_url, "path": req.path,
            "status": resp.status_code if resp else None,
            "req_bytes": req_len, "resp_bytes": resp_len,
            "req_ctype": req.headers.get("content-type", ""), "resp_ctype": resp_ctype,
            "req_headers": [[k, v] for k, v in req.headers.items(multi=True)],
            "resp_headers": [[k, v] for k, v in resp.headers.items(multi=True)] if resp else None,
            "req_body": req_text, "resp_body": resp_text,
            "llm": detect.llm_info(host, req.path, req_text, resp_text, resp_ctype),
            "mcp": detect.mcp_info(req_text),
            "findings": findings or None, "max_sev": worst(findings), "note": note,
            "shield": flow.metadata.get("shield") if (flow.metadata.get("shield") or {}).get("redacted")
                      or (flow.metadata.get("shield") or {}).get("restored") else None,
        })

    def websocket_message(self, flow: http.HTTPFlow):
        m = flow.websocket.messages[-1]
        text = m.text if m.is_text else f"[binary {len(m.content)} bytes]"
        host = flow.request.pretty_host
        app = self._who(flow.client_conn)["app"]
        obj = None
        if not m.is_text:
            dec = self.sockdec.get((flow.id, m.from_client))
            if dec is None:
                dec = self.sockdec[(flow.id, m.from_client)] = SocketDecoder()
            obj = dec.feed(m.content)
        self.rec.on_ws(app, host, flow.id, m.text if m.is_text else None, obj, m.from_client)
        if not provider_api(host, flow.request.path):        # e.g. a realtime model session: not app activity
            self.events.ws(app, host, flow.request.path, m.text if m.is_text else m.content, m.from_client, obj=obj)
        findings = detect.scan(text, "request.body.ws", host) if m.from_client else []
        lim = ctx.options.ambient_body_limit
        self.store.add({
            "ts": m.timestamp, "kind": "ws", "decrypted": 1, **self._who(flow.client_conn),
            "host": host, "path": flow.request.path, "method": "WS↑" if m.from_client else "WS↓",
            "req_bytes": len(m.content) if m.from_client else 0,
            "resp_bytes": 0 if m.from_client else len(m.content),
            "req_body": text[:lim] if m.from_client else None,
            "resp_body": None if m.from_client else text[:lim],
            "findings": findings or None, "max_sev": worst(findings),
        })
        flow.websocket.messages.clear()  # long-lived sockets would otherwise grow forever

    def websocket_end(self, flow: http.HTTPFlow):
        self.sockdec.pop((flow.id, True), None)
        self.sockdec.pop((flow.id, False), None)
        self.rec.on_ws_end(flow.request.pretty_host, flow.id)

    # ---------- everything that isn't decrypted HTTP ----------
    def _count(self, flow):
        # Count and drop message payloads so long-lived streams don't pile up in memory.
        m = flow.messages[-1]
        key = "up" if m.from_client else "down"
        flow.metadata[key] = flow.metadata.get(key, 0) + len(m.content)
        flow.metadata["n"] = flow.metadata.get("n", 0) + 1
        flow.messages.clear()
        # Traffic we can't read is still a signal: go look at what the app wrote locally.
        info = self.clients.get(flow.client_conn.id) or {}
        if info.get("app") and len(m.content) > 60:
            self.local.poke(info["app"], info.get("path") or "")

    def tcp_message(self, flow: tcp.TCPFlow):
        self._count(flow)

    def udp_message(self, flow: udp.UDPFlow):
        self._count(flow)

    def _raw_end(self, flow, kind: str):
        c = flow.client_conn
        opaque = (self.clients.get(c.id) or {}).get("opaque")
        host = opaque[0] if opaque else (
            flow.server_conn.sni or (flow.server_conn.address[0] if flow.server_conn.address else "?"))
        if opaque:
            note = f"encrypted, passed through: {opaque[1]}"
        elif flow.server_conn.address and flow.server_conn.address[1] in (443, 853, 993, 5223):
            note = f"{flow.metadata.get('n', 0)} {kind.upper()} packets on an encrypted port, protocol not parsed"
        else:
            note = f"{flow.metadata.get('n', 0)} {kind.upper()} messages, protocol not parsed"
        self.store.add({
            "ts": c.timestamp_start, "kind": "opaque" if opaque else kind, "decrypted": 0,
            "duration": time.time() - c.timestamp_start, **self._who(c),
            "server": f"{flow.server_conn.address}", "host": host,
            "req_bytes": flow.metadata.get("up", 0), "resp_bytes": flow.metadata.get("down", 0),
            "note": note,
        })

    def tcp_end(self, flow: tcp.TCPFlow):
        self._raw_end(flow, "tcp")

    def tcp_error(self, flow: tcp.TCPFlow):
        self._raw_end(flow, "tcp")

    def udp_end(self, flow: udp.UDPFlow):
        self._raw_end(flow, "udp")

    def dns_response(self, flow: dns.DNSFlow):
        q = flow.request.questions[0].name if flow.request.questions else "?"
        ans = [str(a) for a in (flow.response.answers if flow.response else [])][:10]
        self.store.add({"ts": flow.request.timestamp, "kind": "dns", "decrypted": 1,
                        **self._who(flow.client_conn), "host": q, "resp_body": "\n".join(ans)})


addons = [AmbientAddon()]
