#!/usr/bin/env bash
# One real agent run, on test content only.
# Switches every trigger off except the GitHub-issue one, restarts capture with the real agent
# (Claude Code), creates a test issue with the GitHub CLI, waits for the agent's answer, then
# restores the stand-in agent and the triggers' previous on/off state.
#   tests/real_agent_once.sh
BASE="http://127.0.0.1:8787"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
post() { curl -s -X POST "$BASE$1" -H 'content-type: application/json' -H 'X-Ambient: 1' -d "$2"; }

BEFORE=$(curl -s "$BASE/api/triggers" | jq -c '[.triggers[] | {id, enabled}]')
KEEP=$(curl -s "$BASE/api/triggers" | jq -r '[.triggers[] | select(.name | test("Summarise new issues"))][0].id')
echo "keeping trigger $KEEP on; others off for this run"
for row in $(curl -s "$BASE/api/triggers" | jq -c '.triggers[]' | jq -c 'del(.kinds, .statement, .fired, .last_fired)' | base64); do
  T=$(echo "$row" | base64 -d)
  ID=$(echo "$T" | jq -r .id)
  if [ "$ID" = "$KEEP" ]; then ON=true; else ON=false; fi
  post /api/triggers "$(echo "$T" | jq -c --argjson on "$ON" '.enabled = $on')" >/dev/null
done

AMBIENT_REAL_AGENT=1 "$HERE/scripts/live.sh" restart
sleep 4
URL=$(gh issue create -R luqs1/ambient-ai --title "Nightly test 3: settings page crashes when the trigger list is empty" \
  --body "Steps: open Ambient with no triggers, click the Triggers heading. Expected: an empty state. Actual: the page goes blank. Test issue created by the overnight run, safe to close." 2>&1 | tail -1)
echo "issue: $URL"
for _ in $(seq 1 45); do
  ST=$(curl -s "$BASE/api/moments?limit=1" | jq -r '.[0].action_status // ""')
  [ "$ST" = "ok" ] || [ "$ST" = "error" ] && break
  sleep 2
done
curl -s "$BASE/api/moments?limit=1" | jq -c '.[] | {id, app, title, action, action_status, result: ((.action_result // "") | .[0:400])}'
gh issue close "$URL" >/dev/null 2>&1

"$HERE/scripts/live.sh" restart
sleep 3
for row in $(curl -s "$BASE/api/triggers" | jq -c '.triggers[]' | jq -c 'del(.kinds, .statement, .fired, .last_fired)' | base64); do
  T=$(echo "$row" | base64 -d)
  ID=$(echo "$T" | jq -r .id)
  ON=$(echo "$BEFORE" | jq -c --argjson id "$ID" '[.[] | select(.id == $id)][0].enabled == 1')
  post /api/triggers "$(echo "$T" | jq -c --argjson on "$ON" '.enabled = $on')" >/dev/null
done
echo "restored: $(curl -s "$BASE/api/triggers" | jq -c '[.triggers[] | {id, enabled}]')"
