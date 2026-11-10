#!/usr/bin/env python3
"""Tiny QMP client: qmp.py <socket> <command> [json-arguments]. Prints the reply."""
import json, socket, sys

def main():
    path, cmd = sys.argv[1], sys.argv[2]
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    s = socket.socket(socket.AF_UNIX); s.settimeout(30); s.connect(path)
    f = s.makefile("rw")
    def call(c, a=None):
        m = {"execute": c}
        if a: m["arguments"] = a
        f.write(json.dumps(m) + "\n"); f.flush()
        while True:
            r = json.loads(f.readline())
            if "return" in r or "error" in r:
                return r
    f.readline()  # greeting
    call("qmp_capabilities")
    r = call(cmd, args)
    print(json.dumps(r))
    sys.exit(1 if "error" in r else 0)

main()
