"""Decode binary WebSocket frames from any app into Python objects, without app-specific code.

Many apps compress one long stream across frames (Discord: zstd-stream or zlib-stream) and/or
use a binary encoding (ETF, MessagePack-like). One SocketDecoder per socket direction keeps the
stream state; feed() returns a decoded object, or None when the frame can't be read (then the
caller falls back to pulling printable strings out of the raw bytes).
"""
from __future__ import annotations

import json
import zlib

from engine.discord import etf_decode

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class SocketDecoder:
    def __init__(self):
        self.mode: str | None = None     # None = undecided, then "zstd" | "zlib" | "raw"
        self.z = None
        self.buf = b""
        self.frames = 0
        self.broken = False

    def _decide(self, frame: bytes):
        if frame.startswith(ZSTD_MAGIC):
            try:
                import zstandard
                self.z = zstandard.ZstdDecompressor().decompressobj()
                self.mode = "zstd"
                return
            except Exception:
                pass
        if len(frame) >= 2 and frame[0] == 0x78 and frame[1] in (0x01, 0x5E, 0x9C, 0xDA):
            self.z = zlib.decompressobj()
            self.mode = "zlib"
            return
        self.mode = "raw"

    def feed(self, frame: bytes):
        self.frames += 1
        if self.broken or not frame:
            return None
        if self.mode is None:
            self._decide(frame)
        raw = frame
        if self.mode in ("zstd", "zlib"):
            try:
                raw = self.z.decompress(frame)
            except Exception:
                self.broken = True       # joined mid-stream or not what it looked like: give up quietly
                return None
            if not raw:
                return None
            if self.mode == "zlib":
                # zlib-stream messages end at a sync flush; a message may span frames
                self.buf += raw
                if not frame.endswith(b"\x00\x00\xff\xff"):
                    return None
                raw, self.buf = self.buf, b""
        return decode_payload(raw)


def decode_payload(raw: bytes):
    if not raw:
        return None
    if raw[0] == 131:                    # Erlang External Term Format (Discord desktop)
        try:
            return etf_decode(raw)
        except Exception:
            return None
    if raw[:1] in (b"{", b"["):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None
