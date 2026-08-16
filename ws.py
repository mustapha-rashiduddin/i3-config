#!/usr/bin/env python3
import json
import subprocess
import time


def run(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def get_workspaces():
    try:
        return json.loads(run(["i3-msg", "-t", "get_workspaces"]))
    except Exception:
        return []


def get_tree():
    try:
        return json.loads(run(["i3-msg", "-t", "get_tree"]))
    except Exception:
        return {}


def collect_windows(node):
    out = []
    props = node.get("window_properties") or {}
    if props.get("class"):
        out.append(props["class"])
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        out.extend(collect_windows(child))
    return out


def walk_workspaces(node):
    found = []
    if node.get("type") == "workspace":
        found.append(node)
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found.extend(walk_workspaces(child))
    return found


def apps_by_ws_name():
    result = {}
    for node in walk_workspaces(get_tree()):
        name = node.get("name", "")
        if name == "__i3_scratch":
            continue
        apps = []
        for a in collect_windows(node):
            if a not in apps:
                apps.append(a)
        result[name] = apps
    return result


def render():
    apps = apps_by_ws_name()
    out = []
    for ws in get_workspaces():
        name = ws.get("name", "")
        app_list = apps.get(name, [])
        label = f"{name}:{','.join(app_list)}" if app_list else name
        out.append({
            "id": ws.get("id", 0),
            "num": ws.get("num", -1),
            "name": label,
            "visible": ws.get("visible", False),
            "focused": ws.get("focused", False),
            "output": ws.get("output", "primary"),
            "urgent": ws.get("urgent", False),
        })
    print(json.dumps(out, ensure_ascii=False), flush=True)


def main():
    last_render = 0.0
    while True:
        try:
            render()
            last_render = time.time()
            proc = subprocess.Popen(
                ["i3-msg", "-t", "subscribe", "-m",
                 '["workspace","output","window"]'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            while proc.poll() is None:
                line = proc.stdout.readline()
                if line and time.time() - last_render >= 0.2:
                    render()
                    last_render = time.time()
            proc.wait()
        except Exception:
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
