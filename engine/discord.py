"""Discord gateway decoding: zstd-stream frames carrying ETF (Erlang External Term Format).

The desktop client connects to gateway*.discord.gg with `encoding=etf&compress=zstd-stream`:
one zstd stream per socket, each server frame a flushed chunk of it, each chunk one ETF
payload {op, d, s, t}. We only need dispatches (op 0): READY (who I am, my DMs and
channels) and MESSAGE_CREATE.
"""
from __future__ import annotations

import os
import struct


class ETFError(ValueError):
    pass


def etf_decode(buf: bytes):
    if not buf or buf[0] != 131:
        raise ETFError("not ETF")
    val, _ = _term(buf, 1)
    return val


def _term(b: bytes, i: int):
    tag = b[i]; i += 1
    if tag == 97:                                   # SMALL_INTEGER
        return b[i], i + 1
    if tag == 98:                                   # INTEGER
        return struct.unpack(">i", b[i:i + 4])[0], i + 4
    if tag == 70:                                   # NEW_FLOAT
        return struct.unpack(">d", b[i:i + 8])[0], i + 8
    if tag in (100, 118):                           # ATOM, ATOM_UTF8 (2-byte len)
        n = struct.unpack(">H", b[i:i + 2])[0]; i += 2
        return _atom(b[i:i + n].decode("utf-8", "replace")), i + n
    if tag in (115, 119):                           # SMALL_ATOM, SMALL_ATOM_UTF8
        n = b[i]; i += 1
        return _atom(b[i:i + n].decode("utf-8", "replace")), i + n
    if tag == 109:                                  # BINARY
        n = struct.unpack(">I", b[i:i + 4])[0]; i += 4
        return b[i:i + n].decode("utf-8", "replace"), i + n
    if tag == 107:                                  # STRING (list of bytes)
        n = struct.unpack(">H", b[i:i + 2])[0]; i += 2
        return b[i:i + n].decode("latin-1"), i + n
    if tag == 106:                                  # NIL
        return [], i
    if tag == 108:                                  # LIST
        n = struct.unpack(">I", b[i:i + 4])[0]; i += 4
        out = []
        for _ in range(n):
            v, i = _term(b, i); out.append(v)
        tail, i = _term(b, i)                       # proper lists end in NIL
        return out, i
    if tag in (104, 105):                           # SMALL_TUPLE, LARGE_TUPLE
        if tag == 104:
            n = b[i]; i += 1
        else:
            n = struct.unpack(">I", b[i:i + 4])[0]; i += 4
        out = []
        for _ in range(n):
            v, i = _term(b, i); out.append(v)
        return tuple(out), i
    if tag == 116:                                  # MAP
        n = struct.unpack(">I", b[i:i + 4])[0]; i += 4
        out = {}
        for _ in range(n):
            k, i = _term(b, i); v, i = _term(b, i)
            out[k if isinstance(k, (str, int)) else str(k)] = v
        return out, i
    if tag in (110, 111):                           # SMALL_BIG, LARGE_BIG (snowflakes)
        if tag == 110:
            n = b[i]; i += 1
        else:
            n = struct.unpack(">I", b[i:i + 4])[0]; i += 4
        sign = b[i]; i += 1
        v = int.from_bytes(b[i:i + n], "little")
        return (-v if sign else v), i + n
    raise ETFError(f"unsupported ETF tag {tag}")


def _atom(s: str):
    return {"nil": None, "true": True, "false": False}.get(s, s)


DUMP = os.environ.get("AMBIENT_DUMP_DISCORD")   # debug: directory to save the first raw frames into


class Gateway:
    """One per socket: holds the zstd stream state."""
    n_sockets = 0

    def __init__(self):
        import zstandard
        self.z = zstandard.ZstdDecompressor().decompressobj()
        self.broken = False
        self.n = 0
        Gateway.n_sockets += 1
        self.sock = Gateway.n_sockets

    def feed(self, frame: bytes):
        self.n += 1
        if DUMP and self.n <= 40:
            os.makedirs(DUMP, exist_ok=True)
            with open(os.path.join(DUMP, f"s{self.sock}-{self.n:03d}.bin"), "wb") as f:
                f.write(frame)
        if self.broken:
            return None
        try:
            raw = self.z.decompress(frame)
        except Exception as e:
            self.broken = True          # lost sync (e.g. we joined mid-stream): ignore this socket
            print(f"ambient discord: zstd {e!r} on frame {self.n}")
            return None
        if not raw:
            return None
        try:
            return etf_decode(raw)
        except Exception as e:
            print(f"ambient discord: etf {e!r} (frame {self.n}, {len(raw)} bytes, starts {raw[:12].hex()})")
            return None
