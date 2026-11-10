#!/usr/bin/env bash
# Live check of "show me once": record while doing an action Ambient has never seen
# (starring a repo with the GitHub CLI), bind a trigger to what was recorded, repeat the
# action, and confirm the trigger fires. Reversible: the repo is unstarred at the end.
#   tests/show_me_once.sh [port]   (needs the engine running with capture, and `gh`)
PORT="${1:-8787}"
BASE="http://127.0.0.1:$PORT"
REPO="luqs1/ambient-ai"

post() {
  curl -s -X POST "$BASE$1" -H 'content-type: application/json' -H 'X-Ambient: 1' -d "$2"
}

TOK=$(post /api/record/start '{"seconds":60}' | jq -r .token)
echo "recording: $TOK"
gh api -X PUT "/user/starred/$REPO" >/dev/null 2>&1
sleep 4
REC=$(post "/api/record/$TOK/stop" '{}')
echo "$REC" | jq -c '.kinds[] | {app, label, klass, n, new, sig}'
SIG=$(echo "$REC" | jq -r '[.kinds[] | select(.app=="GitHub CLI")][0].sig')
echo "binding trigger to: $SIG"
BODY=$(jq -n --arg s "$SIG" '{text:"I star a repository on GitHub", sigs:[$s], semantic:false, auto:true, action:"ask", instruction:"Add this repository to my reading list with one line on why it matters.", name:"[night test] Note repos I star"}')
post /api/triggers "$BODY"
echo
gh api -X DELETE "/user/starred/$REPO" >/dev/null 2>&1
sleep 1
gh api -X PUT "/user/starred/$REPO" >/dev/null 2>&1
sleep 5
curl -s "$BASE/api/moments?limit=1" | jq -c '.[] | {id, app, title, text, action, action_status, result: ((.action_result // "") | .[0:100])}'
gh api -X DELETE "/user/starred/$REPO" >/dev/null 2>&1
echo "unstarred again"
