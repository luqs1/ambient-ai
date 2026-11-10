#!/usr/bin/env bash
# Sends known traffic through a running `./ambient.sh proxy 18080 -- --set ambient_ui=18787`
# and prints what ambient recorded.
set -u
CA=~/.mitmproxy/mitmproxy-ca-cert.pem
P=(-x http://127.0.0.1:18080 -s -o /dev/null -w "%{http_code}\n" -m 15)
echo "1 decrypted POST w/ secrets:"; curl "${P[@]}" --cacert $CA https://httpbin.org/post -H 'content-type: application/json' \
  -d '{"note":"my email is jane.doe@example.com","aws":"AKIAIOSFODNN7EXAMPLE","card":"4111 1111 1111 1111","password":"hunter22"}'
echo "2 fake anthropic call:"; curl "${P[@]}" --cacert $CA https://api.anthropic.com/v1/messages -H 'x-api-key: sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE' \
  -H 'content-type: application/json' -d '{"model":"claude-sonnet-5-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
echo "3 anthropic key sent to wrong host:"; curl "${P[@]}" --cacert $CA https://httpbin.org/get -H 'Authorization: Bearer sk-ant-api03-FAKEFAKEFAKEFAKEFAKEFAKE'
echo "4 client that doesn't trust our CA (1st fails, 2nd passes through):"; curl "${P[@]}" https://example.com; curl "${P[@]}" https://example.com
echo "5 MCP-style JSON-RPC:"; curl "${P[@]}" --cacert $CA https://httpbin.org/post -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"~/.ssh/id_rsa"}}}'
python3 -c "
import urllib.request,ssl
c=ssl.create_default_context(cafile='$(echo $CA)')
o=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:18080'}),urllib.request.HTTPSHandler(context=c))
print('6 python:',o.open('https://httpbin.org/headers').status)"
sleep 1
curl -s localhost:18787/api/flows | python3 -c "
import json,sys
for r in reversed(json.load(sys.stdin)):
    print(' | '.join(str(x) for x in (r['id'],r['app'],r['kind'],'plain' if r['decrypted'] else 'ENC',r['host'],r['path'],r['status'],r['req_bytes'],r['resp_bytes'],r['max_sev'],(r['llm'] or '')[:110],r['mcp'] or '',(r['note'] or '')[:80])))"
