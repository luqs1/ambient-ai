#!/usr/bin/env bash
# Live check of the user's own example: "whenever I create a calendar event in Google".
# Uses "show me once" to learn what Google Calendar sends when an event is saved (no
# Calendar-specific code exists), binds a trigger, saves a second event and expects it to fire.
# Drives the Zen browser with keystrokes (macOS). Creates events titled "Ambient night test …"
# with no guests at 3am; delete them afterwards.
#   tests/calendar_live.sh [port]
PORT="${1:-8787}"
BASE="http://127.0.0.1:$PORT"

post() {
  curl -s -X POST "$BASE$1" -H 'content-type: application/json' -H 'X-Ambient: 1' -d "$2"
}

save_event() {   # $1 = title suffix, $2 = start HHMM (UTC) on 2026-11-14
  open -a Zen "https://calendar.google.com/calendar/render?action=TEMPLATE&text=Ambient+night+test+$1+(safe+to+delete)&dates=20261114T0${2}00Z/20261114T0${2}00Z&details=Created+by+the+overnight+test+run.+No+guests."
  sleep 9
  osascript -e 'tell application "Zen" to activate' -e 'delay 0.6' -e 'tell application "System Events" to keystroke "s" using command down'
  sleep 6
}

TOK=$(post /api/record/start '{"seconds":60}' | jq -r .token)
echo "recording: $TOK"
save_event two 320
REC=$(post "/api/record/$TOK/stop" '{}')
echo "$REC" | jq -c '.kinds[] | select(.app | test("Calendar")) | {app, label, klass, n, new, example: ((.example // "") | .[0:60]), sig}'
SIG=$(echo "$REC" | jq -r '[.kinds[] | select((.app | test("Calendar")) and (.sig | endswith("+text")))][0].sig')
echo "binding trigger to: $SIG"
BODY=$(jq -n --arg s "$SIG" '{text:"I create a calendar event in Google", sigs:[$s], semantic:false, auto:true, action:"ask", instruction:"Check my calendar for clashes with this event and tell me what to prepare.", name:"[night test] Prep for new calendar events"}')
post /api/triggers "$BODY"
echo
save_event three 340
curl -s "$BASE/api/moments?limit=1" | jq -c '.[] | {id, app, title, text, action, action_status, result: ((.action_result // "") | .[0:120])}'
