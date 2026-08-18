#!/bin/sh
OVERLAY="$HOME/.config/i3/window_names.json"
new_name=$(dmenu -p "Rename:")
[ -z "$new_name" ] && exit 0
xid=$(xprop -root _NET_ACTIVE_WINDOW | sed 's/.*window id # \(0x[0-9a-fA-F]*\).*/\1/')
[ -z "$xid" ] && exit 1
dec=$((xid))
tmp=$(mktemp)
jq --arg k "$dec" --arg v "$new_name" '.[$k]=$v' "$OVERLAY" > "$tmp" 2>/dev/null && mv "$tmp" "$OVERLAY" || rm -f "$tmp"
i3-msg nop
