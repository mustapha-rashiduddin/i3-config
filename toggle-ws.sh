#!/bin/sh
TOGGLE="$HOME/.config/i3/.show_ws"
if [ -s "$TOGGLE" ]; then
    : > "$TOGGLE"
else
    printf '1' > "$TOGGLE"
fi
i3-msg -t send_tick >/dev/null 2>&1