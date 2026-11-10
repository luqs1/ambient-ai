#!/usr/bin/env bash
# Real mouse click at screen point (x, y) on macOS. usage: scripts/click.sh X Y
osascript -l JavaScript -e "
ObjC.import('CoreGraphics');
var p = \$.CGPointMake($1, $2);
var m = \$.CGEventCreateMouseEvent(\$(), \$.kCGEventMouseMoved, p, \$.kCGMouseButtonLeft);
var d = \$.CGEventCreateMouseEvent(\$(), \$.kCGEventLeftMouseDown, p, \$.kCGMouseButtonLeft);
var u = \$.CGEventCreateMouseEvent(\$(), \$.kCGEventLeftMouseUp, p, \$.kCGMouseButtonLeft);
\$.CGEventPost(\$.kCGHIDEventTap, m); delay(0.15);
\$.CGEventPost(\$.kCGHIDEventTap, d); delay(0.08); \$.CGEventPost(\$.kCGHIDEventTap, u); 'clicked'"
