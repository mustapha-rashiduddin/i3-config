#!/usr/bin/env python3
import json
import os
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


def find_focused(node):
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result = find_focused(child)
        if result:
            return result
    return None


def main():
    try:
        tree = json.loads(subprocess.check_output(
            ["i3-msg", "-t", "get_tree"], text=True, timeout=3))
    except Exception:
        sys.exit(1)
    node = find_focused(tree)
    if not node or not node.get("window"):
        sys.exit(1)
    con_id = str(node["id"])

    overlay = load_overlay()
    current = overlay.get(con_id)
    if not current:
        props = node.get("window_properties") or {}
        current = props.get("class", "")

    try:
        cmd = ["dmenu", "-p", "Rename window:",
               "-nb", "#1c1c1c", "-nf", "#00ff00", "-sb", "#285577",
               "-sf", "#ffffff"]
        if current:
            cmd += ["-l", "1"]
        result = subprocess.run(
            cmd,
            input=current if current else None,
            capture_output=True, text=True, timeout=30)
    except Exception:
        sys.exit(1)

    new_name = result.stdout.strip()
    if not new_name:
        sys.exit(0)

    overlay = load_overlay()
    overlay[con_id] = new_name
    save_overlay(overlay)
    subprocess.run(["i3-msg", "nop"])


if __name__ == "__main__":
    main()
