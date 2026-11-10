#!/usr/bin/env bash
# Print what the trigger builder would suggest for a set of plain sentences.
#   tests/suggest_check.sh [port]
P=${1:-8787}
for s in "when someone asks me for a file" "I promise to do something in a message" "a meeting ends" "I search for something on Google" "whenever I open a pull request on GitHub" "I push code" "I create an issue on GitHub" "someone messages me on WhatsApp" "I send a Slack message" "someone reports a bug" "I create a calendar event in Google"; do
  echo "== $s"
  curl -s -X POST http://127.0.0.1:$P/api/triggers/suggest -H 'content-type: application/json' -H 'X-Ambient: 1' -d "{\"text\":\"$s\"}" \
   | jq -r '(.kinds[] | "   \(if .picked then "✓" else " " end) \(.score)  \(.app) · \(.label)  [\(.klass)]"), "   sure=\(.sure) about_content=\(.about_content)"' | head -6
done
