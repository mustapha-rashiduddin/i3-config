#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys

OVERLAY = os.path.expanduser("~/.config/i3/window_names.json")


def load_overlay():
    try:
        with open(OVERLAY) as f:
            return json.load(f)
    except Exception:
        return {}


def save_overlay(data):
    try:
        with open(OVERLAY, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def run(cmd):
    try:
        return subprocess.check_output(cmd, text=True, timeout=3).strip()
    except Exception:
        return ""


def main():
    raw = run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    m = re.search(r"window id # (0x[0-9a-fA-F]+)", raw)
    if not m:
        sys.exit(1)
    xid = str(int(m.group(1), 16))

    r = run(["xprop", "-id", xid, "WM_CLASS"])
    wm_class = ""
    if "WM_CLASS" in r:
        parts = r.split("=", 1)[1].strip().strip('"').split('", "')
        if len(parts) >= 2:
            wm_class = parts[1]
        elif parts:
            wm_class = parts[0]

    overlay = load_overlay()
    current = overlay.get(xid, wm_class)

    try:
        result = subprocess.run(
            ["dmenu", "-p", "Rename:"],
            capture_output=True, text=True, timeout=30)
    except Exception:
        sys.exit(1)

    new_name = result.stdout.strip()
    if not new_name:
        sys.exit(0)

    overlay = load_overlay()
    overlay[xid] = new_name
    save_overlay(overlay)
    subprocess.run(["i3-msg", "nop"])


if __name__ == "__main__":
    main()
