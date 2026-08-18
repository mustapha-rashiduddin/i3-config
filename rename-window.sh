#!/bin/sh
OVERLAY="$HOME/.config/i3/window_names.json"
PIPE="$HOME/.config/i3/.rename_pipe"
xid=$(xprop -root _NET_ACTIVE_WINDOW | sed 's/.*window id # \(0x[0-9a-fA-F]*\).*/\1/')
[ -z "$xid" ] && exit 1
dec=$((xid))
new_name=$(dmenu -p "Rename:")
[ -z "$new_name" ] && exit 0
printf '%s\t%s\n' "$dec" "$new_name" > "$PIPE"
