#!/usr/bin/env bash
# Screenshot the Ambient app window (macOS). usage: scripts/shot.sh out.png [process-name]
OUT="$1"; PROC="${2:-ambient}"
B=$(osascript -e "tell application \"System Events\" to tell (first process whose name is \"$PROC\") to set frontmost to true" -e "delay 0.4" -e "tell application \"System Events\" to tell (first process whose name is \"$PROC\") to get {position, size} of window 1" | tr -d ' ')
IFS=, read -r X Y W H <<< "$B"
screencapture -x -R"$X,$Y,$W,$H" "$OUT" && echo "$OUT ($W x $H)"
