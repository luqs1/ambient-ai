#!/usr/bin/env python3
"""Ambient's MCP server over stdio, for Claude Code, Claude Desktop and Codex.

A thin client over the running Ambient app's local API (default http://127.0.0.1:8787). Standard library
only: run it with any python3, no install. The tools and the MCP App view live in engine/mcp_app.py.

    python3 integrations/mcp/ambient_mcp.py [--url http://127.0.0.1:8787]
    AMBIENT_URL=http://127.0.0.1:18799 python3 integrations/mcp/ambient_mcp.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from engine.mcp_app import HttpClient, serve_stdio  # noqa: E402

if __name__ == "__main__":
    url = None
    if "--url" in sys.argv[1:]:
        i = sys.argv.index("--url")
        url = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    serve_stdio(HttpClient(url))
