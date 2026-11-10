#!/usr/bin/env bash
# Live check: a trigger bound (by "show me once") to starring ONE repository fires when a second,
# different repository of yours is starred, and moves to the generalised kind.
# Stars one of your own repos with the GitHub CLI, then removes the star and switches the trigger off.
#   tests/star_follows_kind_live.sh [trigger id]
B="http://127.0.0.1:8787"; TID="${1:-8}"
post() { curl -s -X POST "$B$1" -H 'content-type: application/json' -H 'X-Ambient: 1' -d "$2"; }
set_on() { post /api/triggers "$(curl -s $B/api/triggers | jq -c --argjson id "$TID" --argjson on "$1" '.triggers[] | select(.id==$id) | del(.kinds,.statement,.fired,.last_fired) | .enabled=$on')" >/dev/null; }
OWNER=$(gh api user -q .login)
OTHER=$(gh repo list "$OWNER" --limit 30 --json nameWithOwner -q '[.[] | select(.nameWithOwner | endswith("/ambient-ai") | not)][0].nameWithOwner')
if gh api "user/starred/$OTHER" >/dev/null 2>&1; then echo "already starred, pick another"; exit 1; fi
set_on true
BEFORE=$(curl -s "$B/api/moments?limit=1" | jq '.[0].id // 0')
gh api -X PUT "user/starred/$OTHER" >/dev/null
sleep 5
echo "fired: $(curl -s "$B/api/moments?limit=3" | jq -c --argjson b "$BEFORE" '[.[] | select(.id > $b) | {app, sig, action_status}]')"
echo "trigger now bound to: $(curl -s $B/api/triggers | jq -c --argjson id "$TID" '.triggers[] | select(.id==$id) | [.kinds[] | {app, label, sig}]')"
gh api -X DELETE "user/starred/$OTHER" >/dev/null && echo "star removed"
set_on false
