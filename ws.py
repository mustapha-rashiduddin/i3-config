#!/usr/bin/env python3
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from select import select

CHROME_CFG = os.path.expanduser("~/.config/google-chrome")
MARKER = os.path.expanduser("~/.config/i3/.chrome-launched")
MARKER_WINDOW = 6.0
OVERLAY = os.path.expanduser("~/.config/i3/window_names.json")
_prof_cache = {}
_win_profiles = {}
_name_overlay = {}

ROW_KEYS = ["j", "k", "l", ";", "m", ",", ".", "/"]
GREEN = set(ROW_KEYS[:4])

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


def load_overlay():
    global _name_overlay, _overlay_mtime
    try:
        mtime = os.path.getmtime(OVERLAY)
    except Exception:
        mtime = 0.0
    if mtime == _overlay_mtime:
        return
    _overlay_mtime = mtime
    try:
        with open(OVERLAY) as f:
            _name_overlay = json.load(f)
    except Exception:
        _name_overlay = {}
_overlay_mtime = 0.0


def save_overlay():
    try:
        with open(OVERLAY, "w") as f:
            json.dump(_name_overlay, f, indent=2)
    except Exception:
        pass


def cleanup_overlay():
    if not _name_overlay:
        return
    tree = get_tree()
    if not tree:
        return
    known = set()
    _collect_all_ids(tree, known)
    changed = False
    for con_id in list(_name_overlay.keys()):
        if con_id not in known:
            del _name_overlay[con_id]
            changed = True
    if changed:
        save_overlay()


def _collect_all_ids(node, out):
    out.add(str(node.get("id", "")))
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        _collect_all_ids(child, out)


def collect_windows(node):
    out = []
    con_id = str(node.get("id", ""))
    if con_id in _name_overlay:
        out.append(_name_overlay[con_id])
    else:
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


def status_width():
    texts = [disk_block(), mem_block(), load_block(), vol_block(), bat_block(), time_block()]
    text_w = sum(2 * PAD_W + len(t) * CHAR_W for t in texts)
    tray_w = 6 * (2 * PAD_W + 3 * CHAR_W)
    return text_w + tray_w


def box_width(workspaces):
    w = 0
    for ws in workspaces:
        w = max(w, ws.get("rect", {}).get("width", 0))
    if not w:
        return 120
    n = len(ROW_KEYS)
    sw = status_width()
    avail = max(w - sw, 0)
    #return (avail // n) * 1
    return 131


def truncate(label, width):
    max_chars = (width - 2 * PAD_W) // CHAR_W
    if len(label) <= max_chars:
        return label
    return label[:max_chars - 1] + ELL


def vol_block():
    v = run(["pamixer", "--get-volume"])
    muted = run(["pamixer", "--get-mute"]) == "true"
    if not v:
        return "?: ?"
    return f"?: muted ({v}%)" if muted else f"?: {v}%"


def bat_block():
    try:
        with open("/sys/class/power_supply/BAT0/capacity") as f:
            cap = f.read().strip()
        with open("/sys/class/power_supply/BAT0/status") as f:
            st = f.read().strip()
    except Exception:
        return "BAT: ?"
    if st == "Charging":
        icon = "CHR"
    elif st == "Full":
        icon = "FULL"
    else:
        icon = "BAT"
    return f"{icon} {cap}%"


def mem_block():
    out = run(["free", "-h"])
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) >= 7 and f[0].startswith("Mem"):
            return f"{f[2]} | {f[6]}"
    return "MEM: ?"


def disk_block():
    out = run(["df", "-P", "-h", "/"])
    for line in out.splitlines():
        f = line.split()
        if len(f) == 6 and f[0] != "Filesystem":
            return f[3]
    return "DISK: ?"


def load_block():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[0]
    except Exception:
        return "?"


def time_block():
    return datetime.now().strftime("{ %A } %d/%m/%Y %H:%M:%S")


def render(row_keys):
    workspaces = get_workspaces()
    ws_by_name = {ws.get("name"): ws for ws in workspaces}
    apps = apps_by_ws_name()
    w = max((ws.get("rect", {}).get("width", 0) for ws in workspaces), default=0)
    width = box_width(workspaces)
    blocks = []
    for key in row_keys:
        ws = ws_by_name.get(key)
        app_list = apps.get(key, [])
        label = f"{key}:{','.join(app_list)}" if app_list else key
        text = " " + truncate(label, width) + " "
        if ws:
            if ws.get("focused"):
                bg, fg, bd = ("#009900", "#ffffff", "#006600") if key in GREEN else ("#990000", "#ffffff", "#660000")
            elif ws.get("urgent"):
                bg, fg, bd = "#7a1010", "#ffffff", "#a00000"
            else:
                bg, fg, bd = ("#2e2e2e", "#00ff00" if key in GREEN else "#ff5252", "#4a4a4a")
        else:
            bg, fg, bd = ("#1c1c1c", "#00ff00" if key in GREEN else "#ff5252", "#333333")
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
    n = len(row_keys)
    boxes_width = n * width
    sw = status_width()
    spacer_width = max(w - boxes_width - sw, 0) if w else 0
    blocks.append({"full_text": "", "separator": False, "align": "left", "min_width": spacer_width})
    for text in [disk_block(), mem_block(), load_block(), vol_block(), bat_block(), time_block()]:
        blocks.append({"full_text": text})
    return blocks


def handle_event(line):
    try:
        ev = json.loads(line)
    except Exception:
        return
    c = ev.get("container") or {}
    xid = c.get("window")
    con_id = str(c.get("id", ""))
    if ev.get("change") == "close":
        if xid is not None:
            _win_profiles.pop(xid, None)
        if con_id in _name_overlay:
            del _name_overlay[con_id]
            save_overlay()
    elif ev.get("change") == "new":
        d = marker_directory()
        if d and xid is not None:
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
    state = {"first": True}
    load_overlay()

    def emit():
        load_overlay()
        line = json.dumps(render(ROW_KEYS), ensure_ascii=False)
        if not state["first"]:
            line = "," + line
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        state["first"] = False

    print(json.dumps({"version": 1, "click_events": True}), flush=True)
    print("[", flush=True)
    emit()
    last_render = time.time()
    last_cleanup = time.time()
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
                    if now - last_cleanup >= 30:
                        cleanup_overlay()
                        last_cleanup = now
                elif now - last_event >= 2.0:
                    emit()
                    last_render = now
                    last_event = now
        except Exception:
            time.sleep(1)
        proc.wait()


if __name__ == "__main__":
    main()
