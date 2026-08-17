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


def get_focused_con_id():
    try:
        tree = json.loads(subprocess.check_output(
            ["i3-msg", "-t", "get_tree"], text=True, timeout=3))
    except Exception:
        return None
    node = find_focused(tree)
    if node and node.get("window"):
        return str(node["id"])
    return None


def find_focused(node):
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result = find_focused(child)
        if result:
            return result
    return None


def get_current_name(con_id):
    overlay = load_overlay()
    if con_id in overlay:
        return overlay[con_id]
    try:
        tree = json.loads(subprocess.check_output(
            ["i3-msg", "-t", "get_tree"], text=True, timeout=3))
    except Exception:
        return ""
    node = find_focused(tree)
    if not node:
        return ""
    props = node.get("window_properties") or {}
    return props.get("class", "")


def main():
    con_id = get_focused_con_id()
    if not con_id:
        sys.exit(1)

    current = get_current_name(con_id)

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
    subprocess.run(["i3-msg", "workspace", "current"])


if __name__ == "__main__":
    main()
