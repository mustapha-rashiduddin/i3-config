#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import time
from select import select

CHROME_CFG = os.path.expanduser("~/.config/google-chrome")
MARKER = os.path.expanduser("~/.config/i3/.chrome-launched")
MARKER_WINDOW = 6.0
_prof_cache = {}
_win_profiles = {}

ROW_TOP = ["a", "s", "d", "f", "j", "k", "l", ";"]
ROW_BOTTOM = ["z", "x", "c", "v", "m", ",", ".", "/"]
ROWS = {"top": ROW_TOP, "bottom": ROW_BOTTOM}

LEFT_HAND = set(ROW_TOP[:4] + ROW_BOTTOM[:4])

CHAR_W = 12
PAD_W = 11
ELL = "…"


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


def _short(label):
    return label.split("@", 1)[0]


def _profiles():
    key_files = [os.path.join(CHROME_CFG, "Local State")]
    mtime = 0.0
    try:
        mtime = max(os.path.getmtime(f) for f in key_files)
    except Exception:
        pass
    if _prof_cache.get("mtime") == mtime:
        return _prof_cache["map"]
    mapping = {}
    try:
        state = json.load(open(os.path.join(CHROME_CFG, "Local State")))
        dirs = (state.get("profile") or {}).get("info_cache") or {}
        for d in dirs:
            prefs = os.path.join(CHROME_CFG, d, "Preferences")
            email = None
            try:
                p = json.load(open(prefs))
                accs = p.get("account_info") or []
                if accs:
                    email = _short(accs[0].get("email") or "")
            except Exception:
                pass
            mapping[d] = email or _short(dirs[d].get("name") or "") or d
    except Exception:
        pass
    _prof_cache["mtime"] = mtime
    _prof_cache["map"] = mapping
    return mapping


def marker_directory():
    try:
        if time.time() - os.path.getmtime(MARKER) <= MARKER_WINDOW:
            with open(MARKER) as f:
                d = f.read().strip()
            return d or None
    except Exception:
        pass
    return None


def chrome_label(xid):
    if xid in _win_profiles:
        return _win_profiles[xid]
    label = window_profile(xid) or "Google-chrome"
    _win_profiles[xid] = label
    return label


def window_profile(xid):
    r = run(["xprop", "-id", str(xid), "_NET_WM_PID"])
    if "_NET_WM_PID" not in r:
        return None
    try:
        pid = int(r.split("=", 1)[1].strip())
    except Exception:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="replace").replace("\0", " ")
    except Exception:
        return None
    m = re.search(r"--user-data-dir=.*?/(Default|Profile \d+)(?:\s|$)", cmd)
    if not m:
        m = re.search(r"--profile-directory=(Default|Profile \d+)(?:\s|$)", cmd)
    directory = m.group(1) if m else "Default"
    return _profiles().get(directory, "Google-chrome")


def collect_windows(node):
    out = []
    props = node.get("window_properties") or {}
    if props.get("class") == "Google-chrome":
        out.append(chrome_label(node.get("window")))
    elif props.get("class"):
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


def box_width(workspaces):
    w = 0
    for ws in workspaces:
        w = max(w, ws.get("rect", {}).get("width", 0))
    if not w:
        return 120
    n = 8
    sep = 9
    slack = 8
    return (w - n * sep - slack) // n


def truncate(label, width):
    max_chars = (width - 2 * PAD_W) // CHAR_W
    if len(label) <= max_chars:
        return label
    return label[:max_chars - 1] + ELL


def render(row_keys):
    workspaces = get_workspaces()
    ws_by_name = {ws.get("name"): ws for ws in workspaces}
    apps = apps_by_ws_name()
    width = box_width(workspaces)
    blocks = []
    for key in row_keys:
        ws = ws_by_name.get(key)
        app_list = apps.get(key, [])
        label = f"{key}:{','.join(app_list)}" if app_list else key
        text = " " + truncate(label, width) + " "
        if ws:
            if ws.get("focused"):
                bg, fg, bd = "#285577", "#ffffff", "#4c7899"
            elif ws.get("urgent"):
                bg, fg, bd = "#7a1010", "#ffffff", "#a00000"
            else:
                bg, fg, bd = ("#2e2e2e", "#00ff00" if key in LEFT_HAND else "#ff5252", "#4a4a4a")
        else:
            bg, fg, bd = ("#1c1c1c", "#00ff00" if key in LEFT_HAND else "#ff5252", "#333333")
        blocks.append({
            "full_text": text,
            "name": f"ws.{key}",
            "instance": key,
            "align": "left",
            "separator": False,
            "min_width": width,
            "background": bg,
            "color": fg,
        })
    return blocks


def handle_event(line):
    try:
        ev = json.loads(line)
    except Exception:
        return
    c = ev.get("container") or {}
    xid = c.get("window")
    if xid is None:
        return
    if ev.get("change") == "close":
        _win_profiles.pop(xid, None)
    elif ev.get("change") == "new":
        d = marker_directory()
        if d:
            _win_profiles[xid] = _profiles().get(d) or d


def handle_click(line):
    try:
        ev = json.loads(line)
    except Exception:
        return
    name = ev.get("name", "")
    if not name.startswith("ws.") or ev.get("button") not in (1, 2, 3):
        return
    run(["i3-msg", "workspace", name[3:]])


def main():
    row_keys = ROWS.get(sys.argv[1] if len(sys.argv) > 1 else "top", ROW_TOP)
    state = {"first": True}

    def emit():
        line = json.dumps(render(row_keys), ensure_ascii=False)
        if not state["first"]:
            line = "," + line
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        state["first"] = False

    print(json.dumps({"version": 1, "click_events": True}), flush=True)
    print("[", flush=True)
    emit()
    last_render = time.time()
    while True:
        try:
            proc = subprocess.Popen(
                ["i3-msg", "-t", "subscribe", "-m",
                 '["workspace","output","window"]'],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except Exception:
            time.sleep(1)
            continue
        last_event = time.time()
        dirty = False
        try:
            while proc.poll() is None:
                ready, _, _ = select([proc.stdout, sys.stdin], [], [], 0.1)
                now = time.time()
                if sys.stdin in ready:
                    line = sys.stdin.readline()
                    if line:
                        handle_click(line)
                        dirty = True
                        last_event = now
                if proc.stdout in ready:
                    line = proc.stdout.readline()
                    if line:
                        handle_event(line)
                        dirty = True
                        last_event = now
                if dirty and now - last_render >= 0.1:
                    emit()
                    last_render = now
                    dirty = False
                elif now - last_event >= 2.0:
                    emit()
                    last_render = now
                    last_event = now
        except Exception:
            time.sleep(1)
        proc.wait()


if __name__ == "__main__":
    main()
