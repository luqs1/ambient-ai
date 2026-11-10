#!/usr/bin/env python3
"""Stand-in agent for testing triggers without sending anything to a model.
Reads the prompt Ambient would hand to an agent and answers locally.
    AMBIENT_AGENT_CMD="python3 scripts/standin_agent.py"
"""
import re, sys
prompt = sys.stdin.read()
body = prompt.split("\n\n")
what = next((b.strip() for b in body[1:] if b.strip()), body[0].strip())[:160]
if "reply with exactly (nothing)" in prompt and not re.search(r"\b(i['’]?ll|i will|will do|i can|let me|on it)\b", prompt, re.I):
    print("(nothing)")
else:
    print(f"[stand-in agent · nothing left this device] I'd act on: “{what}”")
